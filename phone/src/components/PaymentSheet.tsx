/*
 * PaymentSheet: full-screen payment review, adapted from the reference
 * (big amount, payee, expandable rows, saved-card row, Approve/Cancel).
 *
 * Every row comes from a real wallet field: rows with no backing data are
 * hidden, never invented. There is intentionally NO delivery address/date
 * row — Kelp has no such concept. Approve charges the selected saved card
 * exactly once (single-use proposal + idempotency key).
 */
import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  buildReceiptModel,
  buildSheetModel,
  decidePayment,
  defaultCard,
  formatCardLine,
  getCards,
  getLimits,
  mapWalletError,
  type SheetProposal,
  type WalletCard,
  type WalletLimits,
  type WalletProposal,
} from "../wallet";

interface PaymentSheetProps {
  proposal: SheetProposal;
  /** Preloaded cards (Wallet screen passes its snapshot); when omitted the sheet fetches them. */
  initialCards?: WalletCard[] | null;
  /** Preloaded limits; when omitted the sheet fetches them for the proposal currency. */
  initialLimits?: WalletLimits | null;
  onClose: () => void;
  onDecided?: (proposalId: string, decision: "approve" | "deny") => void;
}

type Phase =
  | { name: "review" }
  | { name: "working"; decision: "approve" | "deny" }
  | { name: "receipt"; denied: boolean }
  | {
      name: "error";
      message: string;
      showRetry: boolean;
      checkoutBound: boolean;
    };

export default function PaymentSheet({ proposal, initialCards, initialLimits, onClose, onDecided }: PaymentSheetProps) {
  const [cards, setCards] = useState<WalletCard[] | null>(initialCards ?? null);
  const [limits, setLimits] = useState<WalletLimits | null>(initialLimits ?? null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selectedCardId, setSelectedCardId] = useState<string | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [phase, setPhase] = useState<Phase>({ name: "review" });
  const [decided, setDecided] = useState<WalletProposal | null>(null);
  const [openRows, setOpenRows] = useState<Record<string, boolean>>({});

  useEffect(() => {
    let cancelled = false;
    if (initialCards === undefined) {
      getCards()
        .then((data) => {
          if (!cancelled) setCards(data.cards);
        })
        .catch((cause: unknown) => {
          if (!cancelled) setLoadError(mapWalletError(cause).message);
        });
    }
    if (initialLimits === undefined) {
      getLimits(proposal.currency)
        .then((data) => {
          if (!cancelled) setLimits(data.limits);
        })
        .catch(() => {
          // Limits are advisory for the caps row; a failure just hides it.
          if (!cancelled) setLimits(null);
        });
    }
    return () => {
      cancelled = true;
    };
  }, [initialCards, initialLimits, proposal.currency]);

  // Lock body scroll while the sheet is open.
  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, []);

  const model = useMemo(
    () => buildSheetModel(proposal, cards ?? [], limits, selectedCardId ?? defaultCard(cards ?? [])?.id ?? null),
    [proposal, cards, limits, selectedCardId],
  );

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const working = phase.name === "working";
  const errorPhase = phase.name === "error" ? phase : null;
  const receiptPhase = phase.name === "receipt" ? phase : null;
  // The receipt view renders ONLY from a real receipt model built from the
  // returned proposal. There is deliberately no fallback to the requested
  // amount: showing "Paid $X" without a receipt would fabricate a charge
  // (e.g. an approve replayed onto an already-denied order answers 200
  // with status "denied").
  const receipt = receiptPhase && !receiptPhase.denied && decided ? buildReceiptModel(decided) : null;
  const noSavedCard = cards !== null && cards.length === 0;

  const decide = async (decision: "approve" | "deny") => {
    if (working) return;
    if (decision === "approve" && noSavedCard) {
      setPhase({
        name: "error",
        message: "No saved card is available for this payment.",
        showRetry: false,
        checkoutBound: false,
      });
      return;
    }
    setPhase({ name: "working", decision });
    try {
      const data = await decidePayment(
        proposal.id,
        decision,
        decision === "approve" ? (model.selectedCardId ?? undefined) : undefined,
      );
      // Branch on the RETURNED proposal status, never on the requested
      // decision. Same-decision replays answer 200 with the stored status:
      // an approve replayed onto an already-denied order comes back
      // "denied", and paid replays come back "paid" *with* their receipt.
      // Rendering "Paid" from the request alone would fabricate a receipt.
      const returned = data.proposal;
      setDecided(returned);
      onDecided?.(proposal.id, decision);
      if (returned.status === "paid") {
        if (!buildReceiptModel(returned)) {
          setPhase({
            name: "error",
            message: "The charge was accepted but no receipt came back. Check the order before retrying.",
            showRetry: false,
            checkoutBound: false,
          });
          return;
        }
        setPhase({ name: "receipt", denied: false });
        return;
      }
      if (returned.status === "denied") {
        setPhase({ name: "receipt", denied: true });
        return;
      }
      setPhase({
        name: "error",
        message: `The order came back "${returned.status}". No charge was confirmed — check the order before retrying.`,
        showRetry: false,
        checkoutBound: false,
      });
    } catch (cause) {
      // No isAlreadyPaidReplay branch by design: paid replays answer 200
      // (handled above); every 409 (`already_decided`: blocked, failed,
      // checkout claims, conflicting answers) is a NOT-paid outcome and
      // belongs in the error phase. The checkout-bound message is preserved
      // by mapWalletError.
      const view = mapWalletError(cause);
      setPhase({
        name: "error",
        message: view.message,
        showRetry: view.showRetry && !view.checkoutBound,
        checkoutBound: view.checkoutBound,
      });
    }
  };

  const toggleRow = (key: string) => {
    setOpenRows((current) => ({ ...current, [key]: !current[key] }));
  };

  return (
    <div className="sheet-backdrop" role="dialog" aria-modal="true" aria-labelledby="pay-sheet-title">
      <div className="pay-sheet">
        <header className="pay-sheet-bar">
          <span className="pay-sheet-eyebrow">Review purchase</span>
          {!receiptPhase && (
            <button type="button" className="icon-btn" aria-label="Close payment sheet" onClick={onClose} disabled={working}>
              <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
                <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
              </svg>
            </button>
          )}
        </header>

        {receiptPhase ? (
          <div className="pay-receipt">
            {receiptPhase.denied || !receipt ? (
              <>
                <p className="pay-receipt-mark" aria-hidden="true">
                  ✕
                </p>
                <h2 id="pay-sheet-title">Payment cancelled</h2>
                <p className="pay-payee">No charge was made. The order was released.</p>
              </>
            ) : (
              <>
                <p className="pay-receipt-mark ok" aria-hidden="true">
                  ✓
                </p>
                <h2 id="pay-sheet-title">Paid {receipt.amountDisplay}</h2>
                <dl className="pay-receipt-rows">
                  {receipt.last4 && (
                    <div>
                      <dt>Card</dt>
                      <dd>···· {receipt.last4}</dd>
                    </div>
                  )}
                  <div>
                    <dt>Paid</dt>
                    <dd>{receipt.paidAt}</dd>
                  </div>
                  <div>
                    <dt>Order</dt>
                    <dd className="digest">{receipt.orderId}</dd>
                  </div>
                </dl>
              </>
            )}
            <button type="button" className="btn btn-pay btn-block" onClick={onClose}>
              Done
            </button>
          </div>
        ) : (
          <>
            <div className="pay-hero">
              {model.amountDisplay ? (
                <p className="pay-amount" id="pay-sheet-title">
                  {model.amountDisplay}
                </p>
              ) : (
                <h2 className="pay-amount-fallback" id="pay-sheet-title">
                  Review purchase
                </h2>
              )}
              <p className="pay-payee">{model.payeeLine}</p>
            </div>

            {loadError && (
              <p className="form-error pay-error" role="alert">
                {loadError}
              </p>
            )}

            <div className="pay-rows" role="list">
              {model.amountDetail && (
                <ExpandableRow
                  label="Amount"
                  value={model.amountDisplay ?? ""}
                  open={openRows.amount === true}
                  onToggle={() => toggleRow("amount")}
                >
                  {model.amountDetail}
                </ExpandableRow>
              )}
              <ExpandableRow
                label="Payee"
                value={model.payeeLine}
                open={openRows.payee === true}
                onToggle={() => toggleRow("payee")}
              >
                {model.payeeLine}
              </ExpandableRow>
              <div className="pay-row" role="listitem">
                <button
                  type="button"
                  className="pay-row-head"
                  aria-expanded={openRows.card === true}
                  onClick={() => toggleRow("card")}
                >
                  <span className="pay-row-label">Card</span>
                  <span className="pay-row-value">{model.cardLine ?? "No saved card"}</span>
                  <span className="pay-chevron" aria-hidden="true">
                    {openRows.card === true ? "▾" : "▸"}
                  </span>
                </button>
                {openRows.card === true && (
                  <div className="pay-row-body">
                    {(cards ?? []).length > 1 ? (
                      <>
                        <button
                          type="button"
                          className="pay-card-toggle"
                          aria-expanded={pickerOpen}
                          onClick={() => setPickerOpen((open) => !open)}
                        >
                          {pickerOpen ? "Hide cards ▾" : "Choose a card ▸"}
                        </button>
                        {pickerOpen && (
                          <ul className="pay-card-list">
                            {(cards ?? []).map((card) => (
                              <li key={card.id}>
                                <button
                                  type="button"
                                  className="pay-card-option"
                                  aria-pressed={model.selectedCardId === card.id}
                                  onClick={() => {
                                    setSelectedCardId(card.id);
                                    setPickerOpen(false);
                                  }}
                                >
                                  <span>
                                    {formatCardLine(card)}{" "}
                                    {card.is_default && <span className="pay-default">Default</span>}
                                  </span>
                                  {model.selectedCardId === card.id && <span aria-hidden="true">✓</span>}
                                </button>
                              </li>
                            ))}
                          </ul>
                        )}
                      </>
                    ) : (
                      <p className="pay-row-note">{model.cardLine ?? "No saved cards on this server yet."}</p>
                    )}
                  </div>
                )}
              </div>
              {model.capsLine && (
                <ExpandableRow
                  label="Caps impact"
                  value={model.capsLine}
                  open={openRows.caps === true}
                  onToggle={() => toggleRow("caps")}
                >
                  {model.capsLine}
                </ExpandableRow>
              )}
              <ExpandableRow
                label="Approval window"
                value="About 30 minutes"
                open={openRows.window === true}
                onToggle={() => toggleRow("window")}
              >
                Approve within ~30 minutes of proposing. After that the order expires and you would need to start a new
                payment.
              </ExpandableRow>
              <ExpandableRow
                label="Idempotency"
                value="Safe to retry"
                open={openRows.idem === true}
                onToggle={() => toggleRow("idem")}
              >
                Safe to retry — duplicate taps won&apos;t double-charge. Each attempt carries its own idempotency key.
              </ExpandableRow>
            </div>

            {errorPhase && (
              <p className="form-error pay-error" role="alert">
                {errorPhase.message}
              </p>
            )}

            <div className="pay-actions">
              {errorPhase?.checkoutBound ? (
                <button type="button" className="btn btn-block" onClick={onClose}>
                  Done
                </button>
              ) : (
                <>
                  <button type="button" className="btn btn-quiet" disabled={working} onClick={() => void decide("deny")}>
                    {working && phase.decision === "deny" ? "Cancelling…" : "Cancel"}
                  </button>
                  <button type="button" className="btn btn-pay" disabled={working} onClick={() => void decide("approve")}>
                    {working && phase.decision === "approve" ? "Approving…" : "Approve"}
                  </button>
                </>
              )}
            </div>
            {!errorPhase?.checkoutBound && (
              <p className="approval-footnote pay-footnote">Approve charges the selected card once.</p>
            )}
            {errorPhase?.showRetry && (
              <p className="approval-footnote pay-footnote">You can try again — nothing was charged.</p>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function ExpandableRow({
  label,
  value,
  open,
  onToggle,
  children,
}: {
  label: string;
  value: string;
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <div className="pay-row" role="listitem">
      <button type="button" className="pay-row-head" aria-expanded={open} onClick={onToggle}>
        <span className="pay-row-label">{label}</span>
        <span className="pay-row-value">{value}</span>
        <span className="pay-chevron" aria-hidden="true">
          {open ? "▾" : "▸"}
        </span>
      </button>
      {open && <div className="pay-row-body">{children}</div>}
    </div>
  );
}
