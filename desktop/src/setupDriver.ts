// How the setup window talks to Rust. The screen depends only on this
// interface, so the dev fake (setupFake.ts) and the real shell are
// interchangeable and the screen has no Tauri imports of its own.
//
// Commands and event, as proposed in #83 (the Rust side lands separately):
//   setup_status            -> SetupStatus
//   setup_choose { mode }   -> ()   mode: "local" | "hosted"
//   setup_retry             -> ()
//   setup_quit              -> ()
//   event "setup-status"    -> SetupStatus
// Payloads are untrusted until parseSetupStatus has seen them.

import type { SetupMode } from "./setup";
import { isTauri } from "./tauri";

export interface SetupDriver {
  status(): Promise<unknown>;
  /** Returns an unsubscribe. Must be safe to call before the listener binds. */
  subscribe(handler: (status: unknown) => void): () => void;
  choose(mode: SetupMode): Promise<void>;
  retry(): Promise<void>;
  quit(): Promise<void>;
}

async function invoke<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  if (!isTauri()) throw new Error("setup is only available inside the Kelp app");
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<T>(command, args);
}

export const tauriSetupDriver: SetupDriver = {
  status: () => invoke<unknown>("setup_status"),

  subscribe(handler) {
    if (!isTauri()) return () => {};
    let disposed = false;
    let unlisten: (() => void) | undefined;
    void (async () => {
      const { listen } = await import("@tauri-apps/api/event");
      const stop = await listen<unknown>("setup-status", ({ payload }) => {
        if (!disposed) handler(payload);
      });
      if (disposed) stop();
      else unlisten = stop;
    })();
    return () => {
      disposed = true;
      unlisten?.();
    };
  },

  choose: (mode) => invoke<void>("setup_choose", { mode }),
  retry: () => invoke<void>("setup_retry"),
  quit: () => invoke<void>("setup_quit"),
};
