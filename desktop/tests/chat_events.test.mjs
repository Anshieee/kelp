// The stream→transcript projection is pure, so it is tested directly.
// Node 22.20 strips the types in chatEvents.ts on import.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { applyEvent, computerCardAnchors, dismissCardMessage, injectDebugCard, withSyntheticCards } from "../src/chatEvents.ts";

const assistant = { id: "msg_a", role: "assistant", status: "streaming", blocks: [] };
const withAssistant = () => [{ ...assistant, blocks: [] }];

const event = (type, data) => ({ threadId: "thr_1", id: "1", event: type, data });

describe("applyEvent", () => {
  it("opens a text block from token_delta and appends the next delta", () => {
    const first = applyEvent(withAssistant(), event("token_delta", { message_id: "msg_a", text: "Hel" }));
    const second = applyEvent(first, event("token_delta", { message_id: "msg_a", text: "lo" }));
    assert.deepEqual(second[0].blocks, [{ type: "text", text: "Hello" }]);
  });

  it("starts a new text block after a tool call", () => {
    let messages = withAssistant();
    messages = applyEvent(messages, event("token_delta", { message_id: "msg_a", text: "Thinking." }));
    messages = applyEvent(messages, event("tool_call_started", { message_id: "msg_a", call_id: "c1", tool: "web.search", label: "Web search", summary: "kites" }));
    messages = applyEvent(messages, event("token_delta", { message_id: "msg_a", text: "Found it." }));
    assert.deepEqual(messages[0].blocks, [
      { type: "text", text: "Thinking." },
      {
        type: "tool_call",
        call_id: "c1",
        tool: "web.search",
        label: "Web search",
        summary: "kites",
        status: "running",
        duration_ms: null,
        effects: [],
        approval_id: null,
        detail: { arguments: {}, result_summary: null, result: null, error: null },
        surface: null,
        session: null,
      },
      { type: "text", text: "Found it." },
    ]);
  });

  it("updates the tool block in place on tool_call_finished", () => {
    let messages = withAssistant();
    messages = applyEvent(messages, event("tool_call_started", { message_id: "msg_a", call_id: "c1", tool: "web.search", label: "Web search" }));
    messages = applyEvent(messages, event("tool_call_finished", { message_id: "msg_a", call_id: "c1", ok: true, status: "done", duration_ms: 42, result_summary: "3 results", detail: { result_summary: "3 results" } }));
    const [tool] = messages[0].blocks;
    assert.equal(tool.status, "done");
    assert.equal(tool.duration_ms, 42);
    assert.equal(tool.summary, "3 results");
  });

  it("marks the tool waiting and adds the approval card", () => {
    let messages = withAssistant();
    messages = applyEvent(messages, event("tool_call_started", { message_id: "msg_a", call_id: "c1", tool: "shell.run" }));
    messages = applyEvent(
      messages,
      event("approval_requested", { message_id: "msg_a", approval: { id: "apr_1", call_id: "c1", tool: "shell.run", title: "Run a command", status: "pending" } }),
    );
    const [tool, approval] = messages[0].blocks;
    assert.equal(tool.status, "waiting_approval");
    assert.equal(tool.approval_id, "apr_1");
    assert.equal(approval.type, "approval");
    assert.equal(approval.approval.status, "pending");
  });

  it("settles both blocks on approval_resolved", () => {
    let messages = withAssistant();
    messages = applyEvent(messages, event("tool_call_started", { message_id: "msg_a", call_id: "c1", tool: "shell.run" }));
    messages = applyEvent(messages, event("approval_requested", { message_id: "msg_a", approval: { id: "apr_1", call_id: "c1", tool: "shell.run", title: "Run", status: "pending" } }));
    messages = applyEvent(messages, event("approval_resolved", { approval_id: "apr_1", status: "denied" }));
    const [tool, approval] = messages[0].blocks;
    assert.equal(approval.approval.status, "denied");
    assert.equal(tool.status, "blocked");
  });

  it("adds an artifact once and carries sources on the message", () => {
    let messages = withAssistant();
    messages = applyEvent(messages, event("artifact_ready", { message_id: "msg_a", artifact: { handle: "art_1", filename: "report.pdf" } }));
    messages = applyEvent(messages, event("artifact_ready", { message_id: "msg_a", artifact: { handle: "art_1", filename: "report.pdf" } }));
    messages = applyEvent(messages, event("sources_ready", { message_id: "msg_a", sources: [{ title: "Example" }], sources_total: 1 }));
    assert.equal(messages[0].blocks.length, 1);
    assert.deepEqual(messages[0].sources, [{ title: "Example" }]);
    assert.equal(messages[0].sources_total, 1);
  });

  it("returns the same array for events it does not own", () => {
    const messages = withAssistant();
    assert.equal(applyEvent(messages, event("usage_updated", { message_id: "msg_a" })), messages);
    assert.equal(applyEvent(messages, event("turn_started", { message_id: "msg_a" })), messages);
  });

  // Safety net: a delta that beats message_created must still be shown.
  it("opens a message for a token that arrives before message_created", () => {
    const messages = withAssistant();
    const next = applyEvent(messages, event("token_delta", { message_id: "msg_b", text: "x" }));
    assert.equal(next.length, 2);
    assert.deepEqual(next[1].blocks, [{ type: "text", text: "x" }]);
  });
});

describe("computer surface", () => {
  const started = (callId, session, extra = {}) => ({
    message_id: "msg_a",
    call_id: callId,
    tool: "computer.session",
    label: "Computer",
    summary: "",
    surface: "computer",
    session,
    ...extra,
  });

  it("carries the session the backend reports", () => {
    const messages = applyEvent(
      withAssistant(),
      event("tool_call_started", started("c1", { id: "sess_1", url: "ws://127.0.0.1:6080/websockify" })),
    );
    const block = messages[0].blocks[0];
    assert.equal(block.surface, "computer");
    assert.deepEqual(block.session, { id: "sess_1", url: "ws://127.0.0.1:6080/websockify" });
  });

  it("keeps the surface on finish, and tolerates a missing url", () => {
    let messages = applyEvent(
      withAssistant(),
      event("tool_call_started", started("c1", { id: "sess_1" })),
    );
    messages = applyEvent(
      messages,
      event("tool_call_finished", { message_id: "msg_a", call_id: "c1", tool: "computer.session", status: "done", duration_ms: 4200 }),
    );
    const block = messages[0].blocks[0];
    assert.equal(block.surface, "computer");
    assert.deepEqual(block.session, { id: "sess_1", url: null });
    assert.equal(block.duration_ms, 4200);
  });

  it("leaves ordinary tools without a surface", () => {
    const messages = applyEvent(
      withAssistant(),
      event("tool_call_started", { message_id: "msg_a", call_id: "c1", tool: "web.search" }),
    );
    const block = messages[0].blocks[0];
    assert.equal(block.surface, null);
    assert.equal(block.session, null);
  });

  it("anchors one card per session at the first call that mentions it", () => {
    let messages = withAssistant();
    messages = applyEvent(messages, event("tool_call_started", started("c1", { id: "sess_1" })));
    messages = applyEvent(messages, event("tool_call_started", started("c2", { id: "sess_1" })));
    messages = applyEvent(messages, event("tool_call_started", started("c3", { id: "sess_2" })));
    messages = applyEvent(
      messages,
      event("tool_call_started", { message_id: "msg_a", call_id: "c4", tool: "web.search" }),
    );
    assert.deepEqual([...computerCardAnchors(messages)].sort(), ["c1", "c3"]);
  });

  it("treats a second session id as a second anchor with its own dial", () => {
    let messages = withAssistant();
    messages = applyEvent(messages, event("tool_call_started", started("c1", { id: "sess_1", url: "ws://127.0.0.1:6080/websockify" })));
    messages = applyEvent(messages, event("tool_call_started", started("c2", { id: "sess_2", url: "ws://127.0.0.1:6081/websockify" })));
    assert.deepEqual([...computerCardAnchors(messages)].sort(), ["c1", "c2"]);
    const urls = messages[0].blocks.filter((b) => b.surface === "computer").map((b) => b.session.url);
    assert.deepEqual(urls, ["ws://127.0.0.1:6080/websockify", "ws://127.0.0.1:6081/websockify"]);
  });

  it("merges a late surface field from an out-of-order event", () => {
    // A `tool_call_finished` that carries surface/session must attach to the
    // block the start created, not orphan or duplicate it.
    let messages = withAssistant();
    messages = applyEvent(messages, event("tool_call_started", { message_id: "msg_a", call_id: "c1", tool: "computer.session" }));
    assert.equal(messages[0].blocks[0].surface, null);
    messages = applyEvent(
      messages,
      event("tool_call_finished", {
        message_id: "msg_a",
        call_id: "c1",
        tool: "computer.session",
        status: "done",
        duration_ms: 4200,
        surface: "computer",
        session: { id: "sess_9", url: "ws://127.0.0.1:6080/websockify" },
      }),
    );
    const [block] = messages[0].blocks;
    assert.equal(messages[0].blocks.length, 1);
    assert.equal(block.surface, "computer");
    assert.deepEqual(block.session, { id: "sess_9", url: "ws://127.0.0.1:6080/websockify" });
    assert.deepEqual([...computerCardAnchors(messages)], ["c1"]);
  });

  it("keeps the earlier surface when the finish carries none", () => {
    let messages = withAssistant();
    messages = applyEvent(messages, event("tool_call_started", started("c1", { id: "sess_1", url: "ws://127.0.0.1:6080/websockify" })));
    messages = applyEvent(
      messages,
      event("tool_call_finished", { message_id: "msg_a", call_id: "c1", tool: "computer.session", status: "done", duration_ms: 100 }),
    );
    const block = messages[0].blocks[0];
    assert.equal(block.surface, "computer");
    assert.deepEqual(block.session, { id: "sess_1", url: "ws://127.0.0.1:6080/websockify" });
  });
});

describe("debug card lifecycle", () => {
  it("injects one card per thread", () => {
    const synthetic = new Map();
    const first = injectDebugCard(synthetic, "thr_1");
    assert.equal(synthetic.get("thr_1").length, 1);
    assert.equal(first.blocks[0].surface, "computer");
    // Re-firing refreshes in place instead of stacking rows.
    const second = injectDebugCard(synthetic, "thr_1");
    const cards = synthetic.get("thr_1");
    assert.equal(cards.length, 1);
    assert.equal(cards[0].id, second.id);
    assert.notEqual(cards[0].id, first.id);
    // A second thread gets its own card.
    injectDebugCard(synthetic, "thr_2");
    assert.equal(synthetic.get("thr_2").length, 1);
  });

  it("dismisses from both the view and the remembered list", () => {
    const synthetic = new Map();
    const message = injectDebugCard(synthetic, "thr_1");
    const view = withSyntheticCards(withAssistant(), synthetic.get("thr_1"));
    assert.equal(view.length, 2);
    const callId = message.blocks[0].call_id;
    const next = dismissCardMessage(view, synthetic.get("thr_1"), callId);
    assert.equal(next.length, 1);
    assert.equal(synthetic.get("thr_1").length, 0);
    // Anchors follow the view: no card, no anchor.
    assert.deepEqual([...computerCardAnchors(next)], []);
  });

  it("re-injects remembered cards when returning to a thread", () => {
    const synthetic = new Map();
    injectDebugCard(synthetic, "thr_1");
    // Leaving the thread drops the view; the snapshot alone has no card.
    const snapshot = withAssistant();
    assert.deepEqual([...computerCardAnchors(snapshot)], []);
    // Coming back merges the remembered synthetic back in.
    const reopened = withSyntheticCards(snapshot, synthetic.get("thr_1"));
    assert.deepEqual([...computerCardAnchors(reopened)].length, 1);
  });

  it("clears remembered cards on sign-out", () => {
    const synthetic = new Map();
    injectDebugCard(synthetic, "thr_1");
    synthetic.clear(); // what signOut does
    const reopened = withSyntheticCards(withAssistant(), synthetic.get("thr_1") ?? []);
    assert.deepEqual([...computerCardAnchors(reopened)], []);
  });

  it("leaves real cards alone when dismissing the debug card", () => {
    let messages = withAssistant();
    messages = applyEvent(messages, event("tool_call_started", {
      message_id: "msg_a",
      call_id: "c1",
      tool: "computer.session",
      label: "Computer",
      surface: "computer",
      session: { id: "sess_real", url: null },
    }));
    const synthetic = new Map();
    const debug = injectDebugCard(synthetic, "thr_1");
    const view = withSyntheticCards(messages, synthetic.get("thr_1"));
    const next = dismissCardMessage(view, synthetic.get("thr_1"), debug.blocks[0].call_id);
    assert.equal(next.length, 1);
    assert.equal(next[0].blocks[0].call_id, "c1");
    assert.deepEqual([...computerCardAnchors(next)], ["c1"]);
  });
});
