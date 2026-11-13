// Seam test for `useViewer`: managed sessions never construct RFB, explicit
// dials go through with the stripped URL, a second session replaces the first,
// and sign-out tears everything down.
//
// The stub stands in for noVNC's module record (see helpers/hook_loader.mjs),
// so the hook under test is unmodified and no production code forks on test
// flags. Timer assertions check scheduling, not firing — the reconnect budget
// is ~2 minutes and the test must not wait it out.
//
// One documented limit: `import.meta.env` is build-baked, so under plain
// `node --test` the hook always reads the managed transport. The rfb-transport
// routing of `openSessionUrl` (stripped URL + sessionId propagation) is pinned
// at helper level in computer_viewer.test.mjs instead; what this file proves
// is the seam that ships — what actually constructs RFB, with what URL, and
// what tears it down.

import { register } from "node:module";
import { describe, it, before } from "node:test";
import assert from "node:assert/strict";

// React 19 strips `act` from the production bundle. The dev-only path is
// what the test needs, and nothing here ships, so pin the env first —
// before any react import below. Unconditional: CI shells often export
// NODE_ENV=production, and `??=` would keep the act-less bundle.
process.env.NODE_ENV = "test";

register(new URL("./helpers/hook_loader.mjs", import.meta.url));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const timeoutCalls = [];
if (typeof globalThis.window === "undefined") {
  globalThis.window = {
    addEventListener() {},
    removeEventListener() {},
    setTimeout: (fn, ms) => {
      timeoutCalls.push(ms);
      return timeoutCalls.length;
    },
    clearTimeout() {},
  };
}
if (typeof globalThis.localStorage === "undefined") {
  const store = new Map();
  globalThis.localStorage = {
    getItem: (key) => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => void store.set(String(key), String(value)),
    removeItem: (key) => void store.delete(key),
  };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

let React;
let TestRenderer;
let useViewer;
let instances;
let resetInstances;

before(async () => {
  React = await import("react");
  TestRenderer = await import("react-test-renderer");
  ({ useViewer } = await import("../src/useViewer.ts"));
  ({ instances, resetInstances } = await import("./helpers/novnc_rfb_stub.mjs"));
});

function Probe({ hook, enabled }) {
  hook.current = useViewer({ enabled });
  return null;
}

// The connection effect resolves a dynamic import before it constructs RFB,
// so the tick count that suffices on one machine can starve another. Poll for
// the wired instance instead of counting flushes.
async function waitForInstance(index) {
  for (let i = 0; i < 50; i++) {
    const instance = instances[index];
    if (instance && instance.listeners.disconnect) return instance;
    await tick();
  }
  throw new Error(`RFB instance ${index} never wired up`);
}

async function mountViewer(enabled) {
  const hook = { current: null };
  let renderer = null;
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(Probe, { hook, enabled }));
    await tick();
  });
  assert.ok(hook.current, "hook handle captured");
  // The fake host the effect needs: the stub stores it without touching DOM.
  await TestRenderer.act(async () => {
    hook.current.setHostRef({ replaceChildren() {} });
    await tick();
  });
  return {
    hook,
    renderer,
    async update(enabled) {
      await TestRenderer.act(async () => {
        renderer.update(React.createElement(Probe, { hook, enabled }));
        await tick();
      });
    },
  };
}

describe("useViewer seam", () => {
  it("a managed session never constructs RFB; it offers the browser link", async () => {
    const { hook } = await mountViewer(true);
    resetInstances();
    await TestRenderer.act(async () => {
      hook.current.openSessionUrl({ id: "sess_1", url: "https://boat.example.test/session/abc" });
      await tick();
    });
    assert.equal(instances.length, 0);
    assert.equal(hook.current.status, "error");
    assert.match(hook.current.detail, /browser/);
  });

  it("an explicit dial constructs RFB once with the stripped URL, then connects", async () => {
    const { hook } = await mountViewer(true);
    resetInstances();
    await TestRenderer.act(async () => {
      hook.current.setUrlInput("ws://127.0.0.1:6080/websockify?token=secret");
      await tick();
    });
    await TestRenderer.act(async () => {
      hook.current.connect();
    });
    await waitForInstance(0);
    await TestRenderer.act(async () => {
      await tick();
    });
    assert.equal(instances.length, 1);
    // The live socket carries the server's auth token; what is displayed and
    // remembered is the stripped form. Tokens never persist, but the dial is
    // where websockify expects its auth — stripping there would break login.
    assert.equal(instances[0].url, "ws://127.0.0.1:6080/websockify?token=secret");
    assert.equal(instances[0].viewOnly, true);
    assert.equal(globalThis.localStorage.getItem("kelp.computer.viewerUrl"), "ws://127.0.0.1:6080/websockify");
    instances[0].fire("connect");
    await TestRenderer.act(async () => {
      await tick();
    });
    assert.equal(hook.current.status, "connected");
  });

  it("a second dial replaces the first; a drop schedules a retry against the redacted URL", async () => {
    const { hook } = await mountViewer(true);
    resetInstances();
    timeoutCalls.length = 0;
    await TestRenderer.act(async () => {
      hook.current.setUrlInput("ws://127.0.0.1:6080/websockify");
      await tick();
    });
    await TestRenderer.act(async () => {
      hook.current.connect();
    });
    await waitForInstance(0);
    await TestRenderer.act(async () => {
      await tick();
    });
    assert.equal(instances.length, 1);
    await TestRenderer.act(async () => {
      hook.current.setUrlInput("ws://127.0.0.1:6099/other?token=secret");
      await tick();
    });
    await TestRenderer.act(async () => {
      hook.current.connect();
    });
    await waitForInstance(1);
    await TestRenderer.act(async () => {
      await tick();
    });
    assert.equal(instances.length, 2);
    assert.equal(instances[0].disconnected, true);
    assert.equal(instances[1].url, "ws://127.0.0.1:6099/other?token=secret");
    // A socket that never opened reports the address it tried — redacted —
    // and budgets a retry without waiting it out here.
    await TestRenderer.act(async () => {
      instances[1].fire("disconnect", { clean: false });
      await tick();
    });
    assert.equal(hook.current.status, "disconnected");
    assert.match(hook.current.detail, /6099/);
    assert.doesNotMatch(hook.current.detail, /secret/);
    assert.equal(timeoutCalls.length, 1);
  });

  it("the password goes straight to RFB and dies with the session; sign-out tears down", async () => {
    const mounted = await mountViewer(true);
    const { hook } = mounted;
    resetInstances();
    await TestRenderer.act(async () => {
      hook.current.setUrlInput("ws://127.0.0.1:6080/websockify");
      await tick();
    });
    await TestRenderer.act(async () => {
      hook.current.connect();
    });
    await waitForInstance(0);
    await TestRenderer.act(async () => {
      await tick();
    });
    assert.equal(instances.length, 1);
    await TestRenderer.act(async () => {
      instances[0].fire("credentialsrequired", { types: ["password"] });
      await tick();
    });
    assert.equal(hook.current.status, "auth");
    await TestRenderer.act(async () => {
      hook.current.setPassword("s3cret");
      await tick();
    });
    await TestRenderer.act(async () => {
      hook.current.submitPassword();
      await tick();
    });
    assert.deepEqual(instances[0].sentCredentials, { password: "s3cret" });
    assert.equal(hook.current.password, "");
    // Sign-out: everything stops, the socket closes, nothing stays selected.
    await mounted.update(false);
    await TestRenderer.act(async () => {
      await tick();
    });
    assert.equal(instances[0].disconnected, true);
    assert.equal(hook.current.status, "idle");
    assert.equal(hook.current.activeSessionId, null);
  });
});
