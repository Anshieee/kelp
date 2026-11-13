import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// Static guards for the packaged posture. A packaged build must never point
// at cleartext loopback: both the webview config and the Rust bridge fail
// closed to an https placeholder outside debug builds, and the CSP never
// carries an unscoped ws: source. Exact-URL and placeholder-host checks live
// here (cheap, no Tauri runtime needed); behaviour checks stay in the
// helper suites that import the modules.

const root = new URL("..", import.meta.url);
const read = (rel) => readFileSync(new URL(rel, root), "utf8");

const DEFAULT_SERVER_URL = "https://kelp.example.com";
const DEFAULT_CSP =
  "default-src 'self'; connect-src 'self' http://127.0.0.1:* ws://127.0.0.1:* wss: https:; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'";

describe("production guards", () => {
  it("webview config defaults to the https placeholder, with a per-build override", () => {
    const config = read("src/config.js");
    assert.match(config, /const DEFAULT_SERVER_URL = "https:\/\/kelp\.example\.com";/);
    assert.match(config, /VITE_KELP_SERVER_URL/);
    assert.doesNotMatch(config, /http:\/\/127\.0\.0\.1/);
  });

  it("Rust bridge defaults to the https placeholder outside debug builds", () => {
    const bridge = read("src-tauri/src/bridge.rs");
    const release = /#\[cfg\(not\(debug_assertions\)\)\][\r\n]+const DEFAULT_SERVER: &str = "https:\/\/kelp\.example\.com";/;
    const debug = /#\[cfg\(debug_assertions\)\][\r\n]+const DEFAULT_SERVER: &str = "http:\/\/127\.0\.0\.1:8000";/;
    assert.match(bridge, release);
    assert.match(bridge, debug);
  });

  it("Tauri CSP pins the loopback allowance and carries no unscoped ws source", () => {
    const conf = JSON.parse(read("src-tauri/tauri.conf.json"));
    assert.equal(conf.app.security.csp, DEFAULT_CSP);
    // A bare `ws:` source (or `ws:*`) would open any cleartext socket; the
    // only ws entry allowed is the scoped `ws://127.0.0.1:*` loopback one.
    // `[;\s]` after the colon excludes `ws://…` from matching.
    assert.doesNotMatch(conf.app.security.csp, /(^|\s)ws:[;\s]/);
    assert.doesNotMatch(conf.app.security.csp, /ws:\*/);
    assert.match(conf.app.security.csp, /ws:\/\/127\.0\.0\.1:\*/);
  });
});
