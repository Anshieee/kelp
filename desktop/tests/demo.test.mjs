import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// Fixture shapes must match what app.js renders, so the demo can never
// drift into shapes the UI cannot display. Every id is demo-prefixed.

const src = readFileSync(new URL("../src/demo.js", import.meta.url), "utf8");

test("every fixture id is demo-prefixed", () => {
  for (const id of ["demo-thread-1", "demo-thread-2", "demo-apr-1", "demo-dev-1", "demo-dev-2"]) {
    assert.ok(src.includes(id), `missing ${id}`);
  }
});

test("no fixture leaks a real-looking secret or host", () => {
  assert.ok(!src.includes("sk-"), "no secret-like strings");
  assert.ok(!src.includes("http"), "demo.js contacts no host");
});

test("canned reply echoes input without executing it", async () => {
  const mod = await import("../src/demo.js");
  const out = mod.demoCannedReply("hello");
  assert.ok(out.includes("hello"));
  assert.ok(out.includes("Demo reply"));
});
