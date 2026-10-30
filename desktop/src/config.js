// The one configured server URL. v1 talks to exactly one Nebius-hosted
// Kelp server; there is no runtime URL switcher, no per-environment list.
// Release fills in the real host once here. Dev may point it at
// `http://127.0.0.1:8000` (`make serve`) for local verification.
export const SERVER_URL = "http://127.0.0.1:8000";
