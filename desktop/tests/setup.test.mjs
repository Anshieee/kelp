// First-run setup and recovery (#83). The rules about what the screen may
// offer live in src/setup.ts and are pinned here directly; the hook test at
// the bottom pins how Rust's status reaches the screen.

import { register } from "node:module";
import { describe, it, before } from "node:test";
import assert from "node:assert/strict";

// See use_viewer.test.mjs: React 19 needs the dev bundle for `act`.
process.env.NODE_ENV = "test";
register(new URL("./helpers/hook_loader.mjs", import.meta.url));
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

import { SETUP_STEPS, UNKNOWN_FAILURE, parseSetupStatus, setupView } from "../src/setup.ts";

const actions = (status) => setupView(status).actions.map((a) => a.action);

describe("parseSetupStatus", () => {
  it("accepts each phase the contract defines", () => {
    assert.deepEqual(parseSetupStatus({ phase: "choose", hostedAvailable: true }), { phase: "choose", hostedAvailable: true });
    for (const step of SETUP_STEPS) {
      assert.deepEqual(parseSetupStatus({ phase: "verifying", step }), { phase: "verifying", step });
    }
    assert.deepEqual(
      parseSetupStatus({ phase: "failed", reason: "guard-failed", retryable: false }),
      { phase: "failed", reason: "guard-failed", retryable: false },
    );
    assert.deepEqual(parseSetupStatus({ phase: "ready", mode: "local" }), { phase: "ready", mode: "local" });
    assert.deepEqual(parseSetupStatus({ phase: "ready", mode: "hosted" }), { phase: "ready", mode: "hosted" });
  });

  it("turns anything malformed into a failure, never a choice", () => {
    for (const value of [
      undefined,
      null,
      "choose",
      42,
      {},
      { phase: "choose!" },
      { phase: "verifying", step: "install" },
      { phase: "ready", mode: "remote" },
      { phase: "ready" },
    ]) {
      assert.deepEqual(parseSetupStatus(value), UNKNOWN_FAILURE, JSON.stringify(value));
    }
  });

  it("only offers Hosted when Rust says so with a literal true", () => {
    for (const hostedAvailable of [undefined, "true", 1, null]) {
      assert.deepEqual(parseSetupStatus({ phase: "choose", hostedAvailable }), { phase: "choose", hostedAvailable: false });
    }
  });

  it("drops every field the contract does not define", () => {
    const parsed = parseSetupStatus({
      phase: "failed",
      reason: "bundle-broken",
      retryable: true,
      message: "Traceback: /opt/kelp/python3 failed",
      path: "/opt/kelp/python3",
      bearer: "secret",
      port: 51234,
    });
    assert.deepEqual(Object.keys(parsed).sort(), ["phase", "reason", "retryable"]);
  });

  it("maps an unknown failure reason to the generic one", () => {
    assert.deepEqual(parseSetupStatus({ phase: "failed", reason: "ENOENT", retryable: false }), {
      phase: "failed",
      reason: "unknown",
      retryable: false,
    });
  });
});

describe("setupView", () => {
  it("offers Run on this computer and Quit on first run, Hosted only when available", () => {
    assert.deepEqual(actions({ phase: "choose", hostedAvailable: true }), ["local", "hosted", "quit"]);
    assert.deepEqual(actions({ phase: "choose", hostedAvailable: false }), ["local", "quit"]);
  });

  it("words Hosted as this launch only", () => {
    const hosted = setupView({ phase: "choose", hostedAvailable: true }).actions.find((a) => a.action === "hosted");
    assert.match(hosted.label, /this time/);
    assert.match(setupView({ phase: "ready", mode: "hosted" }).body, /this launch only/);
  });

  it("never offers Hosted from a failure", () => {
    for (const reason of ["bundle-missing", "bundle-broken", "handshake-failed", "guard-failed", "unknown"]) {
      for (const retryable of [true, false]) {
        const offered = actions({ phase: "failed", reason, retryable });
        assert.ok(!offered.includes("hosted"), `${reason}/${retryable}`);
        assert.ok(!offered.includes("local"), `${reason}/${retryable}`);
        assert.deepEqual(offered, retryable ? ["retry", "quit"] : ["quit"]);
      }
    }
  });

  it("gives every failure fixed copy with no paths, commands, or keys", () => {
    for (const reason of ["bundle-missing", "bundle-broken", "handshake-failed", "guard-failed", "unknown"]) {
      const { body } = setupView({ phase: "failed", reason, retryable: true });
      assert.ok(body.length > 0);
      assert.doesNotMatch(body, /[/\\$`]|python|sudo|pip|api key|token|port/i, reason);
    }
  });

  it("marks steps done, active, and pending in order", () => {
    const { steps, actions: offered } = setupView({ phase: "verifying", step: "handshake" });
    assert.deepEqual(steps.map((s) => s.state), ["done", "active", "pending"]);
    assert.deepEqual(offered.map((a) => a.action), ["quit"]);
  });

  it("offers nothing once ready", () => {
    assert.deepEqual(actions({ phase: "ready", mode: "local" }), []);
  });
});

describe("useSetup", () => {
  let React;
  let TestRenderer;
  let useSetup;

  before(async () => {
    React = await import("react");
    TestRenderer = await import("react-test-renderer");
    ({ useSetup } = await import("../src/useSetup.ts"));
  });

  function fakeDriver({ initial, rejectStatus = false, rejectCommands = false } = {}) {
    const listeners = new Set();
    let resolveStatus;
    const calls = [];
    const driver = {
      status: () =>
        new Promise((resolve, reject) => {
          resolveStatus = rejectStatus ? () => reject(new Error("no shell")) : () => resolve(initial);
        }),
      subscribe(handler) {
        listeners.add(handler);
        return () => listeners.delete(handler);
      },
      choose: async (mode) => {
        calls.push(["choose", mode]);
        if (rejectCommands) throw new Error("refused");
      },
      retry: async () => void calls.push(["retry"]),
      quit: async () => void calls.push(["quit"]),
    };
    return {
      driver,
      calls,
      answer: () => resolveStatus(),
      push: (status) => listeners.forEach((listener) => listener(status)),
    };
  }

  async function mount(driver) {
    const hook = { current: null };
    function Probe() {
      hook.current = useSetup(driver);
      return null;
    }
    let renderer;
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(React.createElement(Probe));
    });
    return { hook, renderer };
  }

  it("starts empty, then shows the parsed initial status", async () => {
    const fake = fakeDriver({ initial: { phase: "choose", hostedAvailable: true, path: "/x" } });
    const { hook } = await mount(fake.driver);
    assert.equal(hook.current.status, null);
    await TestRenderer.act(async () => fake.answer());
    assert.deepEqual(hook.current.status, { phase: "choose", hostedAvailable: true });
  });

  it("lets a pushed status win over a late initial answer", async () => {
    const fake = fakeDriver({ initial: { phase: "choose", hostedAvailable: true } });
    const { hook } = await mount(fake.driver);
    await TestRenderer.act(async () => fake.push({ phase: "verifying", step: "runtime" }));
    await TestRenderer.act(async () => fake.answer());
    assert.deepEqual(hook.current.status, { phase: "verifying", step: "runtime" });
  });

  it("shows the generic failure when there is no shell to ask", async () => {
    const fake = fakeDriver({ rejectStatus: true });
    const { hook } = await mount(fake.driver);
    await TestRenderer.act(async () => fake.answer());
    assert.deepEqual(hook.current.status, UNKNOWN_FAILURE);
  });

  it("sends the chosen action and stays busy until Rust reports back", async () => {
    const fake = fakeDriver({ initial: { phase: "choose", hostedAvailable: false } });
    const { hook } = await mount(fake.driver);
    await TestRenderer.act(async () => fake.answer());
    await TestRenderer.act(async () => hook.current.act("local"));
    assert.deepEqual(fake.calls, [["choose", "local"]]);
    assert.equal(hook.current.busy, true);
    await TestRenderer.act(async () => fake.push({ phase: "verifying", step: "runtime" }));
    assert.equal(hook.current.busy, false);
  });

  it("recovers from a refused command instead of hanging on a disabled button", async () => {
    const fake = fakeDriver({ initial: { phase: "choose", hostedAvailable: true }, rejectCommands: true });
    const { hook } = await mount(fake.driver);
    await TestRenderer.act(async () => fake.answer());
    await TestRenderer.act(async () => hook.current.act("hosted"));
    assert.equal(hook.current.busy, false);
    assert.deepEqual(hook.current.status, UNKNOWN_FAILURE);
  });

  it("stops listening when unmounted", async () => {
    const fake = fakeDriver({ initial: { phase: "choose", hostedAvailable: false } });
    const { hook, renderer } = await mount(fake.driver);
    await TestRenderer.act(async () => fake.answer());
    await TestRenderer.act(async () => renderer.unmount());
    const before = hook.current.status;
    fake.push({ phase: "ready", mode: "local" });
    assert.deepEqual(hook.current.status, before);
  });
});
