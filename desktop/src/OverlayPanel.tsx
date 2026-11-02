import { useEffect, useRef, useState } from "react";
import {
  Avatar,
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
import CloseRoundedIcon from "@mui/icons-material/CloseRounded";
import ExpandMoreRoundedIcon from "@mui/icons-material/ExpandMoreRounded";
import MicRoundedIcon from "@mui/icons-material/MicRounded";
import OpenInNewRoundedIcon from "@mui/icons-material/OpenInNewRounded";
import ScreenshotMonitorRoundedIcon from "@mui/icons-material/ScreenshotMonitorRounded";
import SmartToyRoundedIcon from "@mui/icons-material/SmartToyRounded";

import { cannedReply } from "./mock";
import { hideOverlay, openFullApp, resizeOverlay } from "./tauri";
import type { ChatMessage } from "./types";

const COMPACT = { width: 640, height: 88 };
const EXPANDED = { width: 760, height: 420 };

let nextId = 0;
const uid = () => `overlay-${++nextId}`;

export default function Overlay() {
  const [expanded, setExpanded] = useState(false);
  const [draft, setDraft] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [screenNote, setScreenNote] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      if (screenNote) {
        setScreenNote(false);
        return;
      }
      if (expanded) {
        setExpanded(false);
        void resizeOverlay(COMPACT.width, COMPACT.height);
        return;
      }
      void hideOverlay();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [expanded, screenNote]);

  function grow() {
    if (expanded) return;
    setExpanded(true);
    void resizeOverlay(EXPANDED.width, EXPANDED.height);
  }

  function shrink() {
    setExpanded(false);
    void resizeOverlay(COMPACT.width, COMPACT.height);
  }

  function send() {
    const trimmed = draft.trim();
    if (!trimmed) return;
    grow();
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
        display: "flex",
        flexDirection: "column",
        gap: 1,
        p: 1,
        boxSizing: "border-box",
      }}
    >
      <Paper
        elevation={8}
        sx={{
          display: "flex",
          alignItems: "center",
          gap: 1,
          px: 1.5,
          py: 0.75,
          borderRadius: 999,
          border: "1px solid rgba(25,24,23,0.10)",
        }}
      >
        <Tooltip title={expanded ? "Collapse" : "Expand"}>
          <Avatar
            onClick={expanded ? shrink : grow}
            sx={{
              width: 34,
              height: 34,
              bgcolor: "primary.main",
              cursor: "pointer",
            }}
          >
            <SmartToyRoundedIcon sx={{ fontSize: 20 }} />
          </Avatar>
        </Tooltip>

        <InputBase
          inputRef={inputRef}
          value={draft}
          onChange={(event) => {
            setDraft(event.target.value);
            if (event.target.value) grow();
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              send();
            }
          }}
          placeholder="Ask Kelp…"
          sx={{ flex: 1, fontSize: 15 }}
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
          elevation={8}
          sx={{
            flex: 1,
            minHeight: 0,
            display: "flex",
            flexDirection: "column",
            borderRadius: 3,
            border: "1px solid rgba(25,24,23,0.10)",
            overflow: "hidden",
          }}
        >
          <Box sx={{ flex: 1, minHeight: 0, overflowY: "auto", p: 2 }}>
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
                      maxWidth: "82%",
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
            alignItems="center"
            spacing={1}
            sx={{ px: 2, py: 1, borderTop: "1px solid rgba(25,24,23,0.08)" }}
          >
            <Button
              size="small"
              startIcon={<OpenInNewRoundedIcon />}
              onClick={() => void openFullApp()}
            >
              Open full app
            </Button>
            <Box sx={{ flex: 1 }} />
            <Tooltip title="Close">
              <IconButton size="small" onClick={shrink} aria-label="Collapse">
                <ExpandMoreRoundedIcon fontSize="small" />
              </IconButton>
            </Tooltip>
            <Tooltip title="Hide">
              <IconButton size="small" onClick={() => void hideOverlay()} aria-label="Hide">
                <CloseRoundedIcon fontSize="small" />
              </IconButton>
            </Tooltip>
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
