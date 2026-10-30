import { defineConfig } from "vite";

export default defineConfig({
  root: "src",
  server: { port: 1420, strictPort: true },
  build: {
    outDir: "../dist",
    emptyOutDir: true,
    rollupOptions: { input: { main: "src/index.html", pet: "src/pet.html" } },
  },
});
