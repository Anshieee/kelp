// The one configured server URL. v1 talks to exactly one Nebius-hosted
// Kelp server; there is no runtime URL switcher, no per-environment list.
// The committed default is always the https placeholder. Local development
// overrides it per-build with VITE_KELP_SERVER_URL, never with a committed
// edit (production_guards.test.mjs pins the scheme).
const DEFAULT_SERVER_URL = "https://kelp.example.com";
export const SERVER_URL =
  import.meta.env?.VITE_KELP_SERVER_URL?.trim() || DEFAULT_SERVER_URL;
