import { useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Avatar,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  Divider,
  Drawer,
  IconButton,
  InputBase,
  List,
  ListItemButton,
  ListItemText,
  ListSubheader,
  Stack,
  Tab,
  Tabs,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import AddRoundedIcon from "@mui/icons-material/AddRounded";
import ArrowUpwardRoundedIcon from "@mui/icons-material/ArrowUpwardRounded";
import AttachFileRoundedIcon from "@mui/icons-material/AttachFileRounded";
import MicRoundedIcon from "@mui/icons-material/MicRounded";
import MoreHorizRoundedIcon from "@mui/icons-material/MoreHorizRounded";
import ReplayRoundedIcon from "@mui/icons-material/ReplayRounded";
import ScreenshotMonitorRoundedIcon from "@mui/icons-material/ScreenshotMonitorRounded";
import SearchRoundedIcon from "@mui/icons-material/SearchRounded";
import SettingsRoundedIcon from "@mui/icons-material/SettingsRounded";
import SmartToyRoundedIcon from "@mui/icons-material/SmartToyRounded";
import StopRoundedIcon from "@mui/icons-material/StopRounded";

import { MessageBody, MessagePlaceholder, type ComputerCardHost } from "./Blocks";
import ComputerPane from "./ComputerPane";
import { computerCardAnchors } from "./chatEvents";
import { buttonStatusLabel } from "./computerViewer";
import { type Message, type Thread } from "./bridge";
import { useChat } from "./useChat";
import { useViewer } from "./useViewer";

const DRAWER_WIDTH = 280;

const SUGGESTIONS = [
  "Sort my inbox and show me what needs a reply",
  "Turn this report into a one-page brief",
  "Follow up on an unpaid invoice, politely",
  "Plan my week around three deadlines",
];

const DAY = 86_400_000;

function groupByDay(threads: Thread[]) {
  const startOfToday = new Date();
  startOfToday.setHours(0, 0, 0, 0);
  const buckets = new Map<string, Thread[]>();
  for (const thread of threads) {
    const updated = new Date(thread.updated_at).getTime();
    const age = startOfToday.getTime() - updated;
    const label =
      updated >= startOfToday.getTime()
        ? "Today"
        : age < DAY
          ? "Yesterday"
          : age < 7 * DAY
            ? "Previous 7 days"
            : "Older";
    const bucket = buckets.get(label) ?? [];
    bucket.push(thread);
    buckets.set(label, bucket);
  }
  return ["Today", "Yesterday", "Previous 7 days", "Older"]
    .filter((label) => buckets.has(label))
    .map((label) => ({ label, threads: buckets.get(label)! }));
}

export default function App() {
  const chat = useChat();
  const viewer = useViewer({ enabled: chat.phase === "ready" });
  const [draft, setDraft] = useState("");
  const [bearer, setBearer] = useState("");
  const [askSignIn, setAskSignIn] = useState(false);
  // The Computer panel is a toggle on top of an always-mounted viewer: closing
  // it hides the surface but keeps the RFB session, WebSocket, and framebuffer
  // warm in the background.
  const [computerOpen, setComputerOpen] = useState(false);

  const groups = useMemo(() => groupByDay(chat.threads), [chat.threads]);

  // One card per session, anchored to the first call that mentioned it.
  const cardAnchors = useMemo(() => computerCardAnchors(chat.messages), [chat.messages]);
  const computerCards = useMemo<ComputerCardHost>(
    () => ({
      viewer: {
        status: viewer.status,
        activeSessionId: viewer.activeSessionId,
        hostRef: viewer.hostRef,
        openSessionUrl: viewer.openSessionUrl,
      },
      anchors: cardAnchors,
      onOpen: () => setComputerOpen(true),
      onDismiss: chat.dismissComputerCard,
    }),
    [viewer.status, viewer.activeSessionId, viewer.hostRef, viewer.openSessionUrl, cardAnchors, chat.dismissComputerCard],
  );

  // Debug scaffold: fabricate a computer card so the inline preview can be
  // exercised before the backend emits `surface: "computer"`. Gated out of
  // production, and the block it injects never reaches the server.
  const chatRef = useRef(chat);
  chatRef.current = chat;
  useEffect(() => {
    if (!import.meta.env.DEV) return;
    let disposed = false;
    let stop: (() => void) | undefined;
    const fire = async () => {
      const current = chatRef.current;
      if (current.phase !== "ready") return;
      if (!current.activeId) await current.newChat();
      current.injectComputerCard();
    };
    const onKey = (event: KeyboardEvent) => {
      const combo = (event.metaKey || event.ctrlKey) && event.shiftKey;
      if (combo && event.key.toLowerCase() === "k") {
        event.preventDefault();
        void fire();
      }
    };
    window.addEventListener("keydown", onKey);
    void (async () => {
      try {
        const { listen } = await import("@tauri-apps/api/event");
        const off = await listen("debug-computer-card", () => void fire());
        // StrictMode mounts twice: a registration that resolves after cleanup
        // must unregister itself, or one event fires the handler twice.
        if (disposed) off();
        else stop = off;
      } catch {
        /* plain browser: there is no command bridge to listen on */
      }
    })();
    return () => {
      disposed = true;
      window.removeEventListener("keydown", onKey);
      stop?.();
    };
  }, []);

  const submit = () => {
    void chat.send(draft);
    setDraft("");
  };

  const canSend = Boolean(chat.activeId) && Boolean(draft.trim()) && !chat.busy;

  return (
    <Box sx={{ display: "flex", height: "100%" }}>
      <Drawer
        variant="permanent"
        sx={{
          width: DRAWER_WIDTH,
          flexShrink: 0,
          "& .MuiDrawer-paper": {
            width: DRAWER_WIDTH,
            boxSizing: "border-box",
            borderRight: "1px solid rgba(25,24,23,0.08)",
            background: "#F6F3EC",
            display: "flex",
            flexDirection: "column",
          },
        }}
      >
        <Stack direction="row" alignItems="center" spacing={1} sx={{ px: 2, pt: 2, pb: 1 }}>
          <Typography variant="h6" sx={{ letterSpacing: "-0.03em" }}>
            kelp
          </Typography>
        </Stack>

        <Box sx={{ px: 1.5, pb: 1 }}>
          <Button
            fullWidth
            startIcon={<AddRoundedIcon />}
            onClick={() => void chat.newChat()}
            disabled={chat.phase !== "ready"}
            sx={{ justifyContent: "flex-start", color: "text.primary" }}
          >
            New chat
          </Button>
        </Box>

        <Box sx={{ flex: 1, overflowY: "auto", px: 1 }}>
          {chat.phase === "loading" ? (
            <Stack sx={{ alignItems: "center", pt: 3 }}>
              <CircularProgress size={20} />
            </Stack>
          ) : groups.length === 0 ? (
            <Typography variant="body2" color="text.secondary" sx={{ px: 1.5, pt: 1 }}>
              No conversations yet.
            </Typography>
          ) : (
            groups.map((group) => (
              <List
                key={group.label}
                dense
                subheader={
                  <ListSubheader
                    disableSticky
                    sx={{
                      background: "transparent",
                      fontSize: 11,
                      fontWeight: 600,
                      letterSpacing: "0.06em",
                      textTransform: "uppercase",
                      color: "text.secondary",
                    }}
                  >
                    {group.label}
                  </ListSubheader>
                }
              >
                {group.threads.map((thread) => (
                  <ListItemButton
                    key={thread.id}
                    selected={thread.id === chat.activeId}
                    onClick={() => void chat.openThread(thread.id)}
                    sx={{ borderRadius: 2, mb: 0.25 }}
                  >
                    <ListItemText
                      primary={thread.title || "Untitled"}
                      primaryTypographyProps={{ noWrap: true, fontSize: 14 }}
                    />
                  </ListItemButton>
                ))}
              </List>
            ))
          )}
        </Box>

        <Divider />
        <Box sx={{ p: 1.5 }}>
          <Button
            fullWidth
            startIcon={<SettingsRoundedIcon />}
            onClick={() => {
              // The viewer's socket must not outlive the session it belongs to.
              viewer.disconnect();
              void chat.signOut();
            }}
            sx={{ justifyContent: "flex-start", color: "text.secondary" }}
          >
            Sign out
          </Button>
        </Box>
      </Drawer>

      <Box sx={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
        <Stack
          direction="row"
          alignItems="center"
          spacing={1}
          sx={{ px: 3, py: 1.5, borderBottom: "1px solid rgba(25,24,23,0.08)" }}
        >
          <Tabs
            value={computerOpen ? "computer" : "chat"}
            onChange={(_, value: "chat" | "computer") => {
              if (value === "chat") setComputerOpen(false);
            }}
            sx={{ minHeight: 32, mr: 1 }}
          >
            <Tab value="chat" label="Chat" sx={{ minHeight: 32, py: 0, px: 1.5 }} />
            <Tab
              value="computer"
              // MUI skips onChange for the already-selected tab; the toggle
              // lives here so a second click closes the panel.
              onClick={() => setComputerOpen((open) => !open)}
              sx={{ minHeight: 32, py: 0, px: 1.5 }}
              label={
                <Stack component="span" direction="row" spacing={0.75} alignItems="baseline">
                  <span>Computer</span>
                  <Typography
                    component="span"
                    variant="caption"
                    sx={{ color: "text.secondary", fontSize: 11, lineHeight: 1.2 }}
                  >
                    {buttonStatusLabel(viewer.status)}
                  </Typography>
                </Stack>
              }
            />
          </Tabs>
          {!computerOpen ? (
            <Typography variant="subtitle1" noWrap sx={{ flex: 1, fontWeight: 600 }}>
              {chat.activeThread?.title ?? "New chat"}
            </Typography>
          ) : (
            <Box sx={{ flex: 1 }} />
          )}
          {!computerOpen && (
            <>
              <Typography variant="caption" color="text.secondary">
                {chat.streamState === "open" ? "live" : chat.streamState}
              </Typography>
              <Tooltip title="Search">
                <IconButton size="small">
                  <SearchRoundedIcon fontSize="small" />
                </IconButton>
              </Tooltip>
              <Tooltip title="More">
                <IconButton size="small">
                  <MoreHorizRoundedIcon fontSize="small" />
                </IconButton>
              </Tooltip>
            </>
          )}
        </Stack>

        {/* Panel toggle. The chat surface renders only while the Computer
            panel is closed; the Computer panel below is always mounted. */}
        {!computerOpen && (
          <>
        {chat.failure && (
          <Alert
            severity="error"
            sx={{ mx: 3, mt: 2 }}
            action={
              <Stack direction="row" spacing={1}>
                <Button color="inherit" size="small" onClick={() => void chat.refreshThreads()}>
                  Retry
                </Button>
                <Button color="inherit" size="small" onClick={() => setAskSignIn(true)}>
                  Sign in
                </Button>
              </Stack>
            }
          >
            {chat.failure.message}
          </Alert>
        )}

        <Box sx={{ flex: 1, overflowY: "auto", px: 3, py: 3 }}>
          {chat.messages.length > 0 ? (
            <Stack spacing={2.5} sx={{ maxWidth: 760, mx: "auto" }}>
              {chat.messages.map((message) => (
                <MessageRow
                  key={message.id}
                  message={message}
                  onDecide={chat.decide}
                  computer={computerCards}
                />
              ))}
            </Stack>
          ) : (
            <Stack sx={{ height: "100%", alignItems: "center", justifyContent: "center" }} spacing={3}>
              <Typography variant="h4" align="center">
                What can I help with?
              </Typography>
              <Stack
                direction="row"
                useFlexGap
                flexWrap="wrap"
                justifyContent="center"
                spacing={1}
                sx={{ maxWidth: 620 }}
              >
                {SUGGESTIONS.map((suggestion) => (
                  <Chip
                    key={suggestion}
                    label={suggestion}
                    variant="outlined"
                    disabled={!chat.activeId || chat.busy}
                    onClick={() => {
                      setDraft(suggestion);
                    }}
                  />
                ))}
              </Stack>
            </Stack>
          )}
        </Box>

        <Box sx={{ px: 3, pb: 3, pt: 1 }}>
          <Stack
            direction="row"
            alignItems="flex-end"
            spacing={1}
            sx={{
              maxWidth: 760,
              mx: "auto",
              border: "1px solid rgba(25,24,23,0.12)",
              borderRadius: 4,
              px: 1.5,
              py: 1,
              background: "#FFFFFF",
            }}
          >
            <Tooltip title="Attach a file">
              <IconButton size="small" disabled>
                <AttachFileRoundedIcon fontSize="small" />
              </IconButton>
            </Tooltip>
            <InputBase
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  if (canSend) submit();
                }
              }}
              placeholder={chat.activeId ? "Ask Kelp…" : "Start a new chat first"}
              disabled={!chat.activeId}
              multiline
              maxRows={6}
              sx={{ flex: 1, py: 0.75 }}
            />
            <Tooltip title="Screen access — not wired yet">
              <IconButton size="small">
                <ScreenshotMonitorRoundedIcon fontSize="small" />
              </IconButton>
            </Tooltip>
            <Tooltip title="Voice — not wired yet">
              <IconButton size="small">
                <MicRoundedIcon fontSize="small" />
              </IconButton>
            </Tooltip>
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
                onClick={submit}
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
          </Stack>

          {chat.turnStatus &&
            ["error", "stopped", "interrupted"].includes(chat.turnStatus) && (
              <Stack direction="row" justifyContent="center" sx={{ mt: 1 }}>
                <Button size="small" startIcon={<ReplayRoundedIcon />} onClick={() => void chat.retry()}>
                  Try again
                </Button>
              </Stack>
            )}

          <Typography variant="caption" align="center" display="block" sx={{ mt: 1, color: "text.secondary" }}>
            Connected to the Kelp server through the desktop bridge.
          </Typography>
        </Box>
          </>
        )}

        {/* Mounted once, for the app's whole lifetime: hiding the panel must
            preserve the RFB session, WebSocket, and framebuffer. Hidden means
            display:none + inert — out of the tab order, with no pointer or
            keyboard capture. The viewer itself is view-only regardless. */}
        <Box
          inert={!computerOpen}
          sx={{ display: computerOpen ? "flex" : "none", flex: 1, minHeight: 0 }}
        >
          <ComputerPane viewer={viewer} />
        </Box>
      </Box>

      <Dialog open={chat.phase === "signed-out" || askSignIn} fullWidth maxWidth="xs">
        <DialogTitle>Sign in</DialogTitle>
        <DialogContent>
          <DialogContentText sx={{ mb: 2 }}>
            Paste the owner bearer once. It is exchanged for a session; the
            bridge holds the cookies, not the page.
          </DialogContentText>
          <TextField
            autoFocus
            fullWidth
            type="password"
            label="Kelp owner bearer"
            value={bearer}
            onChange={(event) => setBearer(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && bearer.trim()) {
                setAskSignIn(false);
                void chat.signIn(bearer.trim());
                setBearer("");
              }
            }}
          />
          {chat.failure && chat.phase === "signed-out" && (
            <Alert severity="error" sx={{ mt: 2 }}>
              {chat.failure.message}
            </Alert>
          )}
        </DialogContent>
        <DialogActions>
          <Button
            variant="contained"
            disabled={!bearer.trim()}
            onClick={() => {
              setAskSignIn(false);
              void chat.signIn(bearer.trim());
              setBearer("");
            }}
          >
            Sign in
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}

function MessageRow({
  message,
  onDecide,
  computer,
}: {
  message: Message;
  onDecide: (id: string, decision: "approve" | "deny") => void;
  computer?: ComputerCardHost;
}) {
  const mine = message.role === "user";
  // A message can carry only tool, approval, or artifact blocks — no text — so
  // "thinking…" is only right when there is genuinely nothing to render yet.
  const renderable =
    message.blocks.some((block) => (block.type === "text" ? Boolean(block.text) : true)) ||
    (message.sources?.length ?? 0) > 0;
  return (
    <Stack direction={mine ? "row-reverse" : "row"} spacing={1.5} alignItems="flex-start">
      <Avatar
        sx={{
          width: 30,
          height: 30,
          bgcolor: mine ? "rgba(25,24,23,0.08)" : "primary.main",
          color: mine ? "text.primary" : "primary.contrastText",
        }}
      >
        {mine ? "A" : <SmartToyRoundedIcon sx={{ fontSize: 18 }} />}
      </Avatar>
      <Box
        sx={{
          maxWidth: "76%",
          px: 2,
          py: 1.25,
          borderRadius: 3,
          background: mine ? "rgba(214,232,216,0.7)" : "transparent",
        }}
      >
        {renderable ? (
          <MessageBody message={message} onDecide={onDecide} computer={computer} />
        ) : (
          <MessagePlaceholder message={message} />
        )}
      </Box>
    </Stack>
  );
}
