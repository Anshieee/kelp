import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ThemeProvider } from "@mui/material/styles";

import OverlayPanel from "./OverlayPanel";
import { theme } from "./theme";

const host = document.getElementById("root");
if (!host) throw new Error("missing #root");

// No CssBaseline here on purpose: it paints `body` with the theme background,
// which would fill the transparent window. The reset lives in overlay.html.
createRoot(host).render(
  <StrictMode>
    <ThemeProvider theme={theme}>
      <OverlayPanel />
    </ThemeProvider>
  </StrictMode>,
);
