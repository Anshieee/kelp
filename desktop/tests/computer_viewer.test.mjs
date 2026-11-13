import { beforeEach, describe, it } from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_VIEWER_URL,
  RECONNECT_DELAYS_MS,
  TUNNEL_VIEWER_URL,
  VIEWER_URL_STORAGE_KEY,
  applyViewerSettings,
  buttonStatusLabel,
  computerTransport,
  defaultViewerUrl,
  disconnectDetail,
  formatElapsed,
  isCleartextRemote,
  isDefaultViewerUrl,
  isRfbTransport,
  normaliseViewerUrl,
  planCredentials,
  readStoredViewerUrl,
  reconnectDelay,
  redactViewerUrl,
  rememberViewerUrl,
  resolveViewerTarget,
  statusLabel,
  stripViewerSecrets,
  unsupportedCredentialsMessage,
  viewerConnectionOptions,
  viewerUrlProblem,
  writeStoredViewerUrl,
} from "../src/computerViewer.ts";

// noVNC speaks WebSocket only, so the URL rules are the difference between a
// viewer that connects and one that sits on "Connecting…" forever.

describe("normaliseViewerUrl", () => {
  it("accepts a ws:// endpoint unchanged", () => {
    assert.equal(
      normaliseViewerUrl("ws://127.0.0.1:6080/websockify"),
      "ws://127.0.0.1:6080/websockify",
    );
  });

  it("keeps wss:// (and its query) for a tunnelled endpoint", () => {
    assert.equal(
      normaliseViewerUrl("wss://vm.example.test/view?token=abc"),
      "wss://vm.example.test/view?token=abc",
    );
  });

  it("rewrites http(s) — what tunnel providers hand out — to ws(s)", () => {
    assert.equal(
      normaliseViewerUrl("http://127.0.0.1:6080/websockify"),
      "ws://127.0.0.1:6080/websockify",
    );
    assert.equal(
      normaliseViewerUrl("https://vm.example.test/vnc"),
      "wss://vm.example.test/vnc",
    );
  });

  it("treats a bare host as a local ws endpoint", () => {
    assert.equal(
      normaliseViewerUrl("127.0.0.1:6080/websockify"),
      "ws://127.0.0.1:6080/websockify",
    );
  });

  it("trims surrounding whitespace", () => {
    assert.equal(normaliseViewerUrl("  ws://h:1/x  "), "ws://h:1/x");
  });

  it("rejects what it cannot use", () => {
    for (const bad of ["", "   ", "not a url", "ftp://host/x", "ws://"]) {
      assert.equal(normaliseViewerUrl(bad), null, JSON.stringify(bad));
    }
  });

  it("ships a usable local default", () => {
    assert.equal(normaliseViewerUrl(DEFAULT_VIEWER_URL), DEFAULT_VIEWER_URL);
  });
});

describe("reconnectDelay", () => {
  it("follows the published ladder and then caps", () => {
    assert.deepEqual(RECONNECT_DELAYS_MS, [1_000, 2_000, 5_000, 10_000, 15_000, 30_000, 60_000]);
    assert.equal(reconnectDelay(1), 1_000);
    assert.equal(reconnectDelay(2), 2_000);
    assert.equal(reconnectDelay(3), 5_000);
    assert.equal(reconnectDelay(4), 10_000);
    assert.equal(reconnectDelay(5), 15_000);
    assert.equal(reconnectDelay(6), 30_000);
    assert.equal(reconnectDelay(7), 60_000);
    assert.equal(reconnectDelay(99), 60_000);
  });

  it("never returns something silly for an odd attempt number", () => {
    assert.equal(reconnectDelay(0), 1_000);
    assert.equal(reconnectDelay(-4), 1_000);
    assert.equal(reconnectDelay(2.7), 2_000);
  });

  it("spends its whole budget in about two minutes, then the caller gives up", () => {
    const total = RECONNECT_DELAYS_MS.reduce((sum, delay) => sum + delay, 0);
    assert.equal(total, 123_000);
  });
});

describe("buttonStatusLabel", () => {
  it("maps every viewer state onto the four advertised states", () => {
    assert.equal(buttonStatusLabel("connecting"), "Connecting");
    assert.equal(buttonStatusLabel("auth"), "Password needed");
    assert.equal(buttonStatusLabel("connected"), "Live");
    assert.equal(buttonStatusLabel("idle"), "Disconnected");
    assert.equal(buttonStatusLabel("disconnected"), "Disconnected");
    assert.equal(buttonStatusLabel("error"), "Disconnected");
  });
});

describe("statusLabel", () => {
  it("labels every one of the six states", () => {
    const expected = {
      idle: "Not connected",
      connecting: "Connecting…",
      auth: "Password needed",
      connected: "Connected",
      disconnected: "Disconnected",
      error: "Can't connect",
    };
    for (const [status, label] of Object.entries(expected)) {
      assert.equal(statusLabel(status), label);
    }
  });
});

describe("remembered viewer url", () => {
  let store;

  beforeEach(() => {
    store = new Map();
    globalThis.localStorage = {
      getItem: (key) => (store.has(key) ? store.get(key) : null),
      setItem: (key, value) => {
        store.set(key, String(value));
      },
      removeItem: (key) => {
        store.delete(key);
      },
    };
  });

  it("round-trips through storage", () => {
    assert.equal(readStoredViewerUrl(), null);
    writeStoredViewerUrl("wss://vm.example.test/view");
    assert.equal(readStoredViewerUrl(), "wss://vm.example.test/view");
    assert.equal(store.get(VIEWER_URL_STORAGE_KEY), "wss://vm.example.test/view");
  });

  it("treats a blank value as absent", () => {
    writeStoredViewerUrl("   ");
    assert.equal(readStoredViewerUrl(), null);
  });

  it("survives storage that throws (private mode, disabled storage)", () => {
    globalThis.localStorage = {
      getItem() {
        throw new Error("denied");
      },
      setItem() {
        throw new Error("denied");
      },
    };
    assert.equal(readStoredViewerUrl(), null);
    assert.doesNotThrow(() => writeStoredViewerUrl("ws://h:1/x"));
  });
});

describe("viewer connection settings", () => {
  // noVNC 1.7 reads only `shared` from the constructor; the rest are
  // post-construction properties. Passing them as options would silently run
  // the viewer interactive.
  it("passes the constructor nothing but shared", () => {
    assert.deepEqual(viewerConnectionOptions(), { shared: true });
  });

  it("applies view-only and never resizes the session", () => {
    const viewer = {};
    applyViewerSettings(viewer);
    assert.equal(viewer.viewOnly, true);
    assert.equal(viewer.scaleViewport, true);
    assert.equal(viewer.resizeSession, false);
  });
});

describe("planCredentials", () => {
  it("answers a standard VNC password prompt", () => {
    assert.deepEqual(planCredentials(["password"]), { kind: "password" });
  });

  it("refuses anything beyond a password", () => {
    assert.deepEqual(planCredentials(["username", "password"]), {
      kind: "unsupported",
      requested: ["username"],
    });
    assert.deepEqual(planCredentials(["username", "password", "target"]), {
      kind: "unsupported",
      requested: ["username", "target"],
    });
  });

  it("treats unknown credential types as unsupported, not as a password ask", () => {
    assert.deepEqual(planCredentials(["certificate"]), {
      kind: "unsupported",
      requested: ["certificate"],
    });
  });

  it("de-duplicates and treats an empty ask as a password prompt", () => {
    assert.deepEqual(planCredentials(["password", "username", "username"]), {
      kind: "unsupported",
      requested: ["username"],
    });
    assert.deepEqual(planCredentials([]), { kind: "password" });
  });
});

describe("unsupportedCredentialsMessage", () => {
  it("names what was asked for", () => {
    const message = unsupportedCredentialsMessage(["username", "target"]);
    assert.match(message, /username/);
    assert.match(message, /target/);
    assert.match(message, /view-only/);
  });
});

describe("viewerUrlProblem", () => {
  it("accepts a plain websockify endpoint", () => {
    assert.equal(viewerUrlProblem("ws://127.0.0.1:6080/websockify"), null);
  });

  it("flags the standalone vnc.html page and says what to paste instead", () => {
    const problem = viewerUrlProblem("http://127.0.0.1:6080/vnc.html");
    assert.match(problem, /standalone/);
    assert.match(problem, /websockify/);
    assert.match(problem, /browser/);
  });

  it("flags vnc.html with a query string too", () => {
    assert.match(viewerUrlProblem("https://vm.example.test/vnc.html?autoconnect=1"), /standalone/);
  });

  it("asks for a URL when the box is empty", () => {
    assert.match(viewerUrlProblem("   "), /Enter/);
  });
});

describe("defaultViewerUrl", () => {
  it("defaults to the local test session", () => {
    assert.equal(defaultViewerUrl({}), DEFAULT_VIEWER_URL);
    // No env at all (plain Node) must also fall back cleanly.
    assert.equal(defaultViewerUrl(), DEFAULT_VIEWER_URL);
  });

  it("can be pointed at the tunnel candidate without a code change", () => {
    assert.equal(defaultViewerUrl({ VITE_VNC_WS_URL: TUNNEL_VIEWER_URL }), TUNNEL_VIEWER_URL);
    assert.equal(normaliseViewerUrl(TUNNEL_VIEWER_URL), TUNNEL_VIEWER_URL);
  });

  it("ignores a broken override rather than half-using it", () => {
    assert.equal(defaultViewerUrl({ VITE_VNC_WS_URL: "not a url" }), DEFAULT_VIEWER_URL);
  });
});

describe("formatElapsed", () => {
  it("stays in tenths under ten seconds", () => {
    assert.equal(formatElapsed(400), "0.4s");
    assert.equal(formatElapsed(4_200), "4.2s");
  });

  it("rounds to whole seconds, then minutes", () => {
    assert.equal(formatElapsed(30_400), "30s");
    assert.equal(formatElapsed(63_000), "1m 03s");
  });

  it("has nothing to say without a duration", () => {
    assert.equal(formatElapsed(null), null);
    assert.equal(formatElapsed(-1), null);
    assert.equal(formatElapsed(Number.NaN), null);
  });
});

describe("isDefaultViewerUrl", () => {
  it("recognises the built-in endpoint however it is spelled", () => {
    assert.equal(isDefaultViewerUrl(DEFAULT_VIEWER_URL), true);
    assert.equal(isDefaultViewerUrl("127.0.0.1:6080/websockify"), true);
    assert.equal(isDefaultViewerUrl("http://127.0.0.1:6080/websockify"), true);
  });

  it("treats anything else as a custom endpoint", () => {
    // The common stale case: a remembered port nothing is listening on.
    assert.equal(isDefaultViewerUrl("ws://127.0.0.1:6099/websockify"), false);
    assert.equal(isDefaultViewerUrl(TUNNEL_VIEWER_URL), false);
    assert.equal(isDefaultViewerUrl("not a url"), false);
  });
});

describe("computer transport seam", () => {
  it("defaults to managed sessions; only the explicit flag enables RFB", () => {
    assert.equal(computerTransport({}), "managed");
    assert.equal(computerTransport(undefined), "managed");
    assert.equal(computerTransport({ VITE_COMPUTER_TRANSPORT: "bogus" }), "managed");
    assert.equal(computerTransport({ VITE_COMPUTER_TRANSPORT: "rfb-stub" }), "rfb-stub");
    assert.equal(isRfbTransport({}), false);
    assert.equal(isRfbTransport({ VITE_COMPUTER_TRANSPORT: "rfb-stub" }), true);
  });

  it("routes a managed session URL without touching RFB", () => {
    const session = { id: "sess_1", url: "https://boat.example.test/session/abc123" };
    assert.deepEqual(resolveViewerTarget(session, {}), {
      kind: "managed-session",
      url: "https://boat.example.test/session/abc123",
      sessionId: "sess_1",
    });
    // ...even on the dev transport: managed URLs never become websockify dials.
    assert.equal(resolveViewerTarget(session, { VITE_COMPUTER_TRANSPORT: "rfb-stub" }).kind, "managed-session");
  });

  it("refuses raw viewer endpoints unless the dev transport is explicit", () => {
    const session = { id: "sess_1", url: "ws://127.0.0.1:6080/websockify" };
    assert.equal(resolveViewerTarget(session, {}), null);
    assert.deepEqual(
      resolveViewerTarget(session, { VITE_COMPUTER_TRANSPORT: "rfb-stub" }),
      { kind: "rfb-websockify", url: "ws://127.0.0.1:6080/websockify", sessionId: "sess_1" },
    );
  });

  it("produces a second anchor proposal for a second session id", () => {
    const first = resolveViewerTarget(
      { id: "sess_1", url: "ws://127.0.0.1:6080/websockify" },
      { VITE_COMPUTER_TRANSPORT: "rfb-stub" },
    );
    const second = resolveViewerTarget(
      { id: "sess_2", url: "ws://127.0.0.1:6081/websockify" },
      { VITE_COMPUTER_TRANSPORT: "rfb-stub" },
    );
    assert.notEqual(first.sessionId, second.sessionId);
    assert.notEqual(first.url, second.url);
  });

  it("rejects non-URL sessions and non-session URLs", () => {
    const env = { VITE_COMPUTER_TRANSPORT: "rfb-stub" };
    assert.equal(resolveViewerTarget(null, env), null);
    assert.equal(resolveViewerTarget({ id: "", url: "ws://127.0.0.1:6080/websockify" }, env), null);
    assert.equal(resolveViewerTarget({ id: "s", url: null }, env), null);
    assert.equal(resolveViewerTarget({ id: "s", url: "not a url" }, env), null);
    assert.equal(resolveViewerTarget({ id: "s", url: "https://example.test/plain" }, env), null);
    assert.equal(resolveViewerTarget({ id: "s", url: "ftp://example.test/x" }, env), null);
  });
});

describe("viewer url hygiene", () => {
  it("flags cleartext ws:// to a non-loopback host", () => {
    assert.equal(isCleartextRemote("ws://127.0.0.1:6080/websockify"), false);
    assert.equal(isCleartextRemote("ws://localhost:6080/x"), false);
    assert.equal(isCleartextRemote("wss://example.test/x"), false);
    assert.equal(isCleartextRemote("ws://203.0.113.7:6080/websockify"), true);
    assert.equal(isCleartextRemote("ws://vnc.example.test/x"), true);
    const problem = viewerUrlProblem("ws://203.0.113.7:6080/websockify");
    assert.match(problem, /cleartext/);
    assert.match(problem, /wss/);
  });

  it("treats every loopback spelling as local, including edge hosts", () => {
    // 0.0.0.0 (dial-this-machine), IPv4-mapped IPv6, uppercase scheme, and
    // userinfo URLs must not read as remote — but a trailing-dot remote name
    // still is.
    for (const url of [
      "ws://0.0.0.0:6080/websockify",
      "ws://[::ffff:127.0.0.1]:6080/x",
      "WS://127.0.0.1:6080/websockify",
      "ws://user:pass@127.0.0.1:6080/x",
      "ws://localhost.:6080/x",
      "ws://[::1]:6080/x",
    ]) {
      assert.equal(isCleartextRemote(url), false, url);
    }
    assert.equal(isCleartextRemote("ws://example.test./x"), true);
    // viewerUrlProblem agrees through the same path the dial entry uses.
    assert.equal(viewerUrlProblem("WS://127.0.0.1:6080/websockify"), null);
    assert.match(viewerUrlProblem("ws://user:pass@203.0.113.7:6080/x"), /cleartext/);
  });

  it("never persists query or fragment", () => {
    const store = new Map();
    globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)) };
    rememberViewerUrl("wss://vm.example.test/view?token=secret#frag");
    assert.equal(store.get(VIEWER_URL_STORAGE_KEY), "wss://vm.example.test/view");
    assert.equal(stripViewerSecrets("wss://vm.example.test/view?token=secret#frag"), "wss://vm.example.test/view");
  });

  it("redacts tokens in failure text instead of echoing them", () => {
    const redacted = redactViewerUrl("wss://vm.example.test/view?token=secret");
    assert.doesNotMatch(redacted, /secret/);
    assert.match(redacted, /credentials hidden/);
    assert.equal(redacted.startsWith("wss://vm.example.test/view"), true);
    const detail = disconnectDetail({
      url: "ws://127.0.0.1:6099/websockify?token=secret",
      everConnected: false,
      exhausted: true,
    });
    assert.doesNotMatch(detail, /secret/);
    assert.match(detail, /6099/);
  });

  it("ignores a mid-path htm directory but still flags vnc.html", () => {
    assert.equal(viewerUrlProblem("https://example.test/htmldocs/vnc"), null);
    assert.match(viewerUrlProblem("https://vm.example.test/vnc.html?autoconnect=1"), /standalone/);
    assert.match(viewerUrlProblem("https://vm.example.test/VNC.HTML"), /standalone/);
  });
});

describe("disconnectDetail", () => {
  const url = "ws://127.0.0.1:6099/websockify";

  it("names the address when the socket never opened", () => {
    // The whole point: a stale remembered URL is not a dropped session, and
    // "the connection dropped" sends people looking at tunnels instead of the
    // port they typed.
    const detail = disconnectDetail({ url, everConnected: false, exhausted: false });
    assert.match(detail, /6099/);
    assert.match(detail, /nothing is listening/);
    assert.doesNotMatch(detail, /dropped/);
  });

  it("stops retrying with something actionable", () => {
    const detail = disconnectDetail({ url, everConnected: false, exhausted: true });
    assert.match(detail, /Nothing is answering/);
    assert.match(detail, /default address/);
  });

  it("keeps the dropped wording for a session that was up", () => {
    assert.match(
      disconnectDetail({ url, everConnected: true, exhausted: false }),
      /connection dropped/,
    );
    assert.match(
      disconnectDetail({ url, everConnected: true, exhausted: true }),
      /several attempts/,
    );
  });
});
