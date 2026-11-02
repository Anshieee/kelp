import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import CssBaseline from "@mui/material/CssBaseline";
import { ThemeProvider } from "@mui/material/styles";

import OverlayPanel from "./OverlayPanel";
import { theme } from "./theme";

const host = document.getElementById("root");
if (!host) throw new Error("missing #root");

createRoot(host).render(
  <StrictMode>
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <OverlayPanel />
    </ThemeProvider>
  </StrictMode>,
);
