import { useEffect, useRef } from "react";

/**
 * LivingBlob — the empty-state artwork: the actual hero blob captured from
 * the marketing site's live field art (`landing/` `#kelp-field` canvas,
 * cropped to its opaque bounds), kept alive with motion.
 *
 * Full WebGL-style morphing would cost a phone GPU for a decorative moment,
 * so the artwork moves the cheap way: a slow breathe (scale + faint roll, on
 * incommensurate periods so it never visibly loops) plus a pointer drift
 * that eases the blob a few pixels toward the cursor. The earlier
 * canvas-drawn sketch is gone; this file keeps its name and props so callers
 * do not change.
 *
 * Motion honors the same contract as the site: transform/opacity only (the
 * compositor does the work, no layout thrash), frozen under
 * `prefers-reduced-motion`, and parked while the artwork is off-screen.
 */
export default function LivingBlob({ size = 260, className }: { size?: number; className?: string }) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    const pointer = { x: 0, y: 0, tx: 0, ty: 0 };
    let raf = 0;
    let last = 0;
    let running = false;

    const onMove = (event: PointerEvent) => {
      if (event.pointerType !== "mouse") return;
      const rect = node.getBoundingClientRect();
      pointer.tx = Math.max(-1, Math.min(1, ((event.clientX - rect.left) / rect.width) * 2 - 1));
      pointer.ty = Math.max(-1, Math.min(1, ((event.clientY - rect.top) / rect.height) * 2 - 1));
    };
    const onLeave = () => {
      pointer.tx = 0;
      pointer.ty = 0;
    };

    const frame = (now: number) => {
      raf = 0;
      if (document.hidden) {
        raf = requestAnimationFrame(frame);
        return;
      }
      if (now - last < 50) {
        raf = requestAnimationFrame(frame);
        return;
      }
      last = now;
      pointer.x += (pointer.tx - pointer.x) * 0.06;
      pointer.y += (pointer.ty - pointer.y) * 0.06;
      node.style.setProperty("--drift-x", `${(pointer.x * 7).toFixed(2)}px`);
      node.style.setProperty("--drift-y", `${(pointer.y * 5).toFixed(2)}px`);
      raf = requestAnimationFrame(frame);
    };

    const start = () => {
      if (running || document.hidden) return;
      running = true;
      last = 0;
      raf = requestAnimationFrame(frame);
    };
    const stop = () => {
      running = false;
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
    };

    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) start();
        else stop();
      },
      { threshold: 0.05 },
    );
    observer.observe(node);

    const onVisibility = () => (document.hidden ? stop() : start());
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("pointermove", onMove, { passive: true });
    document.addEventListener("mouseleave", onLeave);

    start();
    return () => {
      stop();
      observer.disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("pointermove", onMove);
      document.removeEventListener("mouseleave", onLeave);
    };
  }, []);

  return (
    <div ref={ref} className={className ? `hero-blob ${className}` : "hero-blob"} role="img" aria-label="Kelp assistant artwork" style={{ width: size, height: size }}>
      <img src="/phone/blob.png" alt="" aria-hidden="true" draggable={false} />
    </div>
  );
}
