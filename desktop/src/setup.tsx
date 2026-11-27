import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import CssBaseline from "@mui/material/CssBaseline";
import { ThemeProvider } from "@mui/material/styles";

import SetupScreen from "./SetupScreen";
import { tauriSetupDriver, type SetupDriver } from "./setupDriver";
import { isTauri } from "./tauri";
import { theme } from "./theme";

const host = document.getElementById("root");
if (!host) throw new Error("missing #root");

// The fake exists only for reviewing the screen in a browser tab during
// development. Release builds, and any run inside the shell, talk to Rust.
async function pickDriver(): Promise<SetupDriver> {
  if (import.meta.env.DEV && !isTauri()) {
    const { fakeSetupDriver } = await import("./setupFake");
    return fakeSetupDriver(new URLSearchParams(window.location.search).get("setup"));
  }
  return tauriSetupDriver;
}

void pickDriver().then((driver) => {
  createRoot(host).render(
    <StrictMode>
      <ThemeProvider theme={theme}>
        <CssBaseline />
        <SetupScreen driver={driver} />
      </ThemeProvider>
    </StrictMode>,
  );
});
