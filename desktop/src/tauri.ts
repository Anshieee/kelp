// Window helpers. Every call is a no-op in a plain browser, so `npm run dev`
// in a tab (without the Tauri shell) still renders both surfaces.

const inTauri = (): boolean =>
  typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

export async function hideOverlay(): Promise<void> {
  if (!inTauri()) return;
  const { getCurrentWindow } = await import("@tauri-apps/api/window");
  await getCurrentWindow().hide();
}

export async function openFullApp(): Promise<void> {
  if (!inTauri()) return;
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
  if (!inTauri()) return;
  const { getCurrentWindow } = await import("@tauri-apps/api/window");
  const { LogicalSize } = await import("@tauri-apps/api/dpi");
  await getCurrentWindow().setSize(new LogicalSize(width, height));
}
