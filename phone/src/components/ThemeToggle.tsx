/*
 * Midnight/Meadow toggle. Presentational only: flips <html data-skin>,
 * persists to localStorage, and updates the theme-color meta (see theme.ts).
 */
import { useState } from "react";
import { getSkin, toggleSkin } from "../theme";

export default function ThemeToggle({ label = true }: { label?: boolean }) {
  const [skin, setSkin] = useState(getSkin);

  const onClick = () => {
    setSkin(toggleSkin());
  };

  const next = skin === "meadow" ? "Midnight dark theme" : "Meadow light theme";
  return (
    <button
      type="button"
      className="theme-toggle"
      onClick={onClick}
      aria-label={`Switch to ${next}`}
      title={`Switch to ${next}`}
    >
      <span aria-hidden="true">◐</span>
      {label && <span className="theme-toggle-label">{skin === "meadow" ? "Meadow" : "Midnight"}</span>}
      {label && <span className="theme-toggle-hint">Change theme</span>}
    </button>
  );
}
