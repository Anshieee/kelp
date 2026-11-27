// Dev-only stand-in for the Rust setup state machine, so the screen can be
// reviewed in a browser tab before the shell implements the commands.
// setup.tsx loads it only when `import.meta.env.DEV` is set and there is no
// Tauri shell, so it is never on a release path.
//
// Pick a scenario with `?setup=`:
//   first-run (default)  choose with Hosted offered -> verify -> ready
//   local-only           choose without Hosted -> verify -> ready
//   broken               verify fails at the handshake; Retry then succeeds
//   guard                verify fails at the loopback guard; Retry then succeeds
//   missing              bundle missing, not retryable
//   garbage              Rust sends something malformed

import type { SetupMode, SetupStatus, SetupStep } from "./setup";
import type { SetupDriver } from "./setupDriver";

type Scenario = "first-run" | "local-only" | "broken" | "guard" | "missing" | "garbage";

const STEP_MS = 900;

export function fakeSetupDriver(scenario: string | null): SetupDriver {
  const name: Scenario =
    scenario === "local-only" ||
    scenario === "broken" ||
    scenario === "guard" ||
    scenario === "missing" ||
    scenario === "garbage"
      ? scenario
      : "first-run";

  const listeners = new Set<(status: unknown) => void>();
  let timers: number[] = [];
  let attempts = 0;

  let current: unknown =
    name === "missing"
      ? ({ phase: "failed", reason: "bundle-missing", retryable: false } satisfies SetupStatus)
      : name === "garbage"
        ? { phase: "choose!", path: "/opt/kelp/python3", token: "do-not-render" }
        : name === "broken" || name === "guard"
          ? ({ phase: "verifying", step: "runtime" } satisfies SetupStatus)
          : ({ phase: "choose", hostedAvailable: name === "first-run" } satisfies SetupStatus);

  const emit = (status: unknown) => {
    current = status;
    for (const listener of listeners) listener(status);
  };

  const clear = () => {
    for (const timer of timers) window.clearTimeout(timer);
    timers = [];
  };

  const run = (mode: SetupMode) => {
    clear();
    attempts += 1;
    const failAt: SetupStep | null =
      attempts === 1 && name === "broken" ? "handshake" : attempts === 1 && name === "guard" ? "loopback-guard" : null;
    const steps: SetupStep[] = mode === "local" ? ["runtime", "handshake", "loopback-guard"] : ["runtime"];
    steps.forEach((step, index) => {
      timers.push(
        window.setTimeout(() => {
          if (failAt && index === steps.indexOf(failAt)) {
            emit({
              phase: "failed",
              reason: failAt === "handshake" ? "handshake-failed" : "guard-failed",
              retryable: true,
            } satisfies SetupStatus);
            clear();
            return;
          }
          emit({ phase: "verifying", step } satisfies SetupStatus);
        }, index * STEP_MS),
      );
    });
    if (!failAt) {
      timers.push(
        window.setTimeout(
          () => emit({ phase: "ready", mode } satisfies SetupStatus),
          steps.length * STEP_MS,
        ),
      );
    }
  };

  // The failing scenarios start mid-verification, as a remembered Local
  // launch would: no choice screen, straight into the checks.
  if (name === "broken" || name === "guard") run("local");

  return {
    status: async () => current,
    subscribe(handler) {
      listeners.add(handler);
      return () => listeners.delete(handler);
    },
    choose: async (mode) => run(mode),
    retry: async () => run("local"),
    quit: async () => {
      // A browser tab cannot close itself; the shell would exit here.
      clear();
      console.info("[setup fake] quit requested");
    },
  };
}
