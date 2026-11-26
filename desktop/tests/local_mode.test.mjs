// Local developer mode seam: the webview may not guess which backend it is
// talking to. Rust's `bridge_backend` answers with the literal "hosted" or
// "local"; anything else is a hard error. Only a confirmed "hosted" unlock
// bearer sign-in/out, the Computer viewer, and the debug computer card.
//
// `useChat` is mounted unmodified through the existing Node/React hook seam:
// the loader below maps `@tauri-apps/api/core` and `@tauri-apps/api/event` to
// data-URL stubs that delegate to per-test handlers, so the real bridge.ts
// argument shapes and the hook's state machine are what runs here. The
// React-test-renderer cannot render App.tsx's JSX in Node, so the App-level
// mounts (sign-in dialog, Computer pane/tab, debug card listener) are pinned
// by source assertions, the same seam bearer_persistence.test.mjs uses.

import { register } from "node:module";
import { describe, it, before, afterEach } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// React 19 strips `act` from the production bundle. Pin the dev/test bundle
// before any react import below.
process.env.NODE_ENV = "test";

const CORE_STUB = `export async function invoke(command, args) {
  const handler = globalThis.__KELP_INVOKE__;
  if (!handler) throw new Error("no invoke stub installed");
  return handler(command, args);
}`;

const EVENT_STUB = `export async function listen(name, handler) {
  const record = globalThis.__KELP_LISTEN__;
  if (record) record(name, handler);
  return () => {};
}`;

const LOADER = `
const stubs = {
  "@tauri-apps/api/core": ${JSON.stringify(CORE_STUB)},
  "@tauri-apps/api/event": ${JSON.stringify(EVENT_STUB)}
};
export async function resolve(specifier, context, next) {
  const stub = stubs[specifier];
  if (stub) {
    return { shortCircuit: true, url: "data:text/javascript," + encodeURIComponent(stub) };
  }
  if (
    (specifier.startsWith("./") || specifier.startsWith("../")) &&
    typeof context.parentURL === "string" &&
    !/\\.[a-zA-Z]+$/.test(specifier)
  ) {
    return { shortCircuit: true, url: new URL(specifier + ".ts", context.parentURL).href };
  }
  return next(specifier, context);
}`;

register("data:text/javascript," + encodeURIComponent(LOADER));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

if (typeof globalThis.window === "undefined") {
  globalThis.window = {
    // isTauri() checks for this key; the IPC modules are stubbed above.
    __TAURI_INTERNALS__: {},
    addEventListener() {},
    removeEventListener() {},
  };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

let React;
let TestRenderer;
let useChat;
let parseBackendMode;

before(async () => {
  React = await import("react");
  TestRenderer = await import("react-test-renderer");
  ({ useChat } = await import("../src/useChat.ts"));
  ({ parseBackendMode } = await import("../src/bridge.ts"));
});

afterEach(() => {
  globalThis.__KELP_INVOKE__ = () => {
    throw new Error("invoke called after teardown");
  };
});

function Probe({ hook }) {
  hook.current = useChat();
  return null;
}

/**
 * An invoke fake that records every command and dispatches on it. A handler
 * that throws is rethrown to the bridge exactly as a Rust `Result::Err`
 * rejects the webview promise (a plain object in this codebase's shape).
 */
function installInvoke(handlers) {
  const calls = [];
  globalThis.__KELP_INVOKE__ = async (command, args) => {
    calls.push({ command, args });
    const handler = handlers[command];
    if (handler === undefined) {
      throw { status: 0, code: "unexpected_command", message: `unexpected ${command}`, retryable: false };
    }
    return typeof handler === "function" ? handler(args, calls) : handler;
  };
  return calls;
}

/** Route `bridge_api` by "METHOD /path" (query string ignored for lookup). */
function apiRoutes(routes) {
  return (args) => {
    const exact = `${args.method} ${args.path}`;
    const bare = exact.split("?")[0];
    const route = routes[exact] ?? routes[bare];
    if (route === undefined) {
      throw { status: 404, code: "not_found", message: `no stub for ${exact}`, retryable: false };
    }
    // A handler is invoked with the bridge arguments; a plain value is returned
    // as the command result.
    return typeof route === "function" ? route(args) : route;
  };
}

async function mountHook() {
  const hook = { current: null };
  const renderer = { current: null };
  await TestRenderer.act(async () => {
    renderer.current = TestRenderer.create(React.createElement(Probe, { hook }));
    await tick();
  });
  return { hook, renderer };
}

async function flush() {
  await TestRenderer.act(async () => {
    await tick();
  });
}

async function waitFor(predicate, label) {
  for (let i = 0; i < 200; i++) {
    if (predicate()) return;
    await flush();
  }
  throw new Error(`timed out waiting for ${label}`);
}

async function callHook(fn) {
  let result;
  await TestRenderer.act(async () => {
    result = await fn();
  });
  return result;
}

async function unmount(handle) {
  await TestRenderer.act(async () => {
    handle.renderer.current.unmount();
  });
}

const THREAD = { id: "t1", title: "One", updated_at: new Date().toISOString() };

describe("parseBackendMode", () => {
  it("accepts only the two contract literals", () => {
    assert.equal(parseBackendMode("hosted"), "hosted");
    assert.equal(parseBackendMode("local"), "local");
    for (const value of ["Hosted", "LOCAL", "local ", "", "staging", null, undefined, 0, 1, {}, ["hosted"]]) {
      assert.equal(parseBackendMode(value), null, `rejected ${JSON.stringify(value)}`);
    }
  });
});

describe("useChat mode gating", () => {
  it("hosted: looks up the mode before any chat call, then signs in normally", async () => {
    const calls = installInvoke({
      bridge_backend: "hosted",
      bridge_api: apiRoutes({
        "GET /api/threads": { threads: [THREAD] },
      }),
      bridge_sign_in: { ok: true },
    });

    const handle = await mountHook();
    await waitFor(() => handle.hook.current.phase === "ready", "hosted ready");
    assert.equal(handle.hook.current.mode, "hosted");
    assert.equal(handle.hook.current.threads.length, 1);

    // The explicit mode lookup is the very first IPC call; nothing chat-shaped
    // runs before it.
    assert.deepEqual(
      calls.map((call) => call.command).slice(0, 2),
      ["bridge_backend", "bridge_api"],
    );

    const signedIn = await callHook(() => handle.hook.current.signIn("owner-bearer"));
    assert.equal(signedIn, true);
    const signInCall = calls.find((call) => call.command === "bridge_sign_in");
    assert.ok(signInCall, "hosted sign-in reached the bridge");
    assert.deepEqual(signInCall.args, { bearer: "owner-bearer", deviceLabel: "Kelp desktop" });

    // A hosted 401 after a successful sign-in may still prompt for a bearer:
    // the App mounts its dialog for phase "signed-out".
    await unmount(handle);
  });

  it("hosted: a 401 moves to signed-out and sign-in stays available", async () => {
    const calls = installInvoke({
      bridge_backend: "hosted",
      bridge_api: () => {
        throw { status: 401, code: "unauthorized", message: "Sign in again.", retryable: false };
      },
      bridge_sign_in: { ok: true },
    });

    const handle = await mountHook();
    await waitFor(() => handle.hook.current.phase === "signed-out", "hosted signed-out");
    assert.equal(handle.hook.current.mode, "hosted");

    const signedIn = await callHook(() => handle.hook.current.signIn("owner-bearer"));
    assert.equal(signedIn, true);
    assert.ok(calls.some((call) => call.command === "bridge_sign_in"));
    await unmount(handle);
  });

  it("hosted: a successful Retry refresh does not skip the sign-in dialog", async () => {
    let unauthorized = true;
    const calls = installInvoke({
      bridge_backend: "hosted",
      bridge_api: () => {
        if (unauthorized) {
          throw { status: 401, code: "unauthorized", message: "Sign in again.", retryable: false };
        }
        return { threads: [THREAD] };
      },
      bridge_sign_in: { ok: true },
    });

    const handle = await mountHook();
    await waitFor(() => handle.hook.current.phase === "signed-out", "hosted signed-out");

    // The server session recovers, but a Retry/background refresh must not
    // mark the UI ready behind the sign-in dialog: only the explicit sign-in
    // path restarts the session and moves it to ready.
    unauthorized = false;
    assert.equal(await callHook(() => handle.hook.current.refreshThreads()), true);
    assert.equal(handle.hook.current.phase, "signed-out");
    assert.equal(handle.hook.current.threads.length, 1);

    assert.equal(await callHook(() => handle.hook.current.signIn("owner-bearer")), true);
    assert.equal(handle.hook.current.phase, "ready");
    assert.ok(calls.some((call) => call.command === "bridge_sign_in"));
    await unmount(handle);
  });

  it("local: reads chat without auth, and refuses the hosted sign-in/out and debug card", async () => {
    const calls = installInvoke({
      bridge_backend: "local",
      bridge_api: apiRoutes({
        "GET /api/threads": { threads: [THREAD] },
        "GET /api/threads/t1": {
          ok: true,
          thread: THREAD,
          messages: [],
          last_seq: 0,
          has_more: false,
        },
      }),
      bridge_open_stream: () => undefined,
      bridge_close_stream: () => undefined,
      bridge_sign_in: { ok: true },
      bridge_sign_out: { ok: true },
    });

    const handle = await mountHook();
    await waitFor(() => handle.hook.current.phase === "ready", "local ready");
    assert.equal(handle.hook.current.mode, "local");
    assert.equal(handle.hook.current.threads.length, 1);
    assert.equal(handle.hook.current.failure, null);

    // Local chat/read stays on the same IPC path as hosted.
    await callHook(() => handle.hook.current.openThread("t1"));
    await waitFor(() => handle.hook.current.activeId === "t1", "local thread open");
    assert.ok(
      calls.some(
        (call) => call.command === "bridge_open_stream" && call.args?.threadId === "t1",
      ),
      "Local still opens the chat stream",
    );

    // Debug computer cards are hosted-only; the fixture cannot fabricate one.
    assert.equal(handle.hook.current.injectComputerCard(), false);
    assert.equal(handle.hook.current.messages.length, 0);

    // Hosted auth endpoints are never invoked from this UI in Local mode.
    assert.equal(await callHook(() => handle.hook.current.signIn("pasted-key")), false);
    assert.equal(await callHook(() => handle.hook.current.signOut()), false);
    assert.equal(calls.some((call) => call.command === "bridge_sign_in"), false);
    assert.equal(calls.some((call) => call.command === "bridge_sign_out"), false);
    // Refusing sign-in reports a safe failure, not a credential prompt.
    assert.equal(handle.hook.current.failure?.code, "auth_unavailable");
    assert.equal(handle.hook.current.phase, "ready");
    // A direct signOut() call on Local bails before touching the active chat:
    // the thread stays open and no stream is torn down. Only a confirmed
    // hosted backend may clear that state.
    assert.equal(handle.hook.current.activeId, "t1");
    assert.equal(handle.hook.current.messages.length, 0);
    assert.equal(
      calls.some((call) => call.command === "bridge_close_stream"),
      false,
      "Local signOut must not close the chat stream",
    );
    // Approval decisions are hosted-only; the Local sidecar denies them, so a
    // stale approval card cannot send a decision through the bridge.
    await callHook(() => handle.hook.current.decide("approval-1", "approve"));
    assert.equal(
      calls.some(
        (call) =>
          call.command === "bridge_api" &&
          String(call.args?.path).startsWith("/api/approvals"),
      ),
      false,
    );
    await unmount(handle);
  });

  it("local: a 401 never becomes a bearer prompt", async () => {
    installInvoke({
      bridge_backend: "local",
      bridge_api: () => {
        throw { status: 401, code: "unauthorized", message: "Not signed in.", retryable: false };
      },
      bridge_sign_in: { ok: true },
    });

    const handle = await mountHook();
    await waitFor(() => handle.hook.current.phase === "error", "local 401 error");
    assert.equal(handle.hook.current.mode, "local");
    assert.notEqual(handle.hook.current.phase, "signed-out");
    assert.equal(handle.hook.current.failure?.status, 401);
    await unmount(handle);
  });

  it("unknown literal: errors out before any chat call instead of defaulting to hosted", async () => {
    const calls = installInvoke({ bridge_backend: "staging" });

    const handle = await mountHook();
    await waitFor(() => handle.hook.current.phase === "error", "unknown mode error");
    assert.equal(handle.hook.current.mode, null);
    assert.equal(handle.hook.current.failure?.code, "unknown_backend_mode");
    assert.deepEqual(calls.map((call) => call.command), ["bridge_backend"]);

    // Programmatic calls cannot get around the disabled New chat/composer:
    // with no confirmed mode every chat action is a no-op, not a guess.
    await callHook(() => handle.hook.current.openThread("t1"));
    await callHook(() => handle.hook.current.newChat());
    await callHook(() => handle.hook.current.send("hello"));
    await callHook(() => handle.hook.current.stop());
    await callHook(() => handle.hook.current.retry());
    await callHook(() => handle.hook.current.decide("a1", "approve"));
    assert.deepEqual(
      calls.map((call) => call.command),
      ["bridge_backend"],
      "unknown mode issues no chat or auth IPC",
    );
    assert.equal(handle.hook.current.activeId, null);
    assert.equal(handle.hook.current.failure?.code, "unknown_backend_mode");

    // Sign-in on an unknown mode declines without replacing the lookup error.
    assert.equal(await callHook(() => handle.hook.current.signIn("pasted")), false);
    assert.equal(handle.hook.current.failure?.code, "unknown_backend_mode");
    assert.equal(calls.some((call) => call.command === "bridge_sign_in"), false);
    await unmount(handle);
  });

  it("lookup rejection: errors out instead of defaulting to hosted", async () => {
    const calls = installInvoke({
      bridge_backend: () => {
        throw new Error("backend mode unavailable");
      },
    });

    const handle = await mountHook();
    await waitFor(() => handle.hook.current.phase === "error", "lookup rejection error");
    assert.equal(handle.hook.current.mode, null);
    assert.equal(handle.hook.current.failure?.code, "unknown_backend_mode");
    assert.deepEqual(calls.map((call) => call.command), ["bridge_backend"]);

    // Retry re-asks Rust for the mode rather than starting chat blind.
    const retryCalls = installInvoke({
      bridge_backend: () => {
        throw { status: 0, code: "no_bridge", message: "The desktop shell is not running.", retryable: false };
      },
    });
    assert.equal(await callHook(() => handle.hook.current.refreshThreads()), false);
    assert.equal(handle.hook.current.failure?.code, "no_bridge");
    assert.equal(handle.hook.current.mode, null);
    assert.equal(handle.hook.current.phase, "error");
    assert.deepEqual(retryCalls.map((call) => call.command), ["bridge_backend"]);

    // Controlled recovery: the next explicit lookup answers exactly "local"
    // and the thread list succeeds. Retry must finish startup — ready phase,
    // real threads, no banner — and touch no auth endpoint.
    const recoveryCalls = installInvoke({
      bridge_backend: "local",
      bridge_api: apiRoutes({ "GET /api/threads": { threads: [THREAD] } }),
    });
    assert.equal(await callHook(() => handle.hook.current.refreshThreads()), true);
    assert.equal(handle.hook.current.mode, "local");
    assert.equal(handle.hook.current.phase, "ready");
    assert.equal(handle.hook.current.threads.length, 1);
    assert.equal(handle.hook.current.failure, null);
    assert.deepEqual(recoveryCalls.map((call) => call.command), ["bridge_backend", "bridge_api"]);
    assert.equal(
      recoveryCalls.some(
        (call) => call.command === "bridge_sign_in" || call.command === "bridge_sign_out",
      ),
      false,
    );
    await unmount(handle);
  });
});

describe("App mounts the hosted-only surfaces behind the mode", () => {
  const root = new URL("..", import.meta.url);
  const read = (rel) => readFileSync(new URL(rel, root), "utf8");

  it("enables the viewer only for a confirmed hosted mode", () => {
    const app = read("src/App.tsx");
    assert.match(app, /useViewer\(\{ enabled: chat\.phase === "ready" && hosted \}\)/);
    assert.match(app, /const hosted = chat\.mode === "hosted";/);
  });

  it("mounts the sign-in dialog, Sign out, and the Sign in action only when hosted", () => {
    const app = read("src/App.tsx");
    assert.match(app, /\{hosted && \(\s*<Dialog/);
    assert.match(app, /\{hosted && \(\s*<Box sx=\{\{ p: 1\.5 \}\}>/);
    assert.match(app, /\{hosted && \(\s*<Button color="inherit" size="small" onClick=\{\(\) => setAskSignIn\(true\)\}/);
  });

  it("mounts neither the Computer tab nor the viewport pane outside hosted", () => {
    const app = read("src/App.tsx");
    assert.match(app, /\{hosted && \(\s*<Tab\s+value="computer"/);
    assert.match(app, /\{hosted && \(\s*<Box\s+inert=\{!computerOpen\}/);
    assert.match(app, /label="Local \(developer\)"/);
  });

  it("gates debug card event injection to hosted, and passes no viewer host in Local", () => {
    const app = read("src/App.tsx");
    assert.match(app, /if \(chat\.mode !== "hosted"\) return;/);
    assert.match(app, /if \(current\.mode !== "hosted"\) return;/);
    // The viewer host is only built when hosted, so a Local transcript card
    // has nothing to connect with.
    assert.match(app, /hosted\s*\?\s*\{\s*viewer:/);
  });

  it("never exposes a Local URL or port through the UI", () => {
    const app = read("src/App.tsx");
    const bridge = read("src/bridge.ts");
    assert.doesNotMatch(app, /serverUrl|127\.0\.0\.1|localhost/);
    assert.doesNotMatch(bridge, /127\.0\.0\.1|localhost/);
    assert.match(bridge, /backendMode: \(\) => call<unknown>\("bridge_backend"\)/);
  });
});

describe("Overlay and transcript keep hosted auth/approval actions behind the mode", () => {
  const root = new URL("..", import.meta.url);
  const read = (rel) => readFileSync(new URL(rel, root), "utf8");
  const count = (text, needle) => text.split(needle).length - 1;

  it("OverlayPanel: placeholder asks for sign-in only when hosted and signed out", () => {
    const overlay = read("src/OverlayPanel.tsx");
    assert.match(overlay, /const hosted = chat\.mode === "hosted";/);
    assert.match(overlay, /const signInRequired = hosted && chat\.phase === "signed-out";/);
    // Exactly one sign-in literal, in the placeholder's sign-in branch. Local
    // and unconfirmed modes must not be described as signed out.
    assert.equal(count(overlay, '"Sign in from the full app"'), 1);
    assert.match(overlay, /placeholder=\{placeholder\}/);
    assert.match(
      overlay,
      /const placeholder = ready\s*\?\s*"Ask Kelp…"\s*:\s*signInRequired\s*\?\s*"Sign in from the full app"\s*:\s*chat\.phase === "loading"\s*\?\s*"Connecting…"\s*:\s*"Kelp is unavailable";/,
    );
  });

  it("OverlayPanel: expanded fallback is neutral outside hosted signed-out", () => {
    const overlay = read("src/OverlayPanel.tsx");
    assert.equal(count(overlay, '"Sign in from the full app to chat here."'), 1);
    // The old unconditional fallback made every not-ready phase look like a
    // missing credential.
    assert.doesNotMatch(
      overlay,
      /chat\.failure\?\.message \?\? "Sign in from the full app to chat here\."/,
    );
    assert.match(
      overlay,
      /signInRequired\s*\?\s*"Sign in from the full app to chat here\."\s*:\s*chat\.failure\?\.message \?\? "Kelp is unavailable right now\."/,
    );
  });

  it("OverlayPanel: approval decisions are only wired on hosted", () => {
    const overlay = read("src/OverlayPanel.tsx");
    assert.match(overlay, /const decide = hosted \? chat\.decide : undefined;/);
    assert.match(overlay, /onDecide=\{decide\}/);
    assert.match(
      overlay,
      /onDecide\?: \(id: string, decision: "approve" \| "deny"\) => void;/,
    );
  });

  it("App: the transcript receives no decision handler outside hosted", () => {
    const app = read("src/App.tsx");
    assert.match(app, /onDecide=\{hosted \? chat\.decide : undefined\}/);
    assert.match(
      app,
      /onDecide\?: \(id: string, decision: "approve" \| "deny"\) => void;/,
    );
  });

  it("Blocks: approval buttons render only when a decision handler exists", () => {
    const blocks = read("src/Blocks.tsx");
    assert.match(blocks, /\{pending && onDecide \? \(/);
    // The read-only status caption remains the fallback for every other state,
    // including a stale pending card in a Local transcript.
    assert.match(blocks, /APPROVAL_STATUS\[approval\.status\] \?\? approval\.status/);
    assert.doesNotMatch(blocks, /onDecide\?\.\(/, "no no-op buttons behind an optional call");
  });
});
