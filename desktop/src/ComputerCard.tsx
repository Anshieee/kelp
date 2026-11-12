import { useEffect, useRef, useState } from "react";
import { Box, Button, IconButton, Stack, Typography } from "@mui/material";
import CloseRoundedIcon from "@mui/icons-material/CloseRounded";
import OpenInNewRoundedIcon from "@mui/icons-material/OpenInNewRounded";

import type { ToolBlock } from "./chatEvents";
import { CARD_MIRROR_MS, formatElapsed, type ViewerStatus } from "./computerViewer";

/** Just the parts of the viewer handle the card needs. */
interface ViewerSource {
  status: ViewerStatus;
  hostRef: { current: HTMLDivElement | null };
}

/**
 * The inline computer card: a live mirror of the app's one viewer, hung under
 * the tool call that drives a screen.
 *
 * It owns no connection. The RFB session lives at app level and survives the
 * Computer tab being hidden — including the `display:none` case, where the
 * canvas keeps its bitmap and only its layout collapses — so the card reads
 * the same framebuffer the tab does by copying the canvas four times a second.
 * That keeps one socket per viewer, which is the whole reason the card can be
 * cheap enough to sit inside a transcript.
 *
 * Clicking it opens the Computer tab (the session is already there); it never
 * dials on its own.
 */
export default function ComputerCard({
  block,
  viewer,
  onOpen,
  onDismiss,
}: {
  block: ToolBlock;
  viewer: ViewerSource;
  onOpen: () => void;
  onDismiss: () => void;
}) {
  const cardRef = useRef<HTMLDivElement | null>(null);
  const frameRef = useRef<HTMLCanvasElement | null>(null);
  // When this card first saw a running call. Kept here rather than in the block
  // so the reducer stays a pure function of the stream (and its tests stay
  // deterministic); a finished call reports the server's own duration anyway.
  const startedAt = useRef<number | null>(block.status === "running" ? Date.now() : null);
  const [mirrored, setMirrored] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  const connected = viewer.status === "connected";
  const running = block.status === "running";

  // Elapsed ticks only while the call is running; a finished call shows the
  // duration the server measured, not a counter that keeps going.
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(timer);
  }, [running]);

  useEffect(() => {
    const card = cardRef.current;
    const canvas = frameRef.current;
    if (!connected || !card || !canvas) return;

    // Pause when nobody is looking: a card scrolled out of the transcript, or
    // a window in the background, must not keep copying pixels.
    let visible = true;
    const observer = new IntersectionObserver(
      (entries) => {
        visible = entries.some((entry) => entry.isIntersecting);
      },
      { threshold: 0.05 },
    );
    observer.observe(card);

    const copy = () => {
      if (!visible || document.hidden) return;
      const source = viewer.hostRef.current?.querySelector("canvas");
      // Width 0 means noVNC has not painted yet: leave the placeholder up
      // rather than showing an empty black rectangle.
      if (!source || !source.width || !source.height) return;
      if (canvas.width !== source.width || canvas.height !== source.height) {
        canvas.width = source.width;
        canvas.height = source.height;
      }
      const context = canvas.getContext("2d");
      if (!context) return;
      context.drawImage(source, 0, 0);
      setMirrored(true);
    };

    copy();
    const timer = window.setInterval(copy, CARD_MIRROR_MS);
    return () => {
      window.clearInterval(timer);
      observer.disconnect();
    };
  }, [connected, viewer.hostRef]);

  const elapsed = formatElapsed(
    running ? (startedAt.current === null ? null : now - startedAt.current) : block.duration_ms,
  );
  const state = connected ? "live" : viewer.status === "connecting" ? "connecting" : "not connected";
  const dot = connected ? "#3f7d5c" : viewer.status === "connecting" ? "#b08034" : "#8a8a86";

  return (
    <Box
      ref={cardRef}
      onClick={onOpen}
      sx={{
        mt: 1,
        border: "1px solid rgba(25,24,23,0.14)",
        borderRadius: 2,
        overflow: "hidden",
        background: "#FFFFFF",
        cursor: "pointer",
        "&:hover": { borderColor: "rgba(25,24,23,0.28)" },
      }}
    >
      <Box sx={{ position: "relative", aspectRatio: "16 / 10", background: "#141615" }}>
        <canvas
          ref={frameRef}
          style={{
            display: mirrored ? "block" : "none",
            width: "100%",
            height: "100%",
            objectFit: "contain",
          }}
        />
        {!mirrored && (
          <Stack
            sx={{
              position: "absolute",
              inset: 0,
              alignItems: "center",
              justifyContent: "center",
              px: 2,
              textAlign: "center",
            }}
          >
            <Typography variant="caption" sx={{ color: "rgba(255,255,255,0.7)" }}>
              {connected
                ? "Waiting for the first frame…"
                : "No live screen — open the Computer tab and connect."}
            </Typography>
          </Stack>
        )}
      </Box>

      <Stack
        direction="row"
        spacing={1}
        alignItems="center"
        sx={{ px: 1.25, py: 0.75 }}
        onClick={(event) => event.stopPropagation()}
      >
        <Box
          component="span"
          sx={{ width: 7, height: 7, borderRadius: "50%", background: dot, flex: "none" }}
        />
        <Typography variant="caption" sx={{ fontWeight: 600 }} noWrap>
          {block.label || block.tool}
        </Typography>
        <Typography variant="caption" color="text.secondary" noWrap>
          {state}
          {elapsed ? ` · ${elapsed}` : ""}
        </Typography>
        <Box sx={{ flex: 1 }} />
        <Button size="small" endIcon={<OpenInNewRoundedIcon />} onClick={onOpen}>
          Open Computer
        </Button>
        <IconButton size="small" aria-label="Dismiss computer card" onClick={onDismiss}>
          <CloseRoundedIcon fontSize="small" />
        </IconButton>
      </Stack>
    </Box>
  );
}
