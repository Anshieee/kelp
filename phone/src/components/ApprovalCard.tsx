/*
 * One pending approval (CHAT_SPEC §7). Approve/Deny are always visible, never
 * in a menu. The countdown is cosmetic and stops at expires_at; the server is
 * authoritative and rejects late decisions.
 */
import { useEffect, useState } from "react";
import type { Approval } from "../chatState";

interface ApprovalCardProps {
  approval: Approval;
  onDecide: (approvalId: string, decision: "approve" | "deny") => Promise<void>;
}

export default function ApprovalCard({ approval, onDecide }: ApprovalCardProps) {
  const [busy, setBusy] = useState<"approve" | "deny" | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const expiresAt = Date.parse(approval.expires_at);
  const expired = !Number.isNaN(expiresAt) && now >= expiresAt;
  const secondsLeft = Number.isNaN(expiresAt) ? null : Math.max(0, Math.floor((expiresAt - now) / 1000));

  const decide = async (decision: "approve" | "deny") => {
    if (busy || expired) return;
    setBusy(decision);
    await onDecide(approval.id, decision);
    setBusy(null);
  };

  return (
    <article className="approval-card" aria-label={`Approval: ${approval.title ?? approval.tool}`}>
      <header className="approval-head">
        <span className="approval-status">Waiting on you</span>
        {secondsLeft !== null && !expired && (
          <span className="approval-timer">expires in {formatCountdown(secondsLeft)}</span>
        )}
        {expired && <span className="approval-timer">expired</span>}
      </header>
      <h3>{approval.title ?? approval.tool}</h3>
      {approval.summary && <p className="approval-summary">{approval.summary}</p>}
      {approval.rationale && <p className="approval-rationale">{approval.rationale}</p>}
      {approval.effects.length > 0 && (
        <p className="approval-effects">
          {approval.effects.map((effect) => (
            <span key={effect} className="effect-chip">
              {effect}
            </span>
          ))}
        </p>
      )}
      <div className="approval-actions">
        <button
          type="button"
          className="btn btn-approve"
          disabled={busy !== null || expired}
          onClick={() => void decide("approve")}
        >
          {busy === "approve" ? "Approving…" : "Approve"}
        </button>
        <button
          type="button"
          className="btn btn-deny"
          disabled={busy !== null || expired}
          onClick={() => void decide("deny")}
        >
          {busy === "deny" ? "Denying…" : "Deny"}
        </button>
      </div>
      <p className="approval-footnote">Nothing has run yet. This approval works once.</p>
    </article>
  );
}

function formatCountdown(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}
