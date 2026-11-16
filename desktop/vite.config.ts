import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Three windows, three entry points: the full app, the overlay panel, and the
// untouched vanilla desk pet. The pet is deliberately not a React entry.
export default defineConfig({
  root: "src",
  plugins: [react()],
  server: { port: 1420, strictPort: true },
  // noVNC's browser probe uses top-level await, which the default `es2020`
  // target rejects: dependency pre-bundling fails, the optimized module never
  // resolves, and the Computer tab can never load its client. WebView2 is
  // current Chromium, and Tauri only ships to modern engines, so `es2022` holds.
  optimizeDeps: { esbuildOptions: { target: "es2022" } },
  build: {
    target: "es2022",
    outDir: "../dist",
    emptyOutDir: true,
    rollupOptions: {
      // These are resolved against the working directory, not `root`, so they
      // keep the `src/` prefix even though `root` is already `src`.
      input: {
        main: "src/index.html",
        overlay: "src/overlay.html",
        pet: "src/pet.html",
      },
    },
  },
});
