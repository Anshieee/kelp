// Keychain abstraction: bearer token lives in the OS keychain only.
// Production path is the Tauri stronghold plugin; outside Tauri (dev
// browser, tests) it falls back to an in-memory store so the raw bearer
// is never written to localStorage / disk by accident.

const MEM = new Map();

function tauriInvoke(cmd, args) {
  const invoke = window.__TAURI__?.core?.invoke ?? window.__TAURI__?.invoke;
  return invoke ? invoke(cmd, args) : Promise.reject(new Error("no tauri runtime"));
}

export const keychain = {
  async setBearer(token) {
    try {
      await tauriInvoke("plugin:stronghold|insert_record", {
        location: { store: "kelp", path: null, salt: null },
        key: "bearer",
        value: Array.from(new TextEncoder().encode(token)),
      });
      return "stronghold";
    } catch {
      MEM.set("bearer", token); // dev/test fallback: memory only
      return "memory";
    }
  },
  async getBearer() {
    try {
      const bytes = await tauriInvoke("plugin:stronghold|get_record", {
        location: { store: "kelp", path: null, salt: null },
        key: "bearer",
      });
      return bytes ? new TextDecoder().decode(new Uint8Array(bytes)) : null;
    } catch {
      return MEM.get("bearer") ?? null;
    }
  },
  async clearBearer() {
    try {
      await tauriInvoke("plugin:stronghold|remove_record", {
        location: { store: "kelp", path: null, salt: null },
        key: "bearer",
      });
    } catch {
      MEM.delete("bearer");
    }
  },
};
