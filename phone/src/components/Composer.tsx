/*
 * Bottom composer: auto-growing textarea, send, and stop-while-working.
 * Enter inserts a newline (phone keyboard); send is the button.
 */
import { useRef, useState, type KeyboardEvent } from "react";

const MAX_CHARS = 8000;

interface ComposerProps {
  /** True while a turn is running: send is disabled, Stop is shown. */
  disabled: boolean;
  onSend: (text: string) => Promise<void>;
  onStop: () => void;
}

export default function Composer({ disabled, onSend, onStop }: ComposerProps) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const trimmed = text.trim();
  const canSend = !disabled && !sending && trimmed.length > 0 && trimmed.length <= MAX_CHARS;

  const submit = async () => {
    if (!canSend) return;
    setSending(true);
    try {
      await onSend(trimmed);
      setText("");
      requestAnimationFrame(() => textareaRef.current?.focus());
    } finally {
      setSending(false);
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      void submit();
    }
  };

  const nearLimit = trimmed.length > MAX_CHARS - 200;

  return (
    <div className="composer">
      <div className="composer-row">
        <textarea
          ref={textareaRef}
          className="composer-input"
          placeholder={disabled ? "Working…" : "Message Kelp"}
          value={text}
          rows={1}
          maxLength={MAX_CHARS}
          disabled={disabled}
          onChange={(event) => {
            setText(event.target.value);
            const el = event.target;
            el.style.height = "auto";
            el.style.height = `${Math.min(el.scrollHeight, 132)}px`;
          }}
          onKeyDown={onKeyDown}
          aria-label="Message"
        />
        {disabled ? (
          <button type="button" className="btn btn-stop" onClick={onStop} aria-label="Stop answering">
            ■
          </button>
        ) : (
          <button
            type="button"
            className="btn btn-send"
            disabled={!canSend}
            onClick={() => void submit()}
            aria-label="Send message"
          >
            {sending ? "…" : "↑"}
          </button>
        )}
      </div>
      {nearLimit && (
        <p className={trimmed.length > MAX_CHARS ? "char-count over" : "char-count"}>
          {trimmed.length}/{MAX_CHARS}
        </p>
      )}
    </div>
  );
}
