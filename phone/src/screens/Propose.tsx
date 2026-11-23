/*
 * Propose screen: amount (major units, parsed to integer cents), currency
 * picker, and the payee line (description). Client-side cap pre-check runs
 * against live limits when available; the server re-checks authoritatively.
 * On success the proposal is handed to the PaymentSheet.
 */
import { useEffect, useRef, useState } from "react";
import { ApiError } from "../api";
import PaymentSheet from "../components/PaymentSheet";
import {
  formatMoney,
  getLimits,
  mapWalletError,
  parseAmountToCents,
  proposalFingerprint,
  proposePayment,
  resolveIdempotencyKey,
  toSheetProposal,
  WALLET_CURRENCIES,
  type IdempotencySlot,
  type SheetProposal,
  type WalletLimits,
  type WalletProposal,
} from "../wallet";

interface ProposeProps {
  onBack: () => void;
}

export default function Propose({ onBack }: ProposeProps) {
  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState<string>("USD");
  const [description, setDescription] = useState("");
  const [limits, setLimits] = useState<WalletLimits | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [showRetry, setShowRetry] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [proposal, setProposal] = useState<WalletProposal | null>(null);
  // Idempotency slot for the in-flight logical proposal; survives retries
  // and re-renders, cleared on success so a stale form never reuses a key.
  const keySlot = useRef<IdempotencySlot | null>(null);

  useEffect(() => {
    let cancelled = false;
    getLimits(currency.toLowerCase())
      .then((data) => {
        if (!cancelled) setLimits(data.limits);
      })
      .catch(() => {
        // Pre-check is best-effort; the server enforces caps authoritatively.
        if (!cancelled) setLimits(null);
      });
    return () => {
      cancelled = true;
    };
  }, [currency]);

  const submit = async () => {
    if (submitting) return;
    setFormError(null);
    setSubmitError(null);
    setShowRetry(false);
    const parsed = parseAmountToCents(amount);
    if (!parsed.ok) {
      setFormError(parsed.error);
      return;
    }
    if (parsed.cents > 0 && limits) {
      if (parsed.cents > limits.per_purchase_cents) {
        setFormError(
          `That's above your per-purchase limit of ${formatMoney(limits.per_purchase_cents, limits.currency)}.`,
        );
        return;
      }
      if (parsed.cents > limits.remaining_cents) {
        setFormError("That would go over your monthly limit.");
        return;
      }
    }
    setSubmitting(true);
    // One key per logical proposal: the first submit mints it, retries of
    // the same amount/currency/payee reuse it (a lost response replays
    // against the same pending order instead of reserving cap twice), and
    // any field edit mints a fresh key for what is now a different payment.
    // The slot clears on success; a stale form can never reuse a spent key.
    const fingerprint = proposalFingerprint({
      amountCents: parsed.cents,
      currency: currency.toLowerCase(),
      description: description.trim(),
    });
    const resolved = resolveIdempotencyKey(keySlot.current, fingerprint);
    keySlot.current = { key: resolved.key, fingerprint };
    try {
      const data = await proposePayment(
        { amountCents: parsed.cents, currency: currency.toLowerCase(), description: description.trim() },
        resolved.key,
      );
      keySlot.current = null;
      setProposal(data.proposal);
    } catch (cause) {
      const view = mapWalletError(cause);
      if (view.notConfigured) {
        setSubmitError(view.message);
        setShowRetry(false);
      } else {
        setSubmitError(cause instanceof ApiError && cause.message ? cause.message : view.message);
        setShowRetry(view.showRetry);
      }
    } finally {
      setSubmitting(false);
    }
  };

  if (proposal) {
    const sheetProposal: SheetProposal = toSheetProposal(proposal);
    return (
      <>
        <ProposeForm
          amount={amount}
          currency={currency}
          description={description}
          limits={limits}
          formError={formError}
          submitError={submitError}
          showRetry={showRetry}
          submitting={submitting}
          onAmount={setAmount}
          onCurrency={setCurrency}
          onDescription={setDescription}
          onSubmit={() => void submit()}
          onBack={onBack}
        />
        <PaymentSheet
          proposal={sheetProposal}
          initialLimits={limits}
          onClose={onBack}
        />
      </>
    );
  }

  return (
    <ProposeForm
      amount={amount}
      currency={currency}
      description={description}
      limits={limits}
      formError={formError}
      submitError={submitError}
      showRetry={showRetry}
      submitting={submitting}
      onAmount={setAmount}
      onCurrency={setCurrency}
      onDescription={setDescription}
      onSubmit={() => void submit()}
      onBack={onBack}
    />
  );
}

interface FormProps {
  amount: string;
  currency: string;
  description: string;
  limits: WalletLimits | null;
  formError: string | null;
  submitError: string | null;
  showRetry: boolean;
  submitting: boolean;
  onAmount: (value: string) => void;
  onCurrency: (value: string) => void;
  onDescription: (value: string) => void;
  onSubmit: () => void;
  onBack: () => void;
}

function ProposeForm(props: FormProps) {
  return (
    <main className="screen settings-screen">
      <header className="top-bar">
        <button type="button" className="icon-btn" aria-label="Back to wallet" onClick={props.onBack}>
          <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
            <path d="M15.5 4.5 8 12l7.5 7.5" stroke="currentColor" strokeWidth="2" fill="none" strokeLinecap="round" />
          </svg>
        </button>
        <h1>New payment</h1>
      </header>

      <section className="settings-section">
        <div className="field">
          <label htmlFor="pay-amount">Amount</label>
          <input
            id="pay-amount"
            type="text"
            inputMode="decimal"
            autoComplete="off"
            placeholder="12.50"
            value={props.amount}
            onChange={(event) => props.onAmount(event.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="pay-currency">Currency</label>
          <select id="pay-currency" value={props.currency} onChange={(event) => props.onCurrency(event.target.value)}>
            {WALLET_CURRENCIES.map((code) => (
              <option key={code} value={code}>
                {code}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="pay-description">Payee</label>
          <input
            id="pay-description"
            type="text"
            autoComplete="off"
            placeholder="Guest & Ribbon order"
            value={props.description}
            onChange={(event) => props.onDescription(event.target.value)}
          />
        </div>
        {props.limits && (
          <p className="settings-note">
            Per purchase {formatMoney(props.limits.per_purchase_cents, props.limits.currency)} ·{" "}
            {formatMoney(props.limits.remaining_cents, props.limits.currency)} monthly remaining.
          </p>
        )}
        {props.formError && (
          <p className="form-error" role="alert">
            {props.formError}
          </p>
        )}
        {props.submitError && (
          <p className="form-error" role="alert">
            {props.submitError}
          </p>
        )}
        <button type="button" className="btn btn-pay btn-block" disabled={props.submitting} onClick={props.onSubmit}>
          {props.submitting ? "Proposing…" : props.showRetry ? "Try again" : "Review payment"}
        </button>
      </section>
    </main>
  );
}
