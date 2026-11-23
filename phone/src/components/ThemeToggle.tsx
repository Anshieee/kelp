/*
 * Light/Midnight toggle. Presentational only: flips <html data-skin>,
 * persists to localStorage, and updates the theme-color meta (see theme.ts).
 */
import { useState } from "react";
import { getSkin, toggleSkin } from "../theme";

export default function ThemeToggle({ label = true }: { label?: boolean }) {
  const [skin, setSkin] = useState(getSkin);

  const onClick = () => {
    setSkin(toggleSkin());
  };

  const next = skin === "light" ? "Midnight dark theme" : "Light theme";
  return (
    <button
      type="button"
      className="theme-toggle"
      onClick={onClick}
      aria-label={`Switch to ${next}`}
      title={`Switch to ${next}`}
    >
      <span aria-hidden="true">{skin === "light" ? "☾" : "☀"}</span>
      {label && <span className="theme-toggle-label">{skin === "light" ? "Light" : "Midnight"}</span>}
      {label && <span className="theme-toggle-hint">Change theme</span>}
    </button>
  );
}
