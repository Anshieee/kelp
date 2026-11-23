/*
 * SproutMark: the Kelp sprout identity, inlined from the marketing site's
 * `sprout` SVG symbol (landing/index.html). Same viewBox (0 0 32 40),
 * fill:none, stroke:currentColor, round caps — so it inherits text color.
 * Replaces the old blue rounded-square avatar motif in app chrome.
 */

interface SproutMarkProps {
  /** Rendered height in px; width follows the 32:40 viewBox ratio. */
  size?: number;
  className?: string;
}

export default function SproutMark({ size = 22, className }: SproutMarkProps) {
  return (
    <svg
      viewBox="0 0 32 40"
      width={(size * 32) / 40}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      className={className}
    >
      <path d="M16 37V6M16 28C3 28 1 19 5 12c10 0 13 8 11 16Zm0-10C29 18 31 9 27 2 17 2 14 10 16 18Z" />
    </svg>
  );
}
