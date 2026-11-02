import { useEffect, useRef, useState } from "react";
import {
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  IconButton,
  InputBase,
  Paper,
  Stack,
  Tooltip,
  Typography,
} from "@mui/material";
import ArrowUpwardRoundedIcon from "@mui/icons-material/ArrowUpwardRounded";
import KeyboardArrowDownRoundedIcon from "@mui/icons-material/KeyboardArrowDownRounded";
import MicRoundedIcon from "@mui/icons-material/MicRounded";
import OpenInFullRoundedIcon from "@mui/icons-material/OpenInFullRounded";
import OpenInNewRoundedIcon from "@mui/icons-material/OpenInNewRounded";
import ScreenshotMonitorRoundedIcon from "@mui/icons-material/ScreenshotMonitorRounded";

import { cannedReply } from "./mock";
import { hideOverlay, onFocusLost, openFullApp, resizeOverlay } from "./tauri";
import type { ChatMessage } from "./types";

// The window is exactly this wide in both states; only the height changes.
const WIDTH = 400;
const COMPACT = { width: WIDTH, height: 76 };
const EXPANDED = { width: WIDTH, height: 440 };

let nextId = 0;
const uid = () => `overlay-${++nextId}`;

export default function OverlayPanel() {
  const [expanded, setExpanded] = useState(false);
  const [draft, setDraft] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [screenNote, setScreenNote] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Outside click: the panel goes away entirely, not back to the bar.
  useEffect(
    () =>
      onFocusLost(() => {
        setExpanded(false);
        void hideOverlay();
      }),
    [],
  );

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

  function send() {
    const trimmed = draft.trim();
    if (!trimmed) return;
    if (!expanded) expand();
    setMessages((current) => [
      ...current,
      { id: uid(), role: "user", text: trimmed },
      { id: uid(), role: "assistant", text: cannedReply(trimmed) },
    ]);
    setDraft("");
  }

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
              send();
            }
          }}
          placeholder="Ask Kelp…"
          sx={{ flex: 1, fontSize: 15, px: 1.5 }}
        />
        <Tooltip title="Screen access — not wired yet">
          <IconButton size="small" onClick={() => setScreenNote(true)}>
            <ScreenshotMonitorRoundedIcon fontSize="small" />
          </IconButton>
        </Tooltip>
        <Tooltip title="Voice — not wired yet">
          <IconButton size="small">
            <MicRoundedIcon fontSize="small" />
          </IconButton>
        </Tooltip>
        <Tooltip title={expanded ? "Open the full app" : "Expand"}>
          <IconButton
            size="small"
            onClick={expand}
            aria-label={expanded ? "Open the full app" : "Expand"}
          >
            {expanded ? (
              <OpenInNewRoundedIcon fontSize="small" />
            ) : (
              <OpenInFullRoundedIcon fontSize="small" />
            )}
          </IconButton>
        </Tooltip>
        <IconButton
          size="small"
          aria-label="Send"
          onClick={send}
          disabled={!draft.trim()}
          sx={{
            background: "primary.main",
            color: "primary.contrastText",
            "&:hover": { background: "primary.dark" },
            "&.Mui-disabled": { background: "rgba(25,24,23,0.08)" },
          }}
        >
          <ArrowUpwardRoundedIcon fontSize="small" />
        </IconButton>
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
            sx={{ px: 2, py: 0.5, borderBottom: "1px solid rgba(25,24,23,0.08)" }}
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
            <Tooltip title="Collapse">
              <IconButton size="small" onClick={collapse} aria-label="Collapse">
                <KeyboardArrowDownRoundedIcon fontSize="small" />
              </IconButton>
            </Tooltip>
          </Stack>

          <Box sx={{ flex: 1, minHeight: 0, overflowY: "auto", px: 2, py: 1.5 }}>
            {messages.length === 0 ? (
              <Typography variant="body2" color="text.secondary">
                Type to chat. Replies are canned in this preview.
              </Typography>
            ) : (
              <Stack spacing={1.5}>
                {messages.map((message) => (
                  <Box
                    key={message.id}
                    sx={{
                      alignSelf:
                        message.role === "user" ? "flex-end" : "flex-start",
                      maxWidth: "84%",
                      px: 1.5,
                      py: 1,
                      borderRadius: 3,
                      background:
                        message.role === "user"
                          ? "rgba(214,232,216,0.8)"
                          : "transparent",
                    }}
                  >
                    <Typography variant="body2" sx={{ whiteSpace: "pre-wrap" }}>
                      {message.text}
                    </Typography>
                  </Box>
                ))}
              </Stack>
            )}
          </Box>

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
