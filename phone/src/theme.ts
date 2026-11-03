/*
 * Midnight/Meadow skin for the phone app. Midnight is the default; the choice
 * is persisted to localStorage and reflected on <html data-skin> plus the
 * theme-color meta so the OS chrome matches. No API or routing impact.
 */

export type Skin = "midnight" | "meadow";

const STORAGE_KEY = "kelp.phone.skin";

/** theme-color values mirror netlify/mobile (Midnight #141414). */
const THEME_COLORS: Record<Skin, string> = {
  midnight: "#141414",
  meadow: "#f6f8f6",
};

function hasDom(): boolean {
  return typeof document !== "undefined";
}

function readStored(): Skin | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw === "meadow" || raw === "midnight" ? raw : null;
  } catch {
    return null;
  }
}

export function getSkin(): Skin {
  if (hasDom()) {
    const attr = document.documentElement.dataset.skin;
    if (attr === "meadow" || attr === "midnight") return attr;
  }
  return readStored() ?? "midnight";
}

export function applySkin(skin: Skin): void {
  try {
    localStorage.setItem(STORAGE_KEY, skin);
  } catch {
    // Private mode etc: the <html> attribute still applies for this visit.
  }
  if (!hasDom()) return;
  document.documentElement.dataset.skin = skin;
  document.documentElement.style.colorScheme = skin === "meadow" ? "light" : "dark";
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", THEME_COLORS[skin]);
}

/** Apply the stored (or default Midnight) skin before first paint. */
export function initSkin(): Skin {
  const skin = readStored() ?? "midnight";
  applySkin(skin);
  return skin;
}

export function toggleSkin(): Skin {
  const next: Skin = getSkin() === "meadow" ? "midnight" : "meadow";
  applySkin(next);
  return next;
}

export function themeColorFor(skin: Skin): string {
  return THEME_COLORS[skin];
}
