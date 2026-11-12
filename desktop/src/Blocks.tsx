import { Box, Button, Chip, CircularProgress, Stack, Typography } from "@mui/material";

import ComputerCard from "./ComputerCard";
import type { ViewerStatus } from "./computerViewer";
import type { Approval, Message, Source } from "./bridge";
import { isApprovalBlock, isArtifactBlock, isToolBlock, type ToolBlock } from "./chatEvents";

const TOOL_STATUS: Record<string, string> = {
  running: "running…",
  waiting_approval: "needs approval",
  done: "done",
  failed: "failed",
  blocked: "denied",
  expired: "expired",
};

const APPROVAL_STATUS: Record<string, string> = {
  pending: "Waiting for you",
  approved: "Approved",
  denied: "Denied",
  expired: "Expired",
  consumed: "Approved",
};

/** What a computer card needs from the app: the one viewer, plus the anchors
 *  and the two callbacks. Passed down rather than read from context so the
 *  transcript stays a pure function of its props. */
export interface ComputerCardHost {
  viewer: { status: ViewerStatus; hostRef: { current: HTMLDivElement | null } };
  anchors: Set<string>;
  onOpen: () => void;
  onDismiss: (callId: string) => void;
}

function ToolRow({ block, computer }: { block: ToolBlock; computer?: ComputerCardHost }) {
  const status = TOOL_STATUS[block.status] ?? block.status;
  const failed = block.status === "failed" || block.status === "blocked";
  // One card per session: `anchors` holds the first call that mentioned each
  // session, so a turn that drives the same screen three times grows one card.
  const showCard =
    computer !== undefined &&
    block.surface === "computer" &&
    computer.anchors.has(block.call_id);
  return (
    <Box
      sx={{
        border: "1px solid rgba(25,24,23,0.10)",
        borderRadius: 2,
        px: 1.25,
        py: 0.75,
        background: "rgba(25,24,23,0.02)",
      }}
    >
      <Stack direction="row" spacing={1} alignItems="center">
        <Typography variant="caption" sx={{ fontWeight: 600 }}>
          {block.label || block.tool}
        </Typography>
        <Box sx={{ flex: 1 }} />
        <Typography variant="caption" color={failed ? "error" : "text.secondary"}>
          {status}
        </Typography>
      </Stack>
      {block.summary && (
        <Typography
          variant="caption"
          color="text.secondary"
          sx={{ display: "block", mt: 0.25, wordBreak: "break-word" }}
        >
          {block.summary}
        </Typography>
      )}
      {showCard && computer && (
        <ComputerCard
          block={block}
          viewer={computer.viewer}
          onOpen={computer.onOpen}
          onDismiss={() => computer.onDismiss(block.call_id)}
        />
      )}
    </Box>
  );
}

function ApprovalCard({
  approval,
  onDecide,
}: {
  approval: Approval;
  onDecide?: (id: string, decision: "approve" | "deny") => void;
}) {
  const pending = approval.status === "pending";
  return (
    <Box
      sx={{
        border: "1px solid rgba(25,24,23,0.16)",
        borderLeft: "3px solid",
        borderLeftColor: pending ? "warning.main" : "rgba(25,24,23,0.16)",
        borderRadius: 2,
        p: 1.5,
        background: "#FFFFFF",
      }}
    >
      <Typography variant="subtitle2" sx={{ fontWeight: 600 }}>
        {approval.title}
      </Typography>
      {approval.rationale && (
        <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
          {approval.rationale}
        </Typography>
      )}
      {approval.summary && (
        <Typography
          variant="caption"
          sx={{ display: "block", mt: 0.75, fontFamily: "monospace", wordBreak: "break-word" }}
        >
          {approval.summary}
        </Typography>
      )}
      {approval.effects && approval.effects.length > 0 && (
        <Stack direction="row" useFlexGap flexWrap="wrap" spacing={0.5} sx={{ mt: 1 }}>
          {approval.effects.map((effect) => (
            <Chip key={effect} size="small" label={effect} variant="outlined" />
          ))}
        </Stack>
      )}
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 1.5 }}>
        {pending ? (
          <>
            <Button
              size="small"
              variant="contained"
              onClick={() => onDecide?.(approval.id, "approve")}
            >
              Approve
            </Button>
            <Button size="small" color="inherit" onClick={() => onDecide?.(approval.id, "deny")}>
              Deny
            </Button>
            {typeof approval.ttl_seconds === "number" && approval.ttl_seconds > 0 && (
              <Typography variant="caption" color="text.secondary">
                expires in {Math.round(approval.ttl_seconds / 60)} min
              </Typography>
            )}
          </>
        ) : (
          <Typography variant="caption" color="text.secondary">
            {APPROVAL_STATUS[approval.status] ?? approval.status}
          </Typography>
        )}
      </Stack>
    </Box>
  );
}

function ArtifactRow({ artifact }: { artifact: Record<string, unknown> }) {
  const name = String(artifact.filename ?? artifact.handle ?? "artifact");
  return (
    <Box
      sx={{
        border: "1px solid rgba(25,24,23,0.10)",
        borderRadius: 2,
        px: 1.25,
        py: 0.75,
        background: "rgba(25,24,23,0.02)",
      }}
    >
      <Typography variant="caption" sx={{ fontWeight: 600 }}>
        {name}
      </Typography>
    </Box>
  );
}

function SourceChips({ sources }: { sources: Source[] }) {
  return (
    <Stack direction="row" useFlexGap flexWrap="wrap" spacing={0.5} sx={{ mt: 0.5 }}>
      {sources.map((source, index) => (
        <Chip
          key={`${source.title ?? source.url ?? "source"}-${index}`}
          size="small"
          label={source.title ?? source.url ?? source.kind ?? "source"}
          variant="outlined"
        />
      ))}
    </Stack>
  );
}

/** Render one message's blocks: text, tool calls, approval cards, artifacts. */
export function MessageBody({
  message,
  onDecide,
  computer,
}: {
  message: Message;
  onDecide?: (id: string, decision: "approve" | "deny") => void;
  computer?: ComputerCardHost;
}) {
  return (
    <Stack spacing={1}>
      {message.blocks.map((block, index) => {
        if (block.type === "text") {
          const text = block.text ?? "";
          return text ? (
            <Typography key={index} variant="body1" sx={{ whiteSpace: "pre-wrap" }}>
              {text}
            </Typography>
          ) : null;
        }
        if (isToolBlock(block)) return <ToolRow key={index} block={block} computer={computer} />;
        if (isApprovalBlock(block)) {
          return <ApprovalCard key={index} approval={block.approval} onDecide={onDecide} />;
        }
        if (isArtifactBlock(block)) {
          return <ArtifactRow key={index} artifact={block.artifact} />;
        }
        return null;
      })}
      {message.sources && message.sources.length > 0 && <SourceChips sources={message.sources} />}
    </Stack>
  );
}

/**
 * What an assistant bubble shows while it has nothing to render.
 *
 * No blocks only means "still thinking" while the message is pending or
 * streaming. A turn that failed stays blockless forever, so a spinner there
 * looks exactly like a hang — which is how a spent key or a provider 403 gets
 * mistaken for "the chat is stuck in thinking".
 */
export function MessagePlaceholder({ message }: { message: Message }) {
  if (message.status === "error") {
    return (
      <Typography variant="caption" color="error">
        The reply failed — check the provider, then retry the turn.
      </Typography>
    );
  }
  if (message.status === "complete") {
    return (
      <Typography variant="caption" color="text.secondary">
        No reply.
      </Typography>
    );
  }
  return (
    <Stack direction="row" spacing={0.5} alignItems="center">
      <CircularProgress size={12} />
      <Typography variant="caption" color="text.secondary">
        thinking…
      </Typography>
    </Stack>
  );
}
