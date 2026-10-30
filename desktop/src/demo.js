// Demo fixtures for the early release (?demo=1). All data is fake,
// hardcoded, and never touches a server. Every surface is labeled demo.

export function isDemo() {
  // Demo branch default: demo mode unless the user opts into live.
  // ?live=1 (or a previous opt-in) selects the real server path.
  try {
    const q = new URLSearchParams(location.search);
    if (q.get("live") === "1") {
      localStorage.setItem("kelp.live", "1");
      return false;
    }
    if (q.get("demo") === "1") {
      localStorage.removeItem("kelp.live");
      return true;
    }
    return localStorage.getItem("kelp.live") !== "1";
  } catch {
    return true;
  }
}

export const demoThreads = [
  { id: "demo-thread-1", title: "Demo: weekend trip plan" },
  { id: "demo-thread-2", title: "Demo: inbox sweep" },
];

export const demoMessages = {
  "demo-thread-1": [
    { role: "user", content: "Find me a quiet weekend trip under $400." },
    { role: "assistant", content: "Demo answer: three options under $400, all fake. Connect a server for real results." },
  ],
  "demo-thread-2": [
    { role: "user", content: "Summarize my inbox." },
    { role: "assistant", content: "Demo answer: 12 messages, 3 need replies, all fake." },
  ],
};

export const demoApprovals = [
  {
    id: "demo-apr-1",
    title: "Demo: book train tickets ($86)",
    detail: "Demo approval. Approving here records nothing and charges nothing.",
    tool: "orders.propose",
  },
];

export const demoDevices = [
  { id: "demo-dev-1", label: "Demo laptop (this device)", current: true, last_seen: "just now" },
  { id: "demo-dev-2", label: "Demo phone", current: false, last_seen: "2h ago" },
];

export const demoSpendText = "demo window: $1.20 of $40.00 (demo data, no cap enforced)";

export function demoCannedReply(text) {
  return `Demo reply to: "${text.slice(0, 80)}" — connect a server for a real answer.`;
}
