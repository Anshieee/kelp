import type { Conversation, ConversationGroup } from "./types";

// Fixtures only. Nothing here is persisted and no server is contacted; the
// backend owner replaces this module with the real thread store.
const now = new Date("2026-11-02T10:00:00Z").getTime();
const hoursAgo = (h: number) => new Date(now - h * 3600_000).toISOString();

export const conversations: Conversation[] = [
  {
    id: "c-ahead-of-week",
    title: "Organise my notes for the week",
    updatedAt: hoursAgo(2),
    messages: [
      { id: "m1", role: "user", text: "Help me organise my notes for the week." },
      {
        id: "m2",
        role: "assistant",
        text: "Let's make a little sense of it. We can start with what needs your attention, then work through the rest.",
      },
      {
        id: "m3",
        role: "assistant",
        text: "I've grouped them into three buckets: decisions, follow-ups, and things that can wait until Friday.",
      },
    ],
  },
  {
    id: "c-inbox",
    title: "Sort my inbox",
    updatedAt: hoursAgo(5),
    messages: [
      { id: "m1", role: "user", text: "Sort my inbox and show me only what needs a reply." },
      {
        id: "m2",
        role: "assistant",
        text: "Done. Three need a reply: Priya, Sam, and the Q3 invoice thread.",
      },
    ],
  },
  {
    id: "c-invoice",
    title: "Follow up on an unpaid invoice",
    updatedAt: hoursAgo(26),
    messages: [
      { id: "m1", role: "user", text: "Follow up on this unpaid invoice, politely, until I hear back." },
      {
        id: "m2",
        role: "assistant",
        text: "Drafted a short, friendly nudge. I'll ask before anything is sent.",
      },
    ],
  },
  {
    id: "c-brief",
    title: "Turn a report into a one-page brief",
    updatedAt: hoursAgo(50),
    messages: [
      { id: "m1", role: "user", text: "Turn this 60-page report into a one-page decision brief." },
      {
        id: "m2",
        role: "assistant",
        text: "Here's the one-pager: the decision, the two alternatives, the cost, and the risk.",
      },
    ],
  },
];

function labelFor(iso: string): string {
  const then = new Date(iso);
  const anchor = new Date(now);
  const dayMs = 86_400_000;
  const startOfToday = new Date(
    anchor.getFullYear(),
    anchor.getMonth(),
    anchor.getDate(),
  ).getTime();
  const time = then.getTime();
  if (time >= startOfToday) return "Today";
  if (time >= startOfToday - dayMs) return "Yesterday";
  if (time >= startOfToday - 7 * dayMs) return "Previous 7 days";
  return "Older";
}

export function groupConversations(list: Conversation[]): ConversationGroup[] {
  const order = ["Today", "Yesterday", "Previous 7 days", "Older"];
  const groups = new Map<string, Conversation[]>();
  for (const conversation of list) {
    const label = labelFor(conversation.updatedAt);
    const bucket = groups.get(label) ?? [];
    bucket.push(conversation);
    groups.set(label, bucket);
  }
  return order
    .filter((label) => groups.has(label))
    .map((label) => ({ label, conversations: groups.get(label)! }));
}

export const groupedConversations = (): ConversationGroup[] =>
  groupConversations(conversations);

// Stand-in for the streaming reply the backend will provide.
export function cannedReply(input: string): string {
  const trimmed = input.trim();
  if (!trimmed) return "Ask me anything — I'll pick it up from there.";
  if (/\binbox|email|mail\b/i.test(trimmed)) {
    return "I'll sort that inbox and show you only the messages that need a reply.";
  }
  if (/\binvoice|payment|paid\b/i.test(trimmed)) {
    return "I can draft a polite follow-up and ask before anything is sent.";
  }
  if (/\bnote|organise|organize|plan\b/i.test(trimmed)) {
    return "Let's make a little sense of it. I'll group these into what's urgent and what can wait.";
  }
  return `Here's what I'd do with “${trimmed}”: start with the smallest useful step, then report back.`;
}
