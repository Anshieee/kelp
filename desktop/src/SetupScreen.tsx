// The first-run setup and recovery window (#83). It renders whatever Rust
// reports through a SetupDriver and sends back exactly one of four actions.
// Every rule about what may be offered lives in setup.ts.

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import CircularProgress from "@mui/material/CircularProgress";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import CheckCircleRoundedIcon from "@mui/icons-material/CheckCircleRounded";
import RadioButtonUncheckedRoundedIcon from "@mui/icons-material/RadioButtonUncheckedRounded";

import { setupView, type SetupAction, type SetupStatus } from "./setup";
import type { SetupDriver } from "./setupDriver";
import { useSetup } from "./useSetup";

export default function SetupScreen({ driver }: { driver: SetupDriver }) {
  const { status, busy, act } = useSetup(driver);

  return (
    <Box
      sx={{
        minHeight: "100%",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        bgcolor: "background.default",
        p: 3,
      }}
    >
      <Paper variant="outlined" sx={{ width: "100%", maxWidth: 420, p: 3.5 }}>
        {status === null ? (
          <Stack direction="row" spacing={1.5} alignItems="center" aria-live="polite">
            <CircularProgress size={18} />
            <Typography color="text.secondary">Starting Kelp…</Typography>
          </Stack>
        ) : (
          <SetupBody status={status} busy={busy} onAction={act} />
        )}
      </Paper>
    </Box>
  );
}

function SetupBody({
  status,
  busy,
  onAction,
}: {
  status: SetupStatus;
  busy: boolean;
  onAction: (action: SetupAction) => void;
}) {
  const view = setupView(status);
  const failed = status.phase === "failed";
  const choosing = status.phase === "choose";

  return (
    <Stack spacing={2.5} aria-live="polite">
      <Typography variant="h5" component="h1">
        {view.title}
      </Typography>

      {failed ? (
        <Alert severity="error">{view.body}</Alert>
      ) : (
        <Typography color="text.secondary">{view.body}</Typography>
      )}

      {view.steps && (
        <Stack component="ol" spacing={1.25} sx={{ listStyle: "none", m: 0, p: 0 }}>
          {view.steps.map(({ step, label, state }) => (
            <Stack
              key={step}
              component="li"
              direction="row"
              spacing={1.25}
              alignItems="center"
              aria-current={state === "active" ? "step" : undefined}
            >
              {/* One fixed slot so the labels line up whichever icon shows. */}
              <Box sx={{ width: 20, height: 20, display: "grid", placeItems: "center", flexShrink: 0 }}>
                {state === "done" ? (
                  <CheckCircleRoundedIcon fontSize="small" color="primary" />
                ) : state === "active" ? (
                  <CircularProgress size={16} thickness={5} />
                ) : (
                  <RadioButtonUncheckedRoundedIcon fontSize="small" sx={{ color: "text.disabled" }} />
                )}
              </Box>
              <Typography color={state === "pending" ? "text.secondary" : "text.primary"}>
                {label}
              </Typography>
            </Stack>
          ))}
        </Stack>
      )}

      {status.phase === "ready" && (
        <Stack direction="row" spacing={1.5} alignItems="center">
          <CircularProgress size={18} />
          <Typography color="text.secondary">Opening Kelp…</Typography>
        </Stack>
      )}

      {view.actions.length > 0 && (
        // The first-run choice stacks full-width so neither option reads as
        // fine print; recovery keeps the usual right-aligned dialog row.
        <Stack
          direction={choosing ? "column" : "row"}
          spacing={1}
          justifyContent="flex-end"
          useFlexGap
        >
          {view.actions.map(({ action, label }, index) => (
            <Button
              key={action}
              fullWidth={choosing}
              variant={
                index === 0 && action !== "quit"
                  ? "contained"
                  : choosing && action === "hosted"
                    ? "outlined"
                    : "text"
              }
              color={action === "quit" ? "inherit" : "primary"}
              disabled={busy}
              onClick={() => onAction(action)}
            >
              {label}
            </Button>
          ))}
        </Stack>
      )}
    </Stack>
  );
}
