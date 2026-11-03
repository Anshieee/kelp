import { describe, expect, it } from "vitest";
import { parsePrometheus } from "../components/StreamGlance";

const SAMPLE = `# HELP spend_used_usd Counted spend inside the spend window.
# TYPE spend_used_usd gauge
spend_used_usd{window="day"} 0.42
spend_used_usd{window="month"} 3.14
# TYPE spend_cap_usd gauge
spend_cap_usd{window="day"} 10
# TYPE spend_cap_ratio gauge
spend_cap_ratio{window="day"} 0.042
# TYPE cost_usd_total counter
cost_usd_total 12.5
# TYPE turns_total counter
turns_total{outcome="stop"} 7
turns_total{outcome="error"} 2
# TYPE http_requests_total counter
http_requests_total{method="GET",route="/api/threads",status="200"} 99
`;

describe("prometheus glance parser", () => {
  it("reads the real Kelp metric names from chat/metrics.py", () => {
    const glance = parsePrometheus(SAMPLE);
    expect(glance.spendDay).toBe(0.42);
    expect(glance.capDay).toBe(10);
    expect(glance.ratioDay).toBe(0.042);
    expect(glance.costTotal).toBe(12.5);
    expect(glance.turns).toBe(9);
  });

  it("returns nulls for absent series", () => {
    const glance = parsePrometheus("# HELP nothing\n");
    expect(glance.spendDay).toBeNull();
    expect(glance.capDay).toBeNull();
    expect(glance.costTotal).toBeNull();
    expect(glance.turns).toBeNull();
  });

  it("ignores malformed lines", () => {
    const glance = parsePrometheus("garbage line\nspend_used_usd{window=\"day\"} notanumber\n");
    expect(glance.spendDay).toBeNull();
  });
});
