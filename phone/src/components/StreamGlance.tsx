/*
 * Server glance: a small card parsed from GET /api/metrics (Prometheus text
 * format). Metric names are read from chat/metrics.py — spend gauges are
 * `spend_used_usd{window="day"|"month"}` etc., not invented names.
 */
import { useEffect, useState } from "react";
import { metricsText } from "../api";

interface Glance {
  spendDay: number | null;
  capDay: number | null;
  ratioDay: number | null;
  costTotal: number | null;
  turns: number | null;
}

export default function StreamGlance() {
  const [glance, setGlance] = useState<Glance | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void metricsText()
      .then((text) => {
        if (!cancelled) setGlance(parsePrometheus(text));
      })
      .catch(() => {
        if (!cancelled) setError("Metrics are unavailable.");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) return <p className="settings-note">{error}</p>;
  if (!glance) return <p className="empty-note">Loading…</p>;

  return (
    <dl className="glance-card">
      {glance.spendDay !== null && (
        <div className="glance-row">
          <dt>Today's spend</dt>
          <dd>
            {formatUsd(glance.spendDay)}
            {glance.capDay !== null && (
              <span className="glance-sub"> of {formatUsd(glance.capDay)} cap</span>
            )}
            {glance.ratioDay !== null && (
              <span className="glance-sub"> ({Math.round(glance.ratioDay * 100)}%)</span>
            )}
          </dd>
        </div>
      )}
      {glance.costTotal !== null && (
        <div className="glance-row">
          <dt>All-time cost</dt>
          <dd>{formatUsd(glance.costTotal)}</dd>
        </div>
      )}
      {glance.turns !== null && (
        <div className="glance-row">
          <dt>Turns</dt>
          <dd>{glance.turns}</dd>
        </div>
      )}
      {glance.spendDay === null && glance.costTotal === null && glance.turns === null && (
        <div className="glance-row">
          <dt>No spend data yet</dt>
          <dd>—</dd>
        </div>
      )}
    </dl>
  );
}

/**
 * Minimal Prometheus text parser: enough for the glance card. Reads
 * `name{labels} value` sample lines and the `# TYPE` hints are ignored.
 */
export function parsePrometheus(text: string): Glance {
  const samples = new Map<string, number>();
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const spaceIndex = trimmed.lastIndexOf(" ");
    if (spaceIndex <= 0) continue;
    const name = trimmed.slice(0, spaceIndex).trim();
    const value = Number(trimmed.slice(spaceIndex + 1).trim());
    if (!Number.isFinite(value)) continue;
    samples.set(name, value);
  }

  const gauge = (base: string, window: string): number | null => {
    const labeled = samples.get(`${base}{window="${window}"}`);
    if (labeled !== undefined) return labeled;
    return samples.get(base) ?? null;
  };

  let turns: number | null = null;
  for (const [name, value] of samples) {
    if (name === "turns_total" || name.startsWith("turns_total{")) {
      turns = (turns ?? 0) + value;
    }
  }

  return {
    spendDay: gauge("spend_used_usd", "day"),
    capDay: gauge("spend_cap_usd", "day"),
    ratioDay: gauge("spend_cap_ratio", "day"),
    costTotal: samples.get("cost_usd_total") ?? null,
    turns,
  };
}

function formatUsd(value: number): string {
  if (Math.abs(value) >= 100) return `$${value.toFixed(0)}`;
  return `$${value.toFixed(2)}`;
}
