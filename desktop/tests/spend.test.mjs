import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { parseSpendGauges } from "../src/api.js";

const MONTH = `
# HELP spend_used_usd Counted spend inside the spend window.
spend_used_usd{window="month"} 8.5
spend_cap_usd{window="month"} 10
spend_cap_ratio{window="month"} 0.85
spend_used_usd{window="day"} 1.25
spend_cap_usd{window="day"} 2
`;

describe("parseSpendGauges", () => {
  it("mirrors the server gauges, month window preferred", () => {
    const s = parseSpendGauges(MONTH);
    assert.equal(s.window, "month");
    assert.equal(s.usedUsd, 8.5);
    assert.equal(s.capUsd, 10);
    assert.ok(Math.abs(s.ratio - 0.85) < 1e-9);
    assert.equal(s.warned, true);
    assert.equal(s.capped, false);
  });

  it("warns at 80 percent, plain capped flag at 100 percent", () => {
    const warn = parseSpendGauges('spend_used_usd{window="month"} 8\nspend_cap_usd{window="month"} 10\n');
    assert.equal(warn.warned, true);
    assert.equal(warn.capped, false);
    const full = parseSpendGauges('spend_used_usd{window="month"} 10\nspend_cap_usd{window="month"} 10\n');
    assert.equal(full.capped, true);
    const low = parseSpendGauges('spend_used_usd{window="month"} 1\nspend_cap_usd{window="month"} 10\n');
    assert.equal(low.warned, false);
    assert.equal(low.capped, false);
  });

  it("falls back to day window and tolerates missing caps", () => {
    const s = parseSpendGauges('spend_used_usd{window="day"} 0.5\n');
    assert.equal(s.window, "day");
    assert.equal(s.capUsd, null);
    assert.equal(s.warned, false);
  });

  it("returns null when the server reports no spend", () => {
    assert.equal(parseSpendGauges("# only comments\n"), null);
    assert.equal(parseSpendGauges(""), null);
  });
});
