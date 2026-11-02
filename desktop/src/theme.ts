import { createTheme } from "@mui/material/styles";

// Interim design language: MUI defaults with the site's sans. The palette is
// re-skinned to match the website in a later pass, so nothing here is precious.
export const theme = createTheme({
  palette: {
    mode: "light",
    background: { default: "#FBF9F5", paper: "#FFFFFF" },
    primary: { main: "#2E5B3F" },
    text: { primary: "#191817", secondary: "#5B5852" },
  },
  shape: { borderRadius: 14 },
  typography: {
    fontFamily:
      '"Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
    h4: { fontWeight: 600, letterSpacing: "-0.02em" },
    h5: { fontWeight: 600, letterSpacing: "-0.02em" },
    h6: { fontWeight: 600 },
  },
  components: {
    MuiButton: {
      defaultProps: { disableElevation: true },
      styleOverrides: { root: { textTransform: "none", fontWeight: 600 } },
    },
    MuiCssBaseline: {
      styleOverrides: {
        "html, body, #root": { height: "100%" },
        body: { margin: 0 },
      },
    },
  },
});
