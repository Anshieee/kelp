import { defineConfig } from "vite";

// The Kelp server mounts the built app at /phone/ (same origin as /api).
// base must match that mount so every asset URL resolves under it.
export default defineConfig({
  base: "/phone/",
  build: {
    outDir: "dist",
    sourcemap: false,
    target: "es2022",
  },
  server: {
    port: 5173,
    // Dev convenience only: proxy /api to a locally running Kelp server.
    // Note the server checks Origin === Host on mutations, so cookie-auth
    // mutations from the dev origin may be rejected; pairing still works.
    proxy: {
      "/api": { target: "http://localhost:8765", changeOrigin: true },
    },
  },
  preview: {
    port: 4173,
  },
});
