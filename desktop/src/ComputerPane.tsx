import { Box, Button, Chip, CircularProgress, Stack, TextField, Typography } from "@mui/material";
import ReplayRoundedIcon from "@mui/icons-material/ReplayRounded";

import { isDefaultViewerUrl, isRfbTransport, statusLabel, unsupportedCredentialsMessage } from "./computerViewer";
import type { ViewerHandle } from "./useViewer";

/**
 * The Computer panel: a view-only noVNC viewer onto the VM's graphical
 * session.
 *
 * This component is presentational — the connection lives in `useViewer`, at
 * the app level, so the panel can be hidden and re-shown without dropping the
 * RFB session, the WebSocket, or the painted framebuffer. App hides this
 * whole surface with `display: none` (+ `inert`) when closed, which takes it
 * out of the tab order and stops any pointer or keyboard capture; noVNC is
 * view-only regardless, so it never forwards input in the first place.
 *
 * Passwords: when the server fires `credentialsrequired`, the panel asks for
 * the VNC password in memory only and hands it straight to noVNC via
 * `sendCredentials`. It is dropped the moment it is submitted or the session
 * ends.
 */
export default function ComputerPane({ viewer }: { viewer: ViewerHandle }) {
  const {
    status,
    detail,
    credentials,
    password,
    setPassword,
    urlInput,
    setUrlInput,
    setHostRef: hostRef,
    connect,
    resetToDefault,
    disconnect,
    submitPassword,
  } = viewer;

  const connected = status === "connected";
  const onDefault = isDefaultViewerUrl(urlInput);
  // The managed transport has no raw endpoint to type: sessions arrive from
  // the backend and dial only through the card's explicit action. The URL row
  // is a dev-transport tool for pointing at the stub.
  const showUrlRow = isRfbTransport();

  return (
    <Box
      sx={{ flex: 1, width: "100%", display: "flex", flexDirection: "column", minHeight: 0 }}
    >
      {showUrlRow ? (
      <Stack
        direction="row"
        spacing={1}
        alignItems="center"
        sx={{ px: 3, py: 1.5, borderBottom: "1px solid rgba(25,24,23,0.08)" }}
      >
        <TextField
          size="small"
          label="Viewer URL"
          value={urlInput}
          onChange={(event) => setUrlInput(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") connect();
          }}
          sx={{ flex: 1, maxWidth: 560 }}
        />
        <Button variant="contained" size="small" onClick={connect} disabled={status === "connecting"}>
          Connect
        </Button>
        {!onDefault && (
          <Button size="small" color="inherit" onClick={resetToDefault}>
            Use default
          </Button>
        )}
        <Button size="small" color="inherit" onClick={disconnect} disabled={status === "idle"}>
          Disconnect
        </Button>
        <Chip size="small" variant="outlined" label={connected ? "live · view-only" : "view-only"} />
      </Stack>
      ) : (
      <Stack
        direction="row"
        spacing={1}
        alignItems="center"
        sx={{ px: 3, py: 1.5, borderBottom: "1px solid rgba(25,24,23,0.08)" }}
      >
        <Chip size="small" variant="outlined" label={connected ? "live · view-only" : "view-only"} />
        <Box sx={{ flex: 1 }} />
        <Button size="small" color="inherit" onClick={disconnect} disabled={status === "idle"}>
          Disconnect
        </Button>
      </Stack>
      )}

      <Box sx={{ position: "relative", flex: 1, minHeight: 0, background: "#141615" }}>
        <Box
          ref={hostRef}
          sx={{
            position: "absolute",
            inset: 0,
            "& canvas": { width: "100% !important", height: "100% !important" },
          }}
        />

        {!connected && (
          <Stack
            sx={{
              position: "absolute",
              inset: 0,
              alignItems: "center",
              justifyContent: "center",
              gap: 1.5,
              px: 3,
              textAlign: "center",
              color: "rgba(255,255,255,0.86)",
            }}
          >
            {status === "connecting" ? (
              <>
                <CircularProgress size={22} sx={{ color: "rgba(255,255,255,0.7)" }} />
                <Typography variant="body2">{statusLabel(status)}</Typography>
              </>
            ) : status === "auth" && credentials?.kind === "password" ? (
              <Stack sx={{ gap: 1.5, alignItems: "center", width: "100%", maxWidth: 320 }}>
                <Typography variant="body2">This session needs its password.</Typography>
                <TextField
                  autoFocus
                  type="password"
                  label="VNC password"
                  size="small"
                  fullWidth
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") submitPassword();
                  }}
                  autoComplete="new-password"
                  sx={{ backgroundColor: "rgba(255,255,255,0.95)", borderRadius: 1 }}
                />
                <Stack direction="row" spacing={1}>
                  <Button
                    variant="contained"
                    size="small"
                    disabled={!password}
                    onClick={submitPassword}
                  >
                    Connect
                  </Button>
                  <Button size="small" color="inherit" onClick={disconnect}>
                    Cancel
                  </Button>
                </Stack>
                <Typography variant="caption" sx={{ color: "rgba(255,255,255,0.6)" }}>
                  Held in memory for this session only — never stored, logged, or sent anywhere
                  but the VNC server.
                </Typography>
              </Stack>
            ) : status === "auth" && credentials?.kind === "unsupported" ? (
              <>
                <Typography variant="body2">{statusLabel(status)}</Typography>
                <Typography variant="caption" sx={{ color: "rgba(255,255,255,0.6)" }}>
                  {unsupportedCredentialsMessage(credentials.requested)}
                </Typography>
                <Button size="small" color="inherit" onClick={disconnect} sx={{ color: "inherit" }}>
                  Close
                </Button>
              </>
            ) : (
              <>
                <Typography variant="body2">{statusLabel(status)}</Typography>
                {detail && (
                  <Typography variant="caption" sx={{ color: "rgba(255,255,255,0.6)" }}>
                    {detail}
                  </Typography>
                )}
                {status === "idle" && (
                  <Typography variant="caption" sx={{ color: "rgba(255,255,255,0.6)" }}>
                    The viewer connects on its own once the Kelp session is ready — or point it at
                    a websockify endpoint and press Connect.
                  </Typography>
                )}
                {status === "disconnected" && (
                  <Typography variant="caption" sx={{ color: "rgba(255,255,255,0.6)" }}>
                    Reconnecting automatically…
                  </Typography>
                )}
                <Button
                  size="small"
                  startIcon={<ReplayRoundedIcon />}
                  onClick={connect}
                  sx={{ color: "inherit" }}
                >
                  Try again
                </Button>
                {(status === "error" || status === "disconnected") && !onDefault && (
                  <Button size="small" onClick={resetToDefault} sx={{ color: "inherit" }}>
                    Use the default address
                  </Button>
                )}
              </>
            )}
          </Stack>
        )}
      </Box>
    </Box>
  );
}
