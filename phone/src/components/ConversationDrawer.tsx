/*
 * Slide-over conversation drawer (netlify/mobile sidebar pattern, adapted).
 *
 * - Full-height slide-over on phone widths; never a permanent sidebar.
 * - Backdrop click and Escape close it.
 * - On open, focus moves to the close button; on close, focus is restored to
 *   whatever held it before (typically the hamburger that opened it).
 * - Pure presentation: thread data lives in ThreadListPanel.
 */
import { useEffect, useRef, type ReactNode } from "react";

interface ConversationDrawerProps {
  open: boolean;
  onClose: () => void;
  /** Element id referenced by the opener's aria-controls. */
  id?: string;
  label?: string;
  children: ReactNode;
}

export default function ConversationDrawer({
  open,
  onClose,
  id = "conv-drawer",
  label = "Conversations",
  children,
}: ConversationDrawerProps) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const restoreRef = useRef<Element | null>(null);

  useEffect(() => {
    if (!open) return;
    // Remember the opener so focus can be restored on close.
    restoreRef.current = document.activeElement;
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    document.body.classList.add("drawer-open");
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.classList.remove("drawer-open");
      const restore = restoreRef.current;
      if (restore instanceof HTMLElement) restore.focus();
    };
  }, [open, onClose]);

  return (
    <>
      <button
        type="button"
        className="drawer-backdrop"
        aria-label="Close conversations"
        hidden={!open}
        onClick={onClose}
        tabIndex={open ? 0 : -1}
      />
      <aside
        id={id}
        className="conv-drawer"
        aria-label={label}
        aria-hidden={!open}
        data-open={open ? "true" : "false"}
      >
        <div className="drawer-heading">
          <span className="brand">
            <span className="kelp-avatar small" aria-hidden="true">
              <i />
              <i />
            </span>
            Kelp
          </span>
          <button
            ref={closeRef}
            type="button"
            className="icon-btn"
            aria-label="Close conversations"
            onClick={onClose}
          >
            ×
          </button>
        </div>
        {children}
      </aside>
    </>
  );
}
