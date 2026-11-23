import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api";
import { setSession } from "../state";
import {
  buildReceiptModel,
  buildSheetModel,
  decidePayment,
  extractProposalRef,
  formatCardLine,
  formatMoney,
  formatPaidAt,
  getCards,
  getLimits,
  mapWalletError,
  MAX_AMOUNT_CENTS,
  newIdempotencyKey,
  parseAmountToCents,
  proposalFingerprint,
  proposePayment,
  resolveIdempotencyKey,
  toSheetProposal,
} from "../wallet";

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  setSession(null);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function jsonResponse(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

describe("formatMoney", () => {
  it("formats USD like $66.30", () => {
    expect(formatMoney(6630, "usd")).toBe("$66.30");
  });

  it("formats EUR, GBP, and INR with their symbols", () => {
    expect(formatMoney(1250, "EUR")).toContain("12,50");
    expect(formatMoney(1250, "EUR")).toContain("€");
    expect(formatMoney(2000, "gbp")).toBe("£20.00");
    const inr = formatMoney(9999, "INR");
    expect(inr).toContain("₹");
    expect(inr).toContain("99.99");
  });

  it("falls back to CODE 0.00 style for unknown currencies", () => {
    expect(formatMoney(100, "xx")).toBe("XX 1.00");
  });
});

describe("parseAmountToCents", () => {
  it("parses major units to integer cents", () => {
    expect(parseAmountToCents("12.50")).toEqual({ ok: true, cents: 1250 });
    expect(parseAmountToCents("66.30")).toEqual({ ok: true, cents: 6630 });
    expect(parseAmountToCents("5")).toEqual({ ok: true, cents: 500 });
  });

  it("rounds fractional cents down (floor)", () => {
    expect(parseAmountToCents("10.999")).toEqual({ ok: true, cents: 1099 });
    expect(parseAmountToCents("10.991")).toEqual({ ok: true, cents: 1099 });
    expect(parseAmountToCents("0.019")).toEqual({ ok: true, cents: 1 });
  });

  it("rejects non-numeric, negative, and zero input", () => {
    expect(parseAmountToCents("abc").ok).toBe(false);
    expect(parseAmountToCents("-5").ok).toBe(false);
    expect(parseAmountToCents("0").ok).toBe(false);
    expect(parseAmountToCents("0.00").ok).toBe(false);
    expect(parseAmountToCents("").ok).toBe(false);
    expect(parseAmountToCents("12.5.6").ok).toBe(false);
  });

  it("rejects amounts above the server max", () => {
    expect(MAX_AMOUNT_CENTS).toBe(99999999);
    expect(parseAmountToCents("999999.99")).toEqual({ ok: true, cents: 99999999 });
    expect(parseAmountToCents("1000000").ok).toBe(false);
  });
});

describe("newIdempotencyKey", () => {
  it("mints uuid-shaped keys, fresh per attempt", () => {
    const first = newIdempotencyKey();
    const second = newIdempotencyKey();
    expect(first).toMatch(UUID_RE);
    expect(second).toMatch(UUID_RE);
    expect(first).not.toBe(second);
  });

  it("reuses one key per logical proposal and mints on field edits", () => {
    const fp = proposalFingerprint({ amountCents: 6630, currency: "usd", description: "Guest & Ribbon order" });
    // Same fields (modulo cosmetic casing/whitespace) reuse the in-flight key:
    // a retry after a lost response replays against the same pending order
    // instead of reserving cap twice.
    const first = resolveIdempotencyKey(null, fp);
    expect(first.reused).toBe(false);
    const retry = resolveIdempotencyKey(
      { key: first.key, fingerprint: fp },
      proposalFingerprint({ amountCents: 6630, currency: "USD", description: "  Guest & Ribbon order " }),
    );
    expect(retry).toEqual({ key: first.key, reused: true });
    // Any real field edit is a different payment and mints a fresh key.
    const edited = resolveIdempotencyKey(
      { key: first.key, fingerprint: fp },
      proposalFingerprint({ amountCents: 6640, currency: "usd", description: "Guest & Ribbon order" }),
    );
    expect(edited.reused).toBe(false);
    expect(edited.key).not.toBe(first.key);
  });
});

describe("wallet client", () => {
  it("fetches saved cards", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, {
        ok: true,
        cards: [{ id: "pm_1", brand: "visa", last4: "4242", exp_month: 8, exp_year: 2027, is_default: true }],
      }),
    );
    const data = await getCards();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/wallet/cards");
    expect(data.cards).toHaveLength(1);
    expect(data.cards[0].last4).toBe("4242");
  });

  it("fetches limits for a currency", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, {
        ok: true,
        limits: {
          currency: "usd",
          per_purchase_cents: 50000,
          monthly_cents: 200000,
          spent_cents: 10000,
          reserved_cents: 5000,
          committed_cents: 15000,
          remaining_cents: 185000,
        },
      }),
    );
    const data = await getLimits("usd");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/wallet/limits?currency=usd");
    expect(data.limits.remaining_cents).toBe(185000);
  });

  it("propose sends a fresh uuid idempotency key per attempt", async () => {
    fetchMock.mockImplementation(async (_url: string, init: { body: string }) => {
      const body = JSON.parse(init.body) as Record<string, unknown>;
      return jsonResponse(201, {
        ok: true,
        proposal: {
          id: "ord_1",
          amount_cents: body.amount_cents,
          currency: body.currency,
          description: body.description,
          status: "proposed",
          created_at: "2026-01-01T00:00:00Z",
          payment_method_id: null,
          decided_at: null,
        },
        idempotent: false,
      });
    });
    const first = await proposePayment({ amountCents: 6630, currency: "usd", description: "Guest & Ribbon order" });
    const second = await proposePayment({ amountCents: 6630, currency: "usd", description: "Guest & Ribbon order" });
    const firstBody = JSON.parse(fetchMock.mock.calls[0][1].body) as Record<string, unknown>;
    const secondBody = JSON.parse(fetchMock.mock.calls[1][1].body) as Record<string, unknown>;
    expect(firstBody.amount_cents).toBe(6630);
    expect(firstBody.idempotency_key).toMatch(UUID_RE);
    expect(secondBody.idempotency_key).toMatch(UUID_RE);
    expect(firstBody.idempotency_key).not.toBe(secondBody.idempotency_key);
    expect(first.proposal.id).toBe("ord_1");
    expect(second.proposal.description).toBe("Guest & Ribbon order");
  });

  it("decide sends the selected card id", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, {
        ok: true,
        proposal: { id: "ord_1", status: "paid" },
        idempotent: false,
      }),
    );
    await decidePayment("ord_1", "approve", "pm_1");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/wallet/approve");
    expect(JSON.parse(init.body)).toEqual({ proposal_id: "ord_1", decision: "approve", payment_method_id: "pm_1" });
  });
});

describe("proposal -> sheet view model", () => {
  const proposal = toSheetProposal({
    id: "ord_1",
    amount_cents: 6630,
    currency: "usd",
    description: "Guest & Ribbon order",
  });
  const cards = [
    { id: "pm_1", brand: "visa", last4: "4242", exp_month: 8, exp_year: 2027, is_default: true },
    { id: "pm_2", brand: "mastercard", last4: "5555", exp_month: 1, exp_year: 2028, is_default: false },
  ];
  const limits = {
    currency: "usd",
    per_purchase_cents: 50000,
    monthly_cents: 50000,
    spent_cents: 30000,
    reserved_cents: 0,
    committed_cents: 30000,
    remaining_cents: 10000,
  };

  it("maps the amount display from real cents", () => {
    const model = buildSheetModel(proposal, cards, limits, null);
    expect(model.amountDisplay).toBe("$66.30");
    expect(model.payeeLine).toBe("Guest & Ribbon order");
  });

  it("falls back to 'Kelp order' instead of showing a raw id as payee", () => {
    const model = buildSheetModel({ ...proposal, description: "  " }, cards, limits, null);
    expect(model.payeeLine).toBe("Kelp order");
  });

  it("maps the card line with brand, last4, and expiry", () => {
    expect(formatCardLine(cards[0])).toBe("Visa ···· 4242 · 08/27");
    const model = buildSheetModel(proposal, cards, limits, null);
    expect(model.cardLine).toBe("Visa ···· 4242 · 08/27");
    expect(model.selectedCardId).toBe("pm_1");
  });

  it("maps the caps line from real limits", () => {
    const model = buildSheetModel(proposal, cards, limits, null);
    expect(model.capsLine).toBe("$33.70 of $500.00 monthly remaining after this");
  });

  it("hides the caps row when limits are unavailable", () => {
    const model = buildSheetModel(proposal, cards, null, null);
    expect(model.capsLine).toBeNull();
  });

  it("hides amount rows when no real amount is known", () => {
    const model = buildSheetModel({ ...proposal, amountCents: null }, cards, limits, null);
    expect(model.amountDisplay).toBeNull();
    expect(model.amountDetail).toBeNull();
    expect(model.capsLine).toBeNull();
  });

  it("maps the receipt from the decided proposal", () => {
    const receipt = buildReceiptModel({
      id: "ord_1",
      amount_cents: 6630,
      currency: "usd",
      description: "Guest & Ribbon order",
      status: "paid",
      created_at: "2026-01-01T00:00:00Z",
      payment_method_id: "pm_1",
      decided_at: "2026-01-02T00:00:00Z",
      receipt: {
        order_id: "ord_1",
        payment_intent_id: "pi_123",
        amount_cents: 6630,
        currency: "usd",
        last4: "4242",
        paid_at: "2026-01-02T03:04:05Z",
      },
    });
    expect(receipt?.amountDisplay).toBe("$66.30");
    expect(receipt?.last4).toBe("4242");
    expect(receipt?.orderId).toBe("ord_1");
    expect(receipt?.paymentIntentId).toBe("pi_123");
    expect(receipt?.paidAt).not.toBe("2026-01-02T03:04:05Z");
  });

  it("returns null receipt when the proposal carries none", () => {
    expect(
      buildReceiptModel({
        id: "ord_1",
        amount_cents: 6630,
        currency: "usd",
        description: "",
        status: "denied",
        created_at: "",
        payment_method_id: null,
        decided_at: null,
      }),
    ).toBeNull();
  });

  it("formats paid_at honestly, passing through unparseable values", () => {
    expect(formatPaidAt("not-a-date")).toBe("not-a-date");
    expect(formatPaidAt("2026-01-02T03:04:05Z")).toContain("2026");
  });
});

describe("extractProposalRef", () => {
  it("extracts a flat proposal ref from approval arguments", () => {
    expect(
      extractProposalRef({
        arguments: { proposal_id: "ord_9", amount_cents: 100, currency: "usd", description: "Tee" },
      }),
    ).toEqual({ proposalId: "ord_9", amountCents: 100, currency: "usd", description: "Tee" });
  });

  it("extracts a nested proposal ref", () => {
    expect(
      extractProposalRef({ arguments: { proposal: { proposal_id: "ord_9", amount_cents: 250 } } }),
    ).toEqual({ proposalId: "ord_9", amountCents: 250, currency: "usd", description: "" });
  });

  it("returns null when no proposal id is carried", () => {
    expect(extractProposalRef({ arguments: { command: ["ls"] } })).toBeNull();
    expect(extractProposalRef({})).toBeNull();
    expect(extractProposalRef({ arguments: null })).toBeNull();
  });

  it("refuses lookalike keys that are not wallet proposal ids", () => {
    // order_id belongs to provider/checkout orders that must never open the
    // card sheet; a bare `proposal` string and a generic `payment` nest are
    // not the documented producer shape either.
    expect(extractProposalRef({ arguments: { order_id: "order_9" } })).toBeNull();
    expect(extractProposalRef({ arguments: { proposal: "ord_9" } })).toBeNull();
    expect(
      extractProposalRef({ arguments: { payment: { proposal_id: "ord_9" } } }),
    ).toBeNull();
  });
});

describe("mapWalletError", () => {
  it("maps wallet_not_configured to the honest empty-state copy", () => {
    const view = mapWalletError(new ApiError(503, "wallet_not_configured", "The wallet isn't connected yet.", false, false));
    expect(view.message).toBe("The wallet isn't connected on this server yet.");
    expect(view.showRetry).toBe(false);
    expect(view.notConfigured).toBe(true);
    expect(view.checkoutBound).toBe(false);
  });

  it("passes the Checkout message through with no card button", () => {
    const view = mapWalletError(
      new ApiError(409, "already_decided", "That order is being paid through Checkout.", false, false),
    );
    expect(view.message).toBe("That order is being paid through Checkout.");
    expect(view.checkoutBound).toBe(true);
    expect(view.showRetry).toBe(false);
  });

  it("maps expired approvals without a retry affordance", () => {
    const view = mapWalletError(new ApiError(409, "approval_expired", "That approval expired.", false, false));
    expect(view.message).toContain("Start a new payment");
    expect(view.showRetry).toBe(false);
  });

  it("offers Try again for user-fixable errors", () => {
    const view = mapWalletError(new ApiError(400, "invalid_amount", "Enter an amount greater than zero.", false, true));
    expect(view.message).toBe("Enter an amount greater than zero.");
    expect(view.showRetry).toBe(true);
  });

  it("adds try-again-later guidance for non-fixable errors", () => {
    const view = mapWalletError(new ApiError(502, "gateway_error", "Can't reach the card processor right now.", false, false));
    expect(view.message).toContain("Please try again later.");
    expect(view.showRetry).toBe(false);
  });

  it("handles non-ApiError failures generically", () => {
    const view = mapWalletError(new TypeError("fetch failed"));
    expect(view.message).toContain("try again later");
    expect(view.showRetry).toBe(false);
  });
});
