import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// The bearer contract: React state only, cleared on both submit paths, one
// wire (bridge_sign_in), cookie jar after that. Nothing reaches localStorage,
// disk, or logs. Static checks — the dialog is a React component with no DOM
// test harness in this repo, so the suite pins the source text that enforces
// the contract instead of rendering it.

const root = new URL("..", import.meta.url);
const read = (rel) => readFileSync(new URL(rel, root), "utf8");

describe("bearer persistence", () => {
  it("App.tsx clears the bearer on both submit paths", () => {
    const app = read("src/App.tsx");
    // Enter-key path and Sign-in button path.
    const clears = (app.match(/setBearer\(""\);/g) ?? []).length;
    assert.ok(clears >= 2, `expected 2+ bearer clears, found ${clears}`);
    assert.doesNotMatch(app, /localStorage/);
    assert.doesNotMatch(app, /sessionStorage/);
  });

  it("the bridge exposes sign-in but no way to read the bearer back", () => {
    const bridge = read("src/bridge.ts");
    assert.match(bridge, /bridge_sign_in/);
    assert.doesNotMatch(bridge, /getBearer|get_bearer|readBearer|loadBearer/i);
    assert.doesNotMatch(bridge, /localStorage|sessionStorage/);
  });

  it("the Rust bridge uses the bearer once and never logs it", () => {
    const bridge = read("src-tauri/src/bridge.rs");
    const uses = (bridge.match(/bearer/g) ?? []).length;
    assert.ok(uses >= 2, "bridge_sign_in should name the bearer");
    // The only Authorization header in the file is the sign-in exchange.
    const authHeaders = (bridge.match(/Authorization/g) ?? []).length;
    assert.equal(authHeaders, 1);
    assert.doesNotMatch(bridge, /log.*bearer|bearer.*log|eprintln|println!|dbg!/i);
  });

  it("no routine request helper attaches the bearer", () => {
    const bridge = read("src/bridge.ts");
    // bridge.api carries method/path/body only — no auth param, no header.
    assert.doesNotMatch(bridge, /Authorization/);
    assert.doesNotMatch(bridge, /Bearer/);
  });
});
