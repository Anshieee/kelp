import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Three windows, three entry points: the full app, the overlay panel, and the
// untouched vanilla desk pet. The pet is deliberately not a React entry.
export default defineConfig({
  root: "src",
  plugins: [react()],
  server: { port: 1420, strictPort: true },
  build: {
    outDir: "../dist",
    emptyOutDir: true,
    rollupOptions: {
      input: {
        main: "src/index.html",
        overlay: "src/overlay.html",
        pet: "src/pet.html",
      },
    },
  },
});
