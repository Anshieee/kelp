/*
 * Message list. Model text renders as plain text with preserved newlines —
 * never HTML, never Markdown (CHAT_SPEC §12 XSS rules). Tool calls render as
 * collapsed cards; failed/interrupted turns get the spec's copy plus retry.
 */
import { useEffect, useRef } from "react";
import type { Message, MessageBlock, TextBlock, ToolCallBlock } from "../api";

interface MessageListProps {
  messages: Message[];
  activeTurnId: string | null;
  turnError: { code: string; message: string; retryable: boolean; turnId: string | null } | null;
  onRetry: () => void;
}

export default function MessageList({ messages, activeTurnId, turnError, onRetry }: MessageListProps) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [messages.length]);

  useEffect(() => {
    // Keep the tail visible while text streams in, but only if the reader is
    // already near the bottom (don't fight them scrolling up).
    const el = endRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    if (rect.top < window.innerHeight * 1.5) {
      el.scrollIntoView({ block: "end" });
    }
  });

  return (
    <div className="message-list" role="log" aria-live="polite" aria-relevant="additions text">
      {messages.length === 0 && (
        <div className="empty-state">
          <p>Ask about a document, the web, or your repo. Nothing leaves this machine unless a
            tool needs it — and anything that changes something waits for your approval.</p>
        </div>
      )}
      {messages.map((message) => (
        <MessageBubble key={message.id} message={message} working={message.turn_id === activeTurnId} />
      ))}
      {turnError && (
        <div className="turn-error" role="alert">
          <p>{friendlyError(turnError.code, turnError.message)}</p>
          {(turnError.retryable || turnError.code === "turn_budget_exceeded") && (
            <button type="button" className="btn btn-small" onClick={onRetry}>
              {turnError.code === "turn_budget_exceeded" ? "Continue" : "Try again"}
            </button>
          )}
        </div>
      )}
      <div ref={endRef} />
    </div>
  );
}

function MessageBubble({ message, working }: { message: Message; working: boolean }) {
  const isUser = message.role === "user";
  return (
    <article
      className={`message ${isUser ? "message-user" : "message-assistant"}`}
      data-status={message.status}
    >
      {message.blocks.map((block, index) => (
        <BlockView key={index} block={block} />
      ))}
      {!isUser && message.status !== "complete" && (
        <p className="message-status">
          {message.status === "error" && "That didn't work."}
          {message.status === "stopped" && "Stopped"}
          {message.status === "interrupted" && "The server restarted while answering."}
          {message.status === "streaming" && (working ? "Working…" : "")}
        </p>
      )}
      {message.sources && message.sources.length > 0 && (
        <p className="source-row">
          {message.sources.slice(0, 8).map((source, index) => (
            <SourceChipView key={source.id ?? index} source={source} />
          ))}
        </p>
      )}
    </article>
  );
}

function BlockView({ block }: { block: MessageBlock }) {
  switch (block.type) {
    case "text":
      return <p className="message-text">{(block as TextBlock).text}</p>;
    case "tool_call":
      return <ToolCallCardView block={block as ToolCallBlock} />;
    case "attachment": {
      const attachment = block.attachment as { name?: string };
      return <p className="attachment-chip">📎 {attachment.name ?? "attachment"}</p>;
    }
    case "artifact": {
      const artifact = block.artifact as {
        name?: string;
        open_url?: string;
        download_url?: string;
      };
      return (
        <div className="artifact-card">
          <span className="artifact-name">{artifact.name ?? "file"}</span>
          <span className="artifact-links">
            {artifact.open_url && (
              <a href={artifact.open_url} target="_blank" rel="noopener noreferrer">
                Open
              </a>
            )}
            {artifact.download_url && <a href={artifact.download_url}>Download</a>}
          </span>
        </div>
      );
    }
    case "schedule_trigger":
      return (
        <p className="schedule-note">
          Scheduled task{(block as { title?: string }).title ? `: ${(block as { title?: string }).title}` : ""}
        </p>
      );
    default:
      return null;
  }
}

function ToolCallCardView({ block }: { block: ToolCallBlock }) {
  const statusLabel =
    block.status === "running"
      ? "Working"
      : block.status === "waiting_approval"
        ? "Waiting on you"
        : block.status === "done"
          ? "Done"
          : block.status === "failed"
            ? "Failed"
            : block.status === "blocked"
              ? "You said no. Nothing ran."
              : block.status;
  return (
    <details className="tool-card">
      <summary>
        <span className="tool-label">{block.label ?? block.tool}</span>
        {block.summary && <span className="tool-summary">{block.summary}</span>}
        <span className={`tool-status status-${block.status}`}>{statusLabel}</span>
        {typeof block.duration_ms === "number" && (
          <span className="tool-duration">{formatDuration(block.duration_ms)}</span>
        )}
      </summary>
      {block.effects && block.effects.length > 0 && (
        <p className="tool-effects">
          {block.effects.map((effect) => (
            <span key={effect} className="effect-chip">
              {effect}
            </span>
          ))}
        </p>
      )}
      <p className="tool-call-id">{block.call_id}</p>
    </details>
  );
}

function SourceChipView({ source }: { source: { kind?: string; title?: string; url?: string; status?: string } }) {
  if (source.status === "failed") {
    return <span className="source-chip failed">{source.title ?? "Source failed"}</span>;
  }
  const url = source.url;
  if (url && /^https?:\/\//.test(url)) {
    let label = url;
    try {
      label = new URL(url).hostname;
    } catch {
      // keep full url as label
    }
    return (
      <a className="source-chip" href={url} target="_blank" rel="noopener noreferrer nofollow">
        {source.title ?? label}
      </a>
    );
  }
  return <span className="source-chip">{source.title ?? "source"}</span>;
}

function friendlyError(code: string, message: string): string {
  if (code === "turn_budget_exceeded") return "That answer got too long. Ask a narrower question, or continue from where it stopped.";
  if (code === "provider_timeout") return "The model took too long to answer.";
  if (code === "provider_rate_limited") return "The model service is busy.";
  if (code === "rate_limited") return "Too many requests. Wait a moment.";
  return message || "Something went wrong.";
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}
