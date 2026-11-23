/**
 * @vitest-environment jsdom
 *
 * Component tests for the payment flow. The money bug these pin (review
 * thread on PR #59, item C1): the sheet must branch the receipt view on the
 * RETURNED proposal status, never on the requested decision — an approve
 * replayed onto an already-denied order answers 200 with status "denied",
 * and paid replays answer 200 (409s are the not-paid outcomes). Showing
 * "Paid" for any of those fabricates a receipt.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import PaymentSheet from "../components/PaymentSheet";
import Wallet from "../screens/Wallet";
import Propose from "../screens/Propose";

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

const CARDS = {
  ok: true,
  cards: [
    { id: "card_1", brand: "Visa", last4: "4242", exp_month: 12, exp_year: 2028, is_default: true },
  ],
};

const LIMITS = {
  ok: true,
  limits: {
    currency: "usd", per_purchase_cents: 50000, monthly_cents: 200000,
    spent_cents: 0, reserved_cents: 0, committed_cents: 0, remaining_cents: 200000,
  },
};

function proposalWith(status: string, withReceipt: boolean) {
  return {
    id: "ord_9",
    amount_cents: 6630,
    currency: "usd",
    description: "Guest & Ribbon order",
    status,
    created_at: "2026-01-01T00:00:00Z",
    payment_method_id: withReceipt ? "card_1" : null,
    decided_at: "2026-01-01T00:01:00Z",
    ...(withReceipt
      ? {
          receipt: {
            order_id: "ord_9", payment_intent_id: "pi_9", amount_cents: 6630,
            currency: "usd", last4: "4242", paid_at: "2026-01-01T00:01:30Z",
          },
        }
      : {}),
  };
}

let container: HTMLDivElement | null = null;
let root: Root | null = null;

function mount(element: React.ReactElement) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(element);
  });
}

function unmount() {
  act(() => {
    root?.unmount();
  });
  container?.remove();
  container = null;
  root = null;
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  if (root) unmount();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function mockFetch(handler: (url: string, init: { body?: string; method?: string }) => unknown) {
  (fetch as unknown as ReturnType<typeof vi.fn>).mockImplementation(
    async (url: string, init: { body?: string; method?: string } = {}) => {
      const result = handler(url, init);
      if (result instanceof Response) return result;
      const [status, body] = result as [number, unknown];
      return jsonResponse(status, body);
    },
  );
}

function text(): string {
  return container?.textContent ?? "";
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
  });
}

describe("PaymentSheet receipt branching (C1: never a fabricated receipt)", () => {
  const baseProposal = { id: "ord_9", amountCents: 6630, currency: "usd", description: "Guest & Ribbon order" };

  it("shows the paid receipt only for status paid with a receipt body", async () => {
    mockFetch((url) => {
      if (url.endsWith("/api/wallet/cards")) return [200, CARDS];
      if (url.includes("/api/wallet/limits")) return [200, LIMITS];
      if (url.endsWith("/api/wallet/approve"))
        return [200, { ok: true, proposal: proposalWith("paid", true), idempotent: false }];
      throw new Error(`unexpected fetch: ${url}`);
    });
    mount(
      <PaymentSheet
        proposal={baseProposal}
        initialCards={null}
        initialLimits={null}
        onClose={() => {}}
      />,
    );
    await flush();
    const approve = [...(container?.querySelectorAll("button") ?? [])].find((b) =>
      b.textContent?.includes("Approve"),
    );
    expect(approve).toBeTruthy();
    await act(async () => {
      approve?.click();
    });
    await flush();
    expect(text()).toMatch(/Paid \$66\.30/);
    expect(text()).toMatch(/4242/);
  });

  it("shows cancelled — never Paid — when approve replays onto a denied order", async () => {
    mockFetch((url) => {
      if (url.endsWith("/api/wallet/cards")) return [200, CARDS];
      if (url.includes("/api/wallet/limits")) return [200, LIMITS];
      if (url.endsWith("/api/wallet/approve"))
        return [200, { ok: true, proposal: proposalWith("denied", false), idempotent: true }];
      throw new Error(`unexpected fetch: ${url}`);
    });
    mount(
      <PaymentSheet
        proposal={baseProposal}
        initialCards={null}
        initialLimits={null}
        onClose={() => {}}
      />,
    );
    await flush();
    const approve = [...(container?.querySelectorAll("button") ?? [])].find((b) =>
      b.textContent?.includes("Approve"),
    );
    await act(async () => {
      approve?.click();
    });
    await flush();
    expect(text()).toMatch(/cancelled|No charge was made/i);
    expect(text()).not.toMatch(/Paid \$66\.30/);
  });

  it("shows the error phase — never Paid — on a 409 already_decided", async () => {
    mockFetch((url) => {
      if (url.endsWith("/api/wallet/cards")) return [200, CARDS];
      if (url.includes("/api/wallet/limits")) return [200, LIMITS];
      if (url.endsWith("/api/wallet/approve"))
        return [
          409,
          { ok: false, error: { code: "already_decided", message: "That order was already answered.", retryable: false, user_fixable: false } },
        ];
      throw new Error(`unexpected fetch: ${url}`);
    });
    mount(
      <PaymentSheet
        proposal={baseProposal}
        initialCards={null}
        initialLimits={null}
        onClose={() => {}}
      />,
    );
    await flush();
    const approve = [...(container?.querySelectorAll("button") ?? [])].find((b) =>
      b.textContent?.includes("Approve"),
    );
    await act(async () => {
      approve?.click();
    });
    await flush();
    expect(text()).not.toMatch(/Paid \$66\.30/);
    expect(text()).toMatch(/already answered|try again later/i);
  });
});

describe("Wallet screen", () => {
  it("renders the honest unconfigured state with no dead buttons", async () => {
    mockFetch(() => [
      503,
      { ok: false, error: { code: "wallet_not_configured", message: "The wallet isn't connected yet.", retryable: false, user_fixable: false } },
    ]);
    mount(<Wallet onBack={() => {}} onNewPayment={() => {}} />);
    await flush();
    expect(text()).toMatch(/isn't connected/i);
    // No New payment button may be enabled in this state.
    const buttons = [...(container?.querySelectorAll("button:not([disabled])") ?? [])].map((b) => b.textContent);
    expect(buttons.join(" ")).not.toMatch(/New payment/);
  });

  it("lists saved cards with masked numbers and caps", async () => {
    mockFetch((url) => {
      if (url.endsWith("/api/wallet/cards")) return [200, CARDS];
      if (url.includes("/api/wallet/limits")) return [200, LIMITS];
      throw new Error(`unexpected fetch: ${url}`);
    });
    mount(<Wallet onBack={() => {}} onNewPayment={() => {}} />);
    await flush();
    expect(text()).toMatch(/4242/);
    expect(text()).not.toMatch(/4111111111111111/);
  });
});

describe("Propose screen", () => {
  it("rejects a non-numeric amount without touching the network", async () => {
    const fetchSpy = vi.fn(async () => jsonResponse(201, { ok: true, proposal: proposalWith("pending", false) }));
    vi.stubGlobal("fetch", fetchSpy);
    mount(<Propose onBack={() => {}} />);
    await flush();
    const amount = container?.querySelector("#pay-amount") as HTMLInputElement | null;
    expect(amount).toBeTruthy();
    await act(async () => {
      if (amount) {
        amount.value = "not-a-number";
        amount.dispatchEvent(new Event("input", { bubbles: true }));
      }
    });
    const review = [...(container?.querySelectorAll("button") ?? [])].find((b) =>
      /review|continue|propose/i.test(b.textContent ?? ""),
    );
    await act(async () => {
      review?.click();
    });
    await flush();
    expect(fetchSpy).not.toHaveBeenCalledWith(
      expect.stringContaining("/api/wallet/propose"),
      expect.anything(),
    );
    expect(text()).toMatch(/greater than zero|valid amount|enter an amount/i);
  });
});
