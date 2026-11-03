import { useEffect, useRef, useState } from "react";
import {
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  IconButton,
  InputBase,
  Paper,
  Stack,
  Typography,
} from "@mui/material";
import ArrowUpwardRoundedIcon from "@mui/icons-material/ArrowUpwardRounded";
import KeyboardArrowDownRoundedIcon from "@mui/icons-material/KeyboardArrowDownRounded";
import MicRoundedIcon from "@mui/icons-material/MicRounded";
import OpenInFullRoundedIcon from "@mui/icons-material/OpenInFullRounded";
import OpenInNewRoundedIcon from "@mui/icons-material/OpenInNewRounded";
import ScreenshotMonitorRoundedIcon from "@mui/icons-material/ScreenshotMonitorRounded";
import StopRoundedIcon from "@mui/icons-material/StopRounded";

import { MessageBody } from "./Blocks";
import { type Message } from "./bridge";
import { useChat } from "./useChat";
import { hideOverlay, onOverlayFocus, openFullApp, resizeOverlay } from "./tauri";

// The window is exactly this wide in both states; only the height changes.
const WIDTH = 400;
const COMPACT = { width: WIDTH, height: 76 };
const EXPANDED = { width: WIDTH, height: 440 };

export default function OverlayPanel() {
  const chat = useChat();
  const [expanded, setExpanded] = useState(false);
  const [draft, setDraft] = useState("");
  const [screenNote, setScreenNote] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const resumed = useRef(false);

  const ready = chat.phase === "ready";

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Being summoned always resets to the compact bar: Rust has already resized
  // the window, so the React state has to follow. Hiding is Rust's job — it can
  // tell whether the orb took focus, which avoids the hide/re-show race. The
  // summon also re-checks the session, so a sign-in done in the full app lands
  // here without restarting the overlay.
  useEffect(
    () =>
      onOverlayFocus((focused) => {
        if (!focused) return;
        setExpanded(false);
        void chat.startSession();
      }),
    [chat.startSession],
  );

  // Continue the most recent conversation so history is there when the panel
  // opens. A brand-new thread is only created once the user actually sends.
  useEffect(() => {
    if (!ready || resumed.current) return;
    const latest = chat.threads[0];
    if (!latest) return;
    resumed.current = true;
    void chat.openThread(latest.id);
  }, [ready, chat.threads, chat.openThread]);

  function collapse() {
    setExpanded(false);
    void resizeOverlay(COMPACT.width, COMPACT.height);
  }

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      if (screenNote) {
        setScreenNote(false);
        return;
      }
      if (expanded) {
        collapse();
        return;
      }
      void hideOverlay();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [expanded, screenNote]);

  function expand() {
    if (expanded) {
      void openFullApp();
      return;
    }
    setExpanded(true);
    void resizeOverlay(EXPANDED.width, EXPANDED.height);
  }

  async function send() {
    const trimmed = draft.trim();
    if (!trimmed || !ready) return;
    if (!expanded) expand();
    setDraft("");
    if (!chat.activeId) await chat.newChat();
    await chat.send(trimmed);
  }

  const canSend = ready && Boolean(draft.trim()) && !chat.busy;

  return (
    <Box
      sx={{
        height: "100%",
        boxSizing: "border-box",
        p: "6px",
        display: "flex",
        flexDirection: "column",
        gap: "8px",
        background: "transparent",
      }}
    >
      <Paper
        elevation={6}
        sx={{
          display: "flex",
          alignItems: "center",
          gap: 0.5,
          height: 64,
          px: 1,
          borderRadius: 999,
          flexShrink: 0,
        }}
      >
        <InputBase
          inputRef={inputRef}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              if (canSend) void send();
            }
          }}
          placeholder={ready ? "Ask Kelp…" : "Sign in from the full app"}
          disabled={!ready}
          // Text sits at 6 (window) + 8 (pill) + 8 (input) = 22px from the
          // window edge, matching the expanded panel's 6 + 16 = 22px.
          sx={{ flex: 1, fontSize: 15, px: 1 }}
        />
        <IconButton
          size="small"
          title="Screen access — not wired yet"
          aria-label="Screen access — not wired yet"
          onClick={() => setScreenNote(true)}
        >
          <ScreenshotMonitorRoundedIcon fontSize="small" />
        </IconButton>
        <IconButton
          size="small"
          title="Voice — not wired yet"
          aria-label="Voice — not wired yet"
        >
          <MicRoundedIcon fontSize="small" />
        </IconButton>
        <IconButton
          size="small"
          title={expanded ? "Open the full app" : "Expand"}
          onClick={expand}
          aria-label={expanded ? "Open the full app" : "Expand"}
        >
          {expanded ? (
            <OpenInNewRoundedIcon fontSize="small" />
          ) : (
            <OpenInFullRoundedIcon fontSize="small" />
          )}
        </IconButton>
        {chat.busy ? (
          <IconButton
            size="small"
            aria-label="Stop"
            onClick={() => void chat.stop()}
            sx={{ background: "rgba(25,24,23,0.08)" }}
          >
            <StopRoundedIcon fontSize="small" />
          </IconButton>
        ) : (
          <IconButton
            size="small"
            aria-label="Send"
            onClick={() => void send()}
            disabled={!canSend}
            sx={{
              background: "primary.main",
              color: "primary.contrastText",
              "&:hover": { background: "primary.dark" },
              "&.Mui-disabled": { background: "rgba(25,24,23,0.08)" },
            }}
          >
            <ArrowUpwardRoundedIcon fontSize="small" />
          </IconButton>
        )}
      </Paper>

      {expanded && (
        <Paper
          elevation={6}
          sx={{
            flex: 1,
            minHeight: 0,
            display: "flex",
            flexDirection: "column",
            borderRadius: 3,
            overflow: "hidden",
          }}
        >
          <Stack
            direction="row"
            alignItems="center"
            // py:0.5 (4px) left the title and the collapse chevron sitting on
            // the panel's rounded corner; 8px gives the header real room.
            sx={{ px: 2, py: 1, borderBottom: "1px solid rgba(25,24,23,0.08)" }}
          >
            <Typography
              variant="caption"
              sx={{
                flex: 1,
                fontWeight: 600,
                letterSpacing: "0.04em",
                textTransform: "uppercase",
                color: "text.secondary",
              }}
            >
              Kelp
            </Typography>
            <IconButton
              size="small"
              onClick={collapse}
              title="Collapse"
              aria-label="Collapse"
            >
              <KeyboardArrowDownRoundedIcon fontSize="small" />
            </IconButton>
          </Stack>

          {!ready ? (
            <Stack sx={{ flex: 1, p: 2 }} spacing={1.5} justifyContent="center">
              <Typography variant="body2" color="text.secondary">
                {chat.phase === "loading"
                  ? "Connecting to the Kelp server…"
                  : chat.failure?.message ?? "Sign in from the full app to chat here."}
              </Typography>
              <Button
                size="small"
                variant="contained"
                startIcon={<OpenInNewRoundedIcon />}
                onClick={() => void openFullApp()}
              >
                Open full app
              </Button>
            </Stack>
          ) : (
            <Box sx={{ flex: 1, minHeight: 0, overflowY: "auto", px: 2, py: 1.5 }}>
              {chat.messages.length === 0 ? (
                <Typography variant="body2" color="text.secondary">
                  Ask anything — I'll pick it up from wherever we left off.
                </Typography>
              ) : (
                <Stack spacing={1.5}>
                  {chat.messages.map((message) => (
                    <MessageBubble key={message.id} message={message} onDecide={chat.decide} />
                  ))}
                </Stack>
              )}
            </Box>
          )}

          <Stack
            direction="row"
            sx={{ px: 2, py: 1, borderTop: "1px solid rgba(25,24,23,0.08)" }}
          >
            <Button
              size="small"
              startIcon={<OpenInNewRoundedIcon />}
              onClick={() => void openFullApp()}
            >
              Open full app
            </Button>
          </Stack>
        </Paper>
      )}

      <Dialog open={screenNote} onClose={() => setScreenNote(false)} maxWidth="xs">
        <DialogTitle>Screen access isn’t connected yet</DialogTitle>
        <DialogContent>
          <DialogContentText>
            When this lands, pressing it will take a single screenshot and
            attach it to your message — one frame, only when you ask. Nothing is
            captured in the background.
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setScreenNote(false)}>Got it</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}

function MessageBubble({
  message,
  onDecide,
}: {
  message: Message;
  onDecide: (id: string, decision: "approve" | "deny") => void;
}) {
  const mine = message.role === "user";
  const renderable =
    message.blocks.some((block) => (block.type === "text" ? Boolean(block.text) : true)) ||
    (message.sources?.length ?? 0) > 0;
  return (
    <Box
      sx={{
        alignSelf: mine ? "flex-end" : "flex-start",
        maxWidth: "84%",
        px: 1.5,
        py: 1,
        borderRadius: 3,
        background: mine ? "rgba(214,232,216,0.8)" : "transparent",
      }}
    >
      {renderable ? (
        <MessageBody message={message} onDecide={onDecide} />
      ) : (
        <Stack direction="row" spacing={0.5} alignItems="center">
          <CircularProgress size={12} />
          <Typography variant="caption" color="text.secondary">
            thinking…
          </Typography>
        </Stack>
      )}
    </Box>
  );
}
