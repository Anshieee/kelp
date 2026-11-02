// Window helpers. Every call is a no-op in a plain browser, so `npm run dev`
// in a tab (without the Tauri shell) still renders both surfaces.

export const isTauri = (): boolean =>
  typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

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

export async function resizeOverlay(
  width: number,
  height: number,
): Promise<void> {
  if (!isTauri()) return;
  const { getCurrentWindow } = await import("@tauri-apps/api/window");
  const { LogicalSize } = await import("@tauri-apps/api/dpi");
  await getCurrentWindow().setSize(new LogicalSize(width, height));
}

/**
 * Run `onBlur` a beat after the window loses focus, unless it comes back
 * first. The grace period is what stops a click on the orb from hiding and
 * instantly re-showing the panel.
 */
export function onFocusLost(onBlur: () => void, graceMs = 160): () => void {
  if (!isTauri()) return () => {};
  let timer: ReturnType<typeof setTimeout> | undefined;
  let disposed = false;
  let unlisten: (() => void) | undefined;

  void (async () => {
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    const stop = await getCurrentWindow().onFocusChanged(({ payload }) => {
      if (disposed) return;
      if (payload) {
        if (timer) clearTimeout(timer);
        timer = undefined;
        return;
      }
      timer = setTimeout(() => {
        timer = undefined;
        onBlur();
      }, graceMs);
    });
    if (disposed) stop();
    else unlisten = stop;
  })();

  return () => {
    disposed = true;
    if (timer) clearTimeout(timer);
    unlisten?.();
  };
}
