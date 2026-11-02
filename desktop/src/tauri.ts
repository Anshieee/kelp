// Window helpers. Every call is a no-op in a plain browser, so `npm run dev`
// in a tab (without the Tauri shell) still renders both surfaces.

export const isTauri = (): boolean =>
  typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

async function call<T>(command: string, args?: Record<string, unknown>) {
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<T>(command, args);
}

export async function hideOverlay(): Promise<void> {
  if (!isTauri()) return;
  const { getCurrentWindow } = await import("@tauri-apps/api/window");
  await getCurrentWindow().hide();
}

export async function openFullApp(): Promise<void> {
  if (!isTauri()) return;
  const { WebviewWindow } = await import("@tauri-apps/api/webviewWindow");
  const main = await WebviewWindow.getByLabel("main");
  if (main) {
    await main.show();
    await main.setFocus();
  }
  await hideOverlay();
}

/**
 * Resize and re-park in one step. Rust owns the placement so the panel keeps
 * its bottom edge on the orb while it grows upward.
 */
export async function resizeOverlay(
  width: number,
  height: number,
): Promise<void> {
  if (!isTauri()) return;
  await call("resize_overlay", { width, height });
}

/**
 * Subscribe to focus changes. Hiding on focus loss is Rust's job — it can ask
 * whether the orb took focus, which removes the race between "blur hides the
 * panel" and "the orb's click re-shows it".
 */
export function onOverlayFocus(
  handler: (focused: boolean) => void,
): () => void {
  if (!isTauri()) return () => {};
  let disposed = false;
  let unlisten: (() => void) | undefined;

  void (async () => {
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    const stop = await getCurrentWindow().onFocusChanged(({ payload }) => {
      if (!disposed) handler(payload);
    });
    if (disposed) stop();
    else unlisten = stop;
  })();

  return () => {
    disposed = true;
    unlisten?.();
  };
}
