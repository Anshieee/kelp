import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";

// Guards the vendored subset of the motion kit. The kit ships ten states in
// four formats; we deliberately copied only the webview's two (.webp loop and
// .svg still) plus the manifest and the component, leaving the 8 MB of native
// sprite atlases and the gallery behind. A half-copied folder would fail
// silently at runtime, which is what this catches.

const DIR = new URL("../src/public/assets/kelp-pet/", import.meta.url);
const manifest = JSON.parse(readFileSync(new URL("manifest.json", DIR), "utf8"));
const component = readFileSync(new URL("kelp-pet.js", DIR), "utf8");

const states = Object.keys(manifest.states);

test("the manifest carries the kit's ten states", () => {
  assert.deepEqual([...states].sort(), [
    "curious",
    "done",
    "excited",
    "happy",
    "idle",
    "offline",
    "surprised",
    "thinking",
    "waiting",
    "working",
  ]);
  assert.equal(manifest.defaultState, "idle");
  assert.equal(manifest.durationMs, 2560);
});

test("every state has the loop and the still the webview uses", () => {
  for (const state of states) {
    assert.ok(existsSync(new URL(`${state}.webp`, DIR)), `${state}.webp missing`);
    assert.ok(existsSync(new URL(`${state}.svg`, DIR)), `${state}.svg missing`);
    assert.equal(manifest.states[state].animation, `${state}.webp`);
    assert.equal(manifest.states[state].poster, `${state}.svg`);
  }
});

test("the native sprite atlases and the gallery were left out", () => {
  for (const state of states) {
    assert.ok(!existsSync(new URL(`${state}.png`, DIR)), `${state}.png should not be vendored`);
  }
  assert.ok(!existsSync(new URL("preview.gif", DIR)), "preview.gif should not be vendored");
  assert.ok(!existsSync(new URL("preview.html", DIR)), "preview.html should not be vendored");
});

test("the component knows exactly the manifest's states", () => {
  const literal = /const STATES = Object\.freeze\(\[([^\]]*)\]\)/.exec(component);
  assert.ok(literal, "STATES list not found in the component");
  const declared = literal[1]
    .split(",")
    .map((part) => part.trim().replace(/^['"]|['"]$/g, ""))
    .filter(Boolean);
  assert.deepEqual([...declared].sort(), [...states].sort());
});
