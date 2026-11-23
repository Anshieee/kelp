/*
 * Typed client for the Kelp wallet API (server-side Stripe config required).
 *
 * Same conventions as api.ts: same-origin relative `/api` paths, session
 * cookie via `credentials: "include"`, CSRF header on mutations (handled by
 * `apiRequest`). Every number and name shown in the UI comes from a real
 * wallet field — nothing here invents amounts, cards, or caps.
 *
 * Pure view helpers (formatMoney, parseAmountToCents, mapWalletError,
 * buildSheetModel, extractProposalRef) have no I/O so they are unit-tested
 * directly; the thin fetch wrappers are tested with a stubbed fetch.
 */
import { ApiError, apiRequest } from "./api";

/* --- wire shapes (exactly what the server sends) --- */

export interface WalletCard {
  id: string;
  brand: string;
  last4: string;
  exp_month: number;
  exp_year: number;
  is_default: boolean;
}

export interface WalletLimits {
  currency: string;
  per_purchase_cents: number;
  monthly_cents: number;
  spent_cents: number;
  reserved_cents: number;
  committed_cents: number;
  remaining_cents: number;
}

export interface WalletReceipt {
  order_id: string;
  payment_intent_id: string;
  amount_cents: number;
  currency: string;
  last4: string | null;
  paid_at: string;
}

export interface WalletProposal {
  id: string;
  amount_cents: number;
  currency: string;
  description: string;
  status: string;
  created_at: string;
  payment_method_id: string | null;
  decided_at: string | null;
  receipt?: WalletReceipt;
  reason?: string;
}

/** Server cap: amounts are positive integer cents, max 99999999. */
export const MAX_AMOUNT_CENTS = 99999999;

/** Default approval window the server enforces (~30 minutes, per-order TTLs vary). */
export const APPROVAL_WINDOW_LABEL = "Approve within ~30 minutes of proposing";

/** Small fixed currency list for the propose form. */
export const WALLET_CURRENCIES = ["USD", "EUR", "GBP", "INR"] as const;
export type WalletCurrency = (typeof WALLET_CURRENCIES)[number];

/* --- fetch wrappers --- */

interface CardsResponse {
  ok?: boolean;
  cards: WalletCard[];
}

interface LimitsResponse {
  ok?: boolean;
  limits: WalletLimits;
}

interface ProposeResponse {
  ok?: boolean;
  proposal: WalletProposal;
  idempotent?: boolean;
}

interface DecideResponse {
  ok?: boolean;
  proposal: WalletProposal;
  idempotent?: boolean;
}

export function getCards(): Promise<CardsResponse> {
  return apiRequest<CardsResponse>("GET", "/wallet/cards");
}

export function getLimits(currency = "usd"): Promise<LimitsResponse> {
  const params = new URLSearchParams({ currency });
  return apiRequest<LimitsResponse>("GET", `/wallet/limits?${params}`);
}

export interface ProposeInput {
  amountCents: number;
  currency: string;
  description: string;
}

/**
 * Propose a payment. The caller owns the idempotency key lifecycle: pass an
 * explicit key per *logical* proposal (see `resolveIdempotencyKey`) so a
 * retry-after-failure replays against the same pending order instead of
 * minting a second one that also holds cap. When no key is passed, a fresh
 * one is generated as a fallback — call this from the submit handler (not
 * during render) so a re-render never mints a new key.
 */
export function proposePayment(input: ProposeInput, idempotencyKey?: string): Promise<ProposeResponse> {
  return apiRequest<ProposeResponse>("POST", "/wallet/propose", {
    body: {
      amount_cents: input.amountCents,
      currency: input.currency,
      description: input.description,
      idempotency_key: idempotencyKey ?? newIdempotencyKey(),
    },
  });
}

export function decidePayment(
  proposalId: string,
  decision: "approve" | "deny",
  paymentMethodId?: string,
): Promise<DecideResponse> {
  const body: Record<string, string> = { proposal_id: proposalId, decision };
  if (paymentMethodId) body.payment_method_id = paymentMethodId;
  return apiRequest<DecideResponse>("POST", "/wallet/approve", { body });
}

/** One idempotency key fallback: uuid-shaped. Prefer `resolveIdempotencyKey` for form flows. */
export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

export interface IdempotencySlot {
  key: string;
  fingerprint: string;
}

/**
 * Stable fingerprint of a logical proposal: same amount, currency, and
 * payee means the same intent to pay. Normalized (lowercased currency,
 * trimmed description) so cosmetic edits do not fork the key.
 */
export function proposalFingerprint(input: { amountCents: number; currency: string; description: string }): string {
  return JSON.stringify([
    input.amountCents,
    input.currency.trim().toLowerCase(),
    input.description.trim(),
  ]);
}

/**
 * One key per logical proposal. The same fields reuse the in-flight key —
 * so a retry after a lost response replays against the same pending order
 * instead of reserving cap twice — while any field edit mints a fresh key
 * for what is now a different payment. No double-charge is possible either
 * way: each order needs its own single-use approve, but only the
 * same-fields-reuse-key rule keeps one intent to one reservation.
 */
export function resolveIdempotencyKey(
  previous: IdempotencySlot | null,
  fingerprint: string,
): { key: string; reused: boolean } {
  if (previous && previous.fingerprint === fingerprint) {
    return { key: previous.key, reused: true };
  }
  return { key: newIdempotencyKey(), reused: false };
}

/* --- money formatting: integer cents + ISO code -> "$66.30" style --- */

const LOCALE_CURRENCY: Record<string, string> = {
  USD: "en-US",
  EUR: "de-DE",
  GBP: "en-GB",
  INR: "en-IN",
};

function normalizeCode(currency: string): string {
  const code = currency.trim().toUpperCase();
  if (!code) return "USD";
  // Three-letter codes go straight to Intl (which validates them); anything
  // else falls through to the generic `CODE 0.00` rendering in the catch.
  return code;
}

/**
 * Format integer cents in the given ISO currency. USD/EUR/GBP/INR use
 * locale-correct symbols ($, €, £, ₹); anything else falls back to
 * `CODE 1,234.56` style. Never invents precision: input is integer cents.
 */
export function formatMoney(amountCents: number, currency: string): string {
  const code = normalizeCode(currency);
  try {
    return new Intl.NumberFormat(LOCALE_CURRENCY[code] ?? "en-US", {
      style: "currency",
      currency: code,
    }).format(amountCents / 100);
  } catch {
    return `${code} ${(amountCents / 100).toFixed(2)}`;
  }
}

/**
 * Parse a major-units amount string ("12.50") into integer cents.
 * Extra decimals are rounded DOWN (floor): "10.999" -> 1099 cents.
 * Rejects non-numeric, negative, and zero input.
 */
export function parseAmountToCents(raw: string): { ok: true; cents: number } | { ok: false; error: string } {
  const text = raw.trim().replace(/,/g, "");
  if (!text) return { ok: false, error: "Enter an amount." };
  const match = /^(\d+)(?:\.(\d*))?$/.exec(text);
  if (!match) return { ok: false, error: "Enter a valid amount, like 12.50." };
  const whole = match[1];
  const fraction = (match[2] ?? "").slice(0, 2).padEnd(2, "0");
  const cents = Number(whole) * 100 + Number(fraction);
  if (!Number.isSafeInteger(cents) || cents <= 0) {
    return { ok: false, error: "Enter an amount greater than zero." };
  }
  if (cents > MAX_AMOUNT_CENTS) return { ok: false, error: "That amount is too large." };
  return { ok: true, cents };
}

/* --- error mapping: envelope code -> user-facing copy --- */

export interface WalletErrorView {
  /** Copy to show inline. Never leaks stacks or raw payloads. */
  message: string;
  /** Whether to offer a Try-again affordance (only for user-fixable errors). */
  showRetry: boolean;
  /** True when the order belongs to hosted Checkout: never offer card approve. */
  checkoutBound: boolean;
  /** True when the wallet has no server-side Stripe config. */
  notConfigured: boolean;
}

const TRY_LATER = "Please try again later.";

function withTryLater(message: string): string {
  return /try again/i.test(message) ? message : `${message} ${TRY_LATER}`;
}

function isCheckoutMessage(message: string): boolean {
  return /checkout/i.test(message);
}

/**
 * Map an API/wallet failure to display copy, honoring `user_fixable`:
 * user-fixable errors keep the server's plain words plus a retry affordance;
 * anything else keeps the server message with "try again later" guidance and
 * no retry button. `wallet_not_configured` and hosted-Checkout
 * `already_decided` get their mandated handling.
 */
export function mapWalletError(cause: unknown): WalletErrorView {
  const fallback: WalletErrorView = {
    message: `Something went wrong. ${TRY_LATER}`,
    showRetry: false,
    checkoutBound: false,
    notConfigured: false,
  };
  if (!(cause instanceof ApiError)) return fallback;
  const { code, message: serverMessage, userFixable } = cause;
  const message = serverMessage || "Something went wrong.";

  if (code === "wallet_not_configured" || cause.status === 503) {
    return {
      message: "The wallet isn't connected on this server yet.",
      showRetry: false,
      checkoutBound: false,
      notConfigured: true,
    };
  }
  if (code === "already_decided" && isCheckoutMessage(message)) {
    return { message, showRetry: false, checkoutBound: true, notConfigured: false };
  }
  if (code === "approval_expired") {
    return {
      message: "That approval expired. Start a new payment to try again.",
      showRetry: false,
      checkoutBound: false,
      notConfigured: false,
    };
  }
  if (userFixable) {
    return { message, showRetry: true, checkoutBound: false, notConfigured: false };
  }
  return { message: withTryLater(message), showRetry: false, checkoutBound: false, notConfigured: false };
}

/* --- card + caps display lines (all from real fields) --- */

export function capitalizeBrand(brand: string): string {
  if (!brand) return "Card";
  return brand.charAt(0).toUpperCase() + brand.slice(1).toLowerCase();
}

/** "Visa ···· 4242 · 08/27" from a real saved card. */
export function formatCardLine(card: WalletCard): string {
  const month = String(card.exp_month).padStart(2, "0");
  return `${capitalizeBrand(card.brand)} ···· ${card.last4} · ${month}/${String(card.exp_year).slice(-2)}`;
}

/**
 * Caps impact from real limits: "$X of $Y monthly remaining after this".
 * Returns null when limits are unavailable (the row is then hidden, never
 * guessed). `committed` counts the spend already on the books; this
 * proposal would add `amountCents` on top.
 */
export function formatCapsLine(limits: WalletLimits | null, amountCents: number): string | null {
  if (!limits) return null;
  const after = Math.max(0, limits.remaining_cents - amountCents);
  return `${formatMoney(after, limits.currency)} of ${formatMoney(limits.monthly_cents, limits.currency)} monthly remaining after this`;
}

export function defaultCard(cards: WalletCard[]): WalletCard | null {
  if (cards.length === 0) return null;
  return cards.find((card) => card.is_default) ?? cards[0];
}

/* --- payment-sheet view model --- */

export interface SheetProposal {
  id: string;
  amountCents: number | null;
  currency: string;
  description: string;
}

export interface SheetModel {
  proposalId: string;
  /** Big-amount line, or null when no real amount is known (row hidden). */
  amountDisplay: string | null;
  /** Payee line: description, or "Kelp order" — never a raw id. */
  payeeLine: string;
  /** "6630 cents · USD"-style detail for the Amount row. */
  amountDetail: string | null;
  cardLine: string | null;
  selectedCardId: string | null;
  capsLine: string | null;
  approvalWindow: string;
  receipt: {
    amountDisplay: string;
    last4: string | null;
    paidAt: string;
    orderId: string;
    paymentIntentId: string;
  } | null;
}

export function toSheetProposal(proposal: Pick<WalletProposal, "id" | "amount_cents" | "currency" | "description">): SheetProposal {
  return {
    id: proposal.id,
    amountCents: typeof proposal.amount_cents === "number" ? proposal.amount_cents : null,
    currency: typeof proposal.currency === "string" && proposal.currency ? proposal.currency : "usd",
    description: typeof proposal.description === "string" ? proposal.description : "",
  };
}

/**
 * Build everything the payment sheet shows from the proposal plus the real
 * cards/limits snapshots. Amount/caps rows are null (hidden) when their
 * source data is missing rather than invented.
 */
export function buildSheetModel(
  proposal: SheetProposal,
  cards: WalletCard[],
  limits: WalletLimits | null,
  selectedCardId?: string | null,
): SheetModel {
  const selected = cards.find((card) => card.id === (selectedCardId ?? defaultCard(cards)?.id)) ?? null;
  const amountDisplay = proposal.amountCents !== null ? formatMoney(proposal.amountCents, proposal.currency) : null;
  return {
    proposalId: proposal.id,
    amountDisplay,
    payeeLine: proposal.description.trim() ? proposal.description.trim() : "Kelp order",
    amountDetail:
      proposal.amountCents !== null ? `${proposal.amountCents}¢ · ${proposal.currency.trim().toUpperCase() || "USD"}` : null,
    cardLine: selected ? formatCardLine(selected) : null,
    selectedCardId: selected?.id ?? null,
    capsLine: proposal.amountCents !== null ? formatCapsLine(limits, proposal.amountCents) : null,
    approvalWindow: APPROVAL_WINDOW_LABEL,
    receipt: null,
  };
}

/** Receipt portion of the sheet model, from the real decided proposal. */
export function buildReceiptModel(proposal: WalletProposal): SheetModel["receipt"] {
  const receipt = proposal.receipt;
  if (!receipt) return null;
  return {
    amountDisplay: formatMoney(receipt.amount_cents, receipt.currency),
    last4: receipt.last4,
    paidAt: formatPaidAt(receipt.paid_at),
    orderId: receipt.order_id,
    paymentIntentId: receipt.payment_intent_id,
  };
}

/** Human "paid at" line; falls back to the raw value when unparseable. */
export function formatPaidAt(iso: string): string {
  const time = Date.parse(iso);
  if (Number.isNaN(time)) return iso;
  try {
    return new Intl.DateTimeFormat("en-US", {
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "numeric",
      minute: "2-digit",
    }).format(new Date(time));
  } catch {
    return iso;
  }
}

/* --- chat entry point: proposal ref inside a chat approval --- */

export interface ProposalRef {
  proposalId: string;
  amountCents: number | null;
  currency: string;
  description: string;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function pickString(source: Record<string, unknown>, ...keys: string[]): string | null {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return null;
}

function pickAmountCents(source: Record<string, unknown>): number | null {
  for (const key of ["amount_cents", "amountCents"]) {
    const value = source[key];
    if (typeof value === "number" && Number.isInteger(value) && value > 0) return value;
  }
  return null;
}

/**
 * Extract a wallet proposal reference from a chat approval, if it carries
 * one. Accepts ONLY `proposal_id`/`proposalId`, at the top level or nested
 * under a `proposal` object — the shape a future wallet spend flow (skill or
 * assistant turn) must produce to link its approval card to the sheet.
 * Deliberately narrower than the server's vocabulary: `order_id` belongs to
 * provider/checkout orders that must never open the card sheet, and generic
 * nests like `payment` risk false positives on unrelated tool arguments.
 * Returns null when there is no proposal id — those cards keep the existing
 * inline Approve/Deny and never open the sheet.
 */
export function extractProposalRef(approval: { arguments?: unknown }): ProposalRef | null {
  const args = asRecord(approval.arguments);
  if (!args) return null;
  const nested = asRecord(args.proposal) ?? args;
  const proposalId = pickString(nested, "proposal_id", "proposalId");
  if (!proposalId) return null;
  const currency = pickString(nested, "currency") ?? pickString(args, "currency") ?? "usd";
  const description = pickString(nested, "description") ?? pickString(args, "description") ?? "";
  const amountCents = pickAmountCents(nested) ?? pickAmountCents(args);
  return { proposalId, amountCents, currency, description };
}
