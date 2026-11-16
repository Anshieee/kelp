import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// The inline SVG is no longer the orb: the kit's animated states are. It stays
// as the last-resort face for when the kit's files cannot be loaded, so its
// shape and the four turn-state tints are still worth pinning.

const svg = readFileSync(new URL("../src/pet.svg", import.meta.url), "utf8");
const css = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
const html = readFileSync(new URL("../src/pet.html", import.meta.url), "utf8");

test("pet.svg has one disc, two eyes, one highlight", () => {
  assert.equal((svg.match(/<circle/g) || []).length, 1);
  assert.equal((svg.match(/<rect/g) || []).length, 2);
  assert.equal((svg.match(/<ellipse/g) || []).length, 1);
  assert.ok(svg.includes('viewBox="0 0 148 148"'));
});

test("all four turn states still tint the fallback disc", () => {
  for (const s of ["working", "waiting", "done", "idle"]) {
    assert.ok(css.includes(`[data-pet-state="${s}"] .pet-disc`), `missing ${s}`);
  }
});

test("pet.html drives the kit component and keeps the svg as its fallback", () => {
  assert.ok(html.includes("<kelp-pet"), "the animated component is the default");
  assert.ok(html.includes('class="kelp-pet"'), "component is sized by a class");
  assert.ok(html.includes("assets/kelp-pet/kelp-pet.js"), "component module loaded");
  assert.ok(html.includes('class="pet-fallback"'), "the svg is the fallback");
  assert.ok(html.includes('class="pet-svg"'), "fallback svg present");
  assert.ok(!html.includes("e-orb-canvas"), "canvas path retired");
  assert.ok(html.includes('data-pet-state="idle"'), "idle default set");
});

test("the fallback stays hidden until it is needed", () => {
  assert.ok(html.includes('class="pet-fallback" hidden'), "hidden in the markup");
  assert.ok(css.includes("[data-pet-fallback] .kelp-pet"), "component yields to it");
});
