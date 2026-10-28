# Frontend scope for Figma (simple version)

**Status:** design brief only. No frontend code is being written until the Figma file is ready.
**Date:** 2026-10-28
**Audience:** whoever draws the screens.

The app is a private assistant you talk to in a browser. You ask it things, it reads your
documents and searches the web and GitHub, and it can take real actions (spending money,
sending things) **only after you press Approve**. Everything it does is written down so you can
check it later.

---

## 1. Screens to design

| # | Screen | What the person sees | What Figma needs to show |
|---|---|---|---|
| 1 | **Chat** | A conversation. Your messages on one side, assistant answers on the other. | Normal answer, answer with sources, answer with a file card, answer asking for approval, long answer that scrolls |
| 2 | **Document cards** (inside chat) | A small card for a PDF summary or a CSV table. | PDF card (name, page count, short summary, "Open"), CSV card (name, row count, small table preview, the number the person asked for) |
| 3 | **Search sources** | Little chips under an answer showing where it looked. | 1 source, 3 sources, many sources (wraps to a second line), one source that failed |
| 4 | **GitHub sweep** | A wider card when the assistant checks GitHub: which repo, what it found, how many items. | A sweep that found things, a sweep that found nothing, a sweep scanning right now |
| 5 | **Approvals inbox** | A list of things waiting for your yes/no. Each row: what it wants to do, how much it costs (if money), Approve / Reject. | List with 1 item, list with many, one item opened in full, the moment after Approve, the moment after Reject |
| 6 | **Scheduler** | Recurring jobs the assistant runs for you. | List of jobs (name, how often, next run, on/off switch), one job opened, no jobs yet |
| 7 | **Wallet — saved cards** | Your payment cards, kept safe by the card company (Stripe). | No cards yet, one card, three cards, one card expired |
| 8 | **Wallet — add card** | The form to add a new card. | Empty form, filled form, card rejected, saving |
| 9 | **Wallet — pay modal** | The final "pay this" pop-up after you approve a purchase. | Before you pay, paying, paid (with receipt), failed |
| 10 | **Wallet — limits** | Your spending rules: per-purchase cap, monthly cap, how much is left. | Under the limit, close to the limit, over the limit (blocked) |
| 11 | **Memory viewer** | The things the assistant remembers about you. | List of memories, one opened, edit, delete, nothing remembered yet |
| 12 | **Activity log** | A plain timeline of everything that happened. | Today only, several days, one entry opened with full detail |
| 13 | **Empty states** | What each screen says when there is nothing in it yet. | One for each screen above, plus the first-run welcome on chat |

**Keep it simple for the person reading it.** One idea per screen. Plain words, not system words.
"Approve" and "Reject", not "Authorize" and "Deny". "Add card", not "Provision payment method".

---

## 2. The three main flows

### Flow A — Drop a file, get a summary

1. Person drags a PDF or CSV onto the chat.
2. The file shows up as a small chip with a progress bar.
3. Assistant says it is reading it ("Reading your file…").
4. A document card appears with the summary (PDF) or the table preview and the answer (CSV).
5. Person can ask follow-up questions; the card stays in the conversation.

### Flow B — Ask to sweep

1. Person types "check my repo for open issues" (or similar).
2. Assistant says what it is about to check and shows it scanning.
3. A sweep card fills in: repo name, how many items, short list of what matters.
4. If the sweep wants to *write* anything (comment, close, spend), it does not just do it —
   it becomes an item in the approvals inbox instead. Read-only sweeps finish on their own.

### Flow C — Propose → Approve → Receipt

1. Assistant wants to do something that costs money or changes something outside the app.
2. It writes a **proposal**: what, why, how much, and what happens if you say yes.
3. The proposal sits in the **approvals inbox** and the chat says "Waiting on you".
4. Person presses **Approve**. If money is involved, the **pay modal** opens and they confirm.
5. The action runs. Person sees a **receipt**: what was done, when, and the amount if any.
6. Same action is also written into the **activity log**.

If the person presses **Reject**: nothing happens, nothing is charged, and the rejection is
recorded in the activity log.

---

## 3. The four states every screen needs

Every card, list row, and action in the app is in one of four states. Figma should show all four
for the chat answer, the sweep card, the approval row, and the pay modal.

| State | Plain wording on screen | How it should look |
|---|---|---|
| **Working** | "Working…" / "Reading your file…" / "Checking GitHub…" | A gentle moving indicator. Nothing clickable yet. Never a frozen screen — if it takes long, add "still working" after a few seconds |
| **Waiting on you** | "Waiting on you" / "Needs your approval" | A clear, calm highlight. This is the one thing the person must notice. Buttons are visible, not hidden in a menu |
| **Failed** | "That didn't work" + one plain sentence why + "Try again" | No scary error codes on screen. Show the reason in normal words, keep the details behind "See details" |
| **Done** | "Done" (or just the result, no label needed) | Quiet. Checkmark or timestamp. No success banners, no confetti |

Extra rule for money: a payment is **never** shown as "Done" until the receipt exists. Until then
it is "Working" or "Waiting on you".

---

## 4. Server calls Figma needs to know about

Figma only needs these so it can draw the right loading and error states — not to build anything.
All of them are plain reads and writes with the same four states above.

| Screen | Call | What it does | When Figma sees it |
|---|---|---|---|
| Wallet — saved cards | `GET /cards` | Returns the person's saved cards | On opening the wallet; shows "Working" then either cards or the empty state |
| Wallet — add card | `POST /setup-intent` | Starts adding a new card, returns a token for the card form | When the person presses "Add card" (before the form appears) |
| Wallet — add card | `POST /attach` | Saves the card the person just entered | When they press "Save card"; shows saving, then the new card, or "That didn't work" |
| Wallet — limits | `GET /limits` | Returns the per-purchase cap, the monthly cap, and how much is left | On the wallet screen; the "over the limit" look comes from this |
| Approvals inbox | `POST /propose` | Turns an intended action into a proposal for the person to review | Mid-conversation; the new row appears in the inbox as "Waiting on you" |
| Approvals inbox | `POST /approve` | Records the yes (or the no) and runs the action | When the person presses Approve or Reject; then the receipt |
| Activity log | `GET /activity` | Returns the timeline of what happened | On opening the log, and after every approval |

That is the whole list Figma needs. Everything else on these screens is either local to the
browser or comes back inside the chat stream.

---

## 5. Notes for the Figma file

- **Desktop first, then a narrow version.** The person uses this on a laptop. Show one narrow
  phone-width version of chat and the approvals inbox, nothing more.
- **One warning colour only.** "Waiting on you" is the one thing allowed to stand out. If
  everything is highlighted, nothing is.
- **Money screens get the most care.** Pay modal, limits, and the receipt are the screens people
  will actually be nervous about. Big numbers, plain wording, an obvious way to cancel.
- **Every empty state should say what to do next**, not just "No items". Example: "No cards yet.
  Add one to let the assistant pay for things you approve."
- **Skip for now:** voice input, WhatsApp mirroring, sharing with other people, and any
  third-party app store. Not in this round.
