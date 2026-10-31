import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// The SVG pet must stay faithful to the website's orb-face element:
// one disc, two capsule eyes, one highlight, four state tints in CSS.

const svg = readFileSync(new URL("../src/pet.svg", import.meta.url), "utf8");
const css = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
const html = readFileSync(new URL("../src/pet.html", import.meta.url), "utf8");

test("pet.svg has one disc, two eyes, one highlight", () => {
  assert.equal((svg.match(/<circle/g) || []).length, 1);
  assert.equal((svg.match(/<rect/g) || []).length, 2);
  assert.equal((svg.match(/<ellipse/g) || []).length, 1);
  assert.ok(svg.includes('viewBox="0 0 148 148"'));
});

test("all four states tint the disc", () => {
  for (const s of ["working", "waiting", "done", "idle"]) {
    assert.ok(css.includes(`[data-pet-state="${s}"] .pet-disc`), `missing ${s}`);
  }
});

test("pet.html inlines the svg, not the canvas", () => {
  assert.ok(html.includes('class="pet-svg"'), "inline svg present");
  assert.ok(!html.includes("e-orb-canvas"), "canvas removed");
  assert.ok(html.includes('data-pet-state="idle"'), "idle default set");
});
