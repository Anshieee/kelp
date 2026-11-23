/*
 * Light/Midnight skin for the phone app, matching the Kelp marketing site's
 * design language (light-first). Light is the default; the choice is persisted
 * to localStorage and reflected on <html data-skin> plus the theme-color meta
 * so the OS chrome matches. No API or routing impact.
 *
 * Unknown stored values (including the retired "meadow" skin) fall back to
 * light so old prefs never crash or strand the app on a missing skin.
 */

export type Skin = "light" | "midnight";

const STORAGE_KEY = "kelp.phone.skin";

/** theme-color values: site canvas for light, dark chrome for midnight. */
const THEME_COLORS: Record<Skin, string> = {
  light: "#fbf9f5",
  midnight: "#141414",
};

function hasDom(): boolean {
  return typeof document !== "undefined";
}

function readStored(): Skin | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw === "light" || raw === "midnight" ? raw : null;
  } catch {
    return null;
  }
}

export function getSkin(): Skin {
  if (hasDom()) {
    const attr = document.documentElement.dataset.skin;
    if (attr === "light" || attr === "midnight") return attr;
  }
  return readStored() ?? "light";
}

export function applySkin(skin: Skin): void {
  try {
    localStorage.setItem(STORAGE_KEY, skin);
  } catch {
    // Private mode etc: the <html> attribute still applies for this visit.
  }
  if (!hasDom()) return;
  document.documentElement.dataset.skin = skin;
  document.documentElement.style.colorScheme = skin === "light" ? "light" : "dark";
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", THEME_COLORS[skin]);
}

/** Apply the stored (or default light) skin before first paint. */
export function initSkin(): Skin {
  const skin = readStored() ?? "light";
  applySkin(skin);
  return skin;
}

export function toggleSkin(): Skin {
  const next: Skin = getSkin() === "light" ? "midnight" : "light";
  applySkin(next);
  return next;
}

export function themeColorFor(skin: Skin): string {
  return THEME_COLORS[skin];
}
