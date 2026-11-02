import { useMemo, useState } from "react";
import {
  Avatar,
  Box,
  Button,
  Chip,
  Divider,
  Drawer,
  IconButton,
  InputBase,
  List,
  ListItemButton,
  ListItemText,
  ListSubheader,
  Stack,
  Tooltip,
  Typography,
} from "@mui/material";
import AddRoundedIcon from "@mui/icons-material/AddRounded";
import ArrowUpwardRoundedIcon from "@mui/icons-material/ArrowUpwardRounded";
import AttachFileRoundedIcon from "@mui/icons-material/AttachFileRounded";
import MicRoundedIcon from "@mui/icons-material/MicRounded";
import MoreHorizRoundedIcon from "@mui/icons-material/MoreHorizRounded";
import ScreenshotMonitorRoundedIcon from "@mui/icons-material/ScreenshotMonitorRounded";
import SearchRoundedIcon from "@mui/icons-material/SearchRounded";
import SettingsRoundedIcon from "@mui/icons-material/SettingsRounded";
import SmartToyRoundedIcon from "@mui/icons-material/SmartToyRounded";

import { cannedReply, conversations as seed, groupConversations } from "./mock";
import type { ChatMessage, Conversation } from "./types";

const DRAWER_WIDTH = 280;

const SUGGESTIONS = [
  "Sort my inbox and show me what needs a reply",
  "Turn this report into a one-page brief",
  "Follow up on an unpaid invoice, politely",
  "Plan my week around three deadlines",
];

let nextId = 0;
const uid = (prefix: string) => `${prefix}-${++nextId}`;

export default function App() {
  const [items, setItems] = useState<Conversation[]>(seed);
  const [activeId, setActiveId] = useState<string>(seed[0]?.id ?? "");
  const [draft, setDraft] = useState("");

  const groups = useMemo(() => groupConversations(items), [items]);
  const active = items.find((item) => item.id === activeId) ?? items[0];

  function patchActive(messages: ChatMessage[], title?: string) {
    setItems((current) =>
      current.map((item) =>
        item.id === active?.id
          ? {
              ...item,
              title: title ?? item.title,
              updatedAt: new Date().toISOString(),
              messages,
            }
          : item,
      ),
    );
  }

  function send(text: string = draft) {
    const trimmed = text.trim();
    if (!trimmed || !active) return;
    const asked: ChatMessage = { id: uid("m"), role: "user", text: trimmed };
    const answered: ChatMessage = {
      id: uid("m"),
      role: "assistant",
      text: cannedReply(trimmed),
    };
    patchActive(
      [...active.messages, asked, answered],
      active.title === "New chat" ? trimmed.slice(0, 48) : undefined,
    );
    setDraft("");
  }

  function newChat() {
    const created: Conversation = {
      id: uid("c"),
      title: "New chat",
      updatedAt: new Date().toISOString(),
      messages: [],
    };
    setItems((current) => [created, ...current]);
    setActiveId(created.id);
    setDraft("");
  }

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
            onClick={newChat}
            sx={{ justifyContent: "flex-start", color: "text.primary" }}
          >
            New chat
          </Button>
        </Box>

        <Box sx={{ flex: 1, overflowY: "auto", px: 1 }}>
          {groups.map((group) => (
            <List
              key={group.label}
              dense
              subheader={
                <ListSubheader
                  disableSticky
                  sx={{ background: "transparent", fontSize: 11, fontWeight: 600, letterSpacing: "0.06em", textTransform: "uppercase", color: "text.secondary" }}
                >
                  {group.label}
                </ListSubheader>
              }
            >
              {group.conversations.map((conversation) => (
                <ListItemButton
                  key={conversation.id}
                  selected={conversation.id === active?.id}
                  onClick={() => setActiveId(conversation.id)}
                  sx={{ borderRadius: 2, mb: 0.25 }}
                >
                  <ListItemText
                    primary={conversation.title}
                    primaryTypographyProps={{ noWrap: true, fontSize: 14 }}
                  />
                </ListItemButton>
              ))}
            </List>
          ))}
        </Box>

        <Divider />
        <Box sx={{ p: 1.5 }}>
          <Button fullWidth startIcon={<SettingsRoundedIcon />} sx={{ justifyContent: "flex-start", color: "text.secondary" }}>
            Settings
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
          <Typography variant="subtitle1" noWrap sx={{ flex: 1, fontWeight: 600 }}>
            {active?.title ?? "New chat"}
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
        </Stack>

        <Box sx={{ flex: 1, overflowY: "auto", px: 3, py: 3 }}>
          {active && active.messages.length > 0 ? (
            <Stack spacing={2.5} sx={{ maxWidth: 760, mx: "auto" }}>
              {active.messages.map((message) => (
                <Message key={message.id} message={message} />
              ))}
            </Stack>
          ) : (
            <Stack sx={{ height: "100%", alignItems: "center", justifyContent: "center" }} spacing={3}>
              <Typography variant="h4" align="center">
                What can I help with?
              </Typography>
              <Stack direction="row" useFlexGap flexWrap="wrap" justifyContent="center" spacing={1} sx={{ maxWidth: 620 }}>
                {SUGGESTIONS.map((suggestion) => (
                  <Chip
                    key={suggestion}
                    label={suggestion}
                    onClick={() => send(suggestion)}
                    variant="outlined"
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
              <IconButton size="small">
                <AttachFileRoundedIcon fontSize="small" />
              </IconButton>
            </Tooltip>
            <InputBase
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  send();
                }
              }}
              placeholder="Ask Kelp…"
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
            <IconButton
              size="small"
              onClick={() => send()}
              disabled={!draft.trim()}
              aria-label="Send"
              sx={{ background: "primary.main", color: "primary.contrastText", "&:hover": { background: "primary.dark" }, "&.Mui-disabled": { background: "rgba(25,24,23,0.08)" } }}
            >
              <ArrowUpwardRoundedIcon fontSize="small" />
            </IconButton>
          </Stack>
          <Typography variant="caption" align="center" display="block" sx={{ mt: 1, color: "text.secondary" }}>
            Preview build — replies are canned and nothing is stored.
          </Typography>
        </Box>
      </Box>
    </Box>
  );
}

function Message({ message }: { message: ChatMessage }) {
  const mine = message.role === "user";
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
        <Typography variant="body1" sx={{ whiteSpace: "pre-wrap" }}>
          {message.text}
        </Typography>
      </Box>
    </Stack>
  );
}
