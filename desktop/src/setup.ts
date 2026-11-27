// First-run setup and recovery: the contract between Rust and the setup
// window (issue #83).
//
// Rust owns the state machine: runtime verification, the handshake, the
// loopback guard, and remembering the Local choice. This module only parses
// what Rust reports and decides what the screen may offer. It never sees an
// interpreter path, a port, a bearer, or a provider credential, and it never
// renders raw error text: every failure maps to fixed copy here.
//
// Pure on purpose (no React, no Tauri, no `import.meta`), so `node --test`
// can pin every rule directly.

export type SetupMode = "hosted" | "local";

export type SetupStep = "runtime" | "handshake" | "loopback-guard";

export type SetupFailure =
  | "bundle-missing"
  | "bundle-broken"
  | "handshake-failed"
  | "guard-failed"
  | "unknown";

export type SetupStatus =
  /** First run only. `hostedAvailable` is Rust's call, not the page's. */
  | { phase: "choose"; hostedAvailable: boolean }
  | { phase: "verifying"; step: SetupStep }
  | { phase: "failed"; reason: SetupFailure; retryable: boolean }
  /** Rust builds the app windows and closes setup after this. */
  | { phase: "ready"; mode: SetupMode };

export type SetupAction = "local" | "hosted" | "retry" | "quit";

export const SETUP_STEPS: readonly SetupStep[] = ["runtime", "handshake", "loopback-guard"];

const FAILURES: readonly SetupFailure[] = [
  "bundle-missing",
  "bundle-broken",
  "handshake-failed",
  "guard-failed",
  "unknown",
];

/** What the page shows when it cannot trust what it was told. */
export const UNKNOWN_FAILURE: SetupStatus = { phase: "failed", reason: "unknown", retryable: true };

/**
 * Accept only the shapes the contract defines, and rebuild them from known
 * fields so nothing extra (a path, a message, a token) survives the parse.
 * Anything else is a failure, never a choice: a malformed or missing status
 * must not be able to put a Hosted button on screen.
 */
export function parseSetupStatus(value: unknown): SetupStatus {
  if (typeof value !== "object" || value === null) return UNKNOWN_FAILURE;
  const raw = value as Record<string, unknown>;
  switch (raw.phase) {
    case "choose":
      return { phase: "choose", hostedAvailable: raw.hostedAvailable === true };
    case "verifying":
      return SETUP_STEPS.includes(raw.step as SetupStep)
        ? { phase: "verifying", step: raw.step as SetupStep }
        : UNKNOWN_FAILURE;
    case "failed":
      return {
        phase: "failed",
        reason: FAILURES.includes(raw.reason as SetupFailure) ? (raw.reason as SetupFailure) : "unknown",
        // Retry only asks Rust to try again; Rust decides whether it can.
        retryable: raw.retryable !== false,
      };
    case "ready":
      return raw.mode === "hosted" || raw.mode === "local"
        ? { phase: "ready", mode: raw.mode }
        : UNKNOWN_FAILURE;
    default:
      return UNKNOWN_FAILURE;
  }
}

export const STEP_LABELS: Record<SetupStep, string> = {
  runtime: "Starting Kelp on this computer",
  handshake: "Checking that it responds",
  "loopback-guard": "Confirming it is private to this computer",
};

const FAILURE_COPY: Record<SetupFailure, string> = {
  "bundle-missing": "This copy of Kelp is missing the part that runs on your computer.",
  "bundle-broken": "Kelp could not start on this computer.",
  "handshake-failed": "Kelp started but did not respond as expected, so it was stopped.",
  "guard-failed": "Kelp could not confirm it was private to this computer, so it was stopped.",
  unknown: "Kelp could not finish setting up.",
};

export interface SetupView {
  title: string;
  body: string;
  /** Present only while verifying: every step with its progress. */
  steps?: { step: SetupStep; label: string; state: "done" | "active" | "pending" }[];
  /** Ordered; the first one is the primary button. */
  actions: { action: SetupAction; label: string }[];
}

const QUIT = { action: "quit", label: "Quit" } as const;

/**
 * The screen for a status. The recovery rules live here, not in JSX:
 * - `failed` offers only Retry and Quit, so a broken bundle can never drift
 *   into Hosted;
 * - Hosted appears only in `choose`, only when Rust says it may, and is
 *   worded as this launch only (Rust does not remember it);
 * - there are no inputs: no paths, commands, or keys.
 */
export function setupView(status: SetupStatus): SetupView {
  switch (status.phase) {
    case "choose":
      return {
        title: "Set up Kelp",
        body: "Choose where Kelp runs. Running on this computer is remembered once Kelp has checked that it works.",
        actions: [
          { action: "local", label: "Run on this computer" },
          ...(status.hostedAvailable
            ? [{ action: "hosted" as const, label: "Use Kelp's servers this time" }]
            : []),
          QUIT,
        ],
      };
    case "verifying": {
      const current = SETUP_STEPS.indexOf(status.step);
      return {
        title: "Setting up Kelp",
        body: "This takes a few seconds.",
        steps: SETUP_STEPS.map((step, index) => ({
          step,
          label: STEP_LABELS[step],
          state: index < current ? "done" : index === current ? "active" : "pending",
        })),
        actions: [QUIT],
      };
    }
    case "failed":
      return {
        title: "Kelp could not start",
        body: `${FAILURE_COPY[status.reason]} ${
          status.retryable
            ? "Try again, or quit and open Kelp later."
            : "Quit, reinstall Kelp, and open it again."
        }`,
        actions: status.retryable ? [{ action: "retry", label: "Try again" }, QUIT] : [QUIT],
      };
    case "ready":
      return {
        title: "Kelp is ready",
        body:
          status.mode === "local"
            ? "Kelp will run on this computer from now on."
            : "Kelp will use its servers for this launch only.",
        actions: [],
      };
  }
}
