/*
 * Wallet screen: saved cards, caps glance, and the "New payment" entry.
 *
 * Unconfigured state (the server has no Stripe config): the app's
 * empty-state pattern with honest copy and NO dead buttons — nothing to tap
 * that could not work.
 */
import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../api";
import { formatCardLine, formatMoney, getCards, getLimits, mapWalletError, type WalletCard, type WalletLimits } from "../wallet";

interface WalletProps {
  onBack: () => void;
  onNewPayment: () => void;
}

type LoadState =
  | { name: "loading" }
  | { name: "ready"; cards: WalletCard[]; limits: WalletLimits | null }
  | { name: "unconfigured" }
  | { name: "error"; message: string };

const LIMITS_CURRENCY = "usd";

export default function Wallet({ onBack, onNewPayment }: WalletProps) {
  const [state, setState] = useState<LoadState>({ name: "loading" });

  const refresh = useCallback(async () => {
    setState({ name: "loading" });
    try {
      const [cardsData, limitsData] = await Promise.all([
        getCards(),
        getLimits(LIMITS_CURRENCY).catch(() => null),
      ]);
      setState({ name: "ready", cards: cardsData.cards, limits: limitsData?.limits ?? null });
    } catch (cause) {
      const view = mapWalletError(cause);
      if (view.notConfigured) {
        setState({ name: "unconfigured" });
      } else {
        setState({ name: "error", message: cause instanceof ApiError ? cause.message : "Couldn't load your wallet." });
      }
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <main className="screen settings-screen">
      <header className="top-bar">
        <button type="button" className="icon-btn" aria-label="Back to threads" onClick={onBack}>
          <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
            <path d="M15.5 4.5 8 12l7.5 7.5" stroke="currentColor" strokeWidth="2" fill="none" strokeLinecap="round" />
          </svg>
        </button>
        <h1>Wallet</h1>
      </header>

      {state.name === "loading" && <p className="empty-note">Loading…</p>}

      {state.name === "unconfigured" && (
        <div className="empty-state">
          <p>The wallet isn&apos;t connected on this server yet.</p>
          <p className="settings-note">Once the server owner connects payments, your saved cards and caps will show up here.</p>
        </div>
      )}

      {state.name === "error" && (
        <div className="empty-state">
          <p role="alert">{state.message}</p>
          <button type="button" className="btn" onClick={() => void refresh()}>
            Try again
          </button>
        </div>
      )}

      {state.name === "ready" && (
        <>
          <section className="settings-section" aria-labelledby="wallet-cards-heading">
            <h2 id="wallet-cards-heading">Saved cards</h2>
            {state.cards.length === 0 ? (
              <p className="settings-note">No saved cards on this server yet.</p>
            ) : (
              <ul className="device-list">
                {state.cards.map((card) => (
                  <li key={card.id} className="device-item">
                    <div className="device-info">
                      <span className="device-name">
                        {formatCardLine(card)}
                        {card.is_default && <span className="device-chip">Default</span>}
                      </span>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="settings-section" aria-labelledby="wallet-caps-heading">
            <h2 id="wallet-caps-heading">Caps glance</h2>
            {state.limits ? (
              <dl className="glance-card">
                <div className="glance-row">
                  <dt>Per purchase</dt>
                  <dd>{formatMoney(state.limits.per_purchase_cents, state.limits.currency)}</dd>
                </div>
                <div className="glance-row">
                  <dt>Monthly remaining</dt>
                  <dd>{formatMoney(state.limits.remaining_cents, state.limits.currency)}</dd>
                </div>
              </dl>
            ) : (
              <p className="settings-note">Caps are unavailable right now.</p>
            )}
          </section>

          <section className="settings-section">
            <button type="button" className="btn btn-pay btn-block" onClick={onNewPayment}>
              New payment
            </button>
          </section>
        </>
      )}
    </main>
  );
}
