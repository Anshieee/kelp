import { useEffect, useRef } from "react";

/**
 * LivingBlob — the empty-state artwork, inspired by the marketing site's
 * hero field (landing/, `a-motion`/`e-fx` layers).
 *
 * The site paints its blob with a WebGL shader; a phone PWA cannot afford
 * that budget for a decorative moment, so this is a small 2D-canvas
 * re-imagining with the same ingredients: a peach → gold → mint wash,
 * concentric topographic rings that breathe, and a slow pointer-follow
 * drift. Same performance contract as the site: ~30fps, backing store well
 * below device pixels, DPR capped, fully paused when the tab is hidden, the
 * artwork is off-screen, or the user prefers reduced motion (one static
 * frame, no loop).
 */
const TAU = Math.PI * 2;

interface BlobProps {
  /** CSS size of the square artwork. Defaults to 260. */
  size?: number;
  className?: string;
}

/** Ring wobble: three incommensurate harmonics so the morph never repeats. */
function ringRadius(theta: number, t: number, level: number): number {
  return (
    1 +
    0.1 * Math.sin(3 * theta + 0.7 * t + level * 1.1) +
    0.06 * Math.sin(5 * theta - 0.5 * t + level * 1.7) +
    0.035 * Math.sin(2 * theta + 0.31 * t + level * 2.3)
  );
}

function traceRing(
  ctx: CanvasRenderingContext2D,
  cx: number,
  cy: number,
  base: number,
  t: number,
  level: number,
): void {
  const steps = 90;
  ctx.beginPath();
  for (let i = 0; i <= steps; i++) {
    const theta = (i / steps) * TAU;
    // Angular warp grows with level so inner rings swirl into eddies.
    const warp = 0.35 * level * Math.sin(theta * 2 - 0.4 * t + level * 2.0);
    const r = base * ringRadius(theta + warp, t, level);
    const x = cx + r * Math.cos(theta);
    const y = cy + r * Math.sin(theta);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.closePath();
}

export default function LivingBlob({ size = 260, className }: BlobProps) {
  const ref = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    // Cheap backing store: softness is part of the look.
    const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    const scale = dpr * 0.6;
    const W = Math.max(2, Math.round(size * scale));
    canvas.width = W;
    canvas.height = W;

    const peach = "#f6a97a";
    const gold = "#f7d974";
    const pale = "#f5edd0";
    const mint = "#7fd8a4";
    const deepMint = "#4fbf85";
    const line = "rgba(255,253,245,0.55)";

    let raf = 0;
    let last = 0;
    let running = false;
    let visible = true;
    const pointer = { x: 0, y: 0, tx: 0, ty: 0 };
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");

    function draw(t: number): void {
      const breathe = 1 + 0.018 * Math.sin(t * 0.7);
      const cx = W / 2 + pointer.x * W * 0.02;
      const cy = W / 2 + pointer.y * W * 0.02;
      const maxR = W * 0.46 * breathe;

      ctx!.clearRect(0, 0, W, W);

      // Wash: peach crown, gold belt, mint base.
      const wash = ctx!.createLinearGradient(0, cy - maxR, 0, cy + maxR);
      wash.addColorStop(0, peach);
      wash.addColorStop(0.42, gold);
      wash.addColorStop(0.68, pale);
      wash.addColorStop(0.88, mint);
      wash.addColorStop(1, deepMint);
      traceRing(ctx!, cx, cy, maxR, t, 0);
      ctx!.fillStyle = wash;
      ctx!.fill();

      // Topographic rings, shrinking inward.
      ctx!.strokeStyle = line;
      ctx!.lineWidth = Math.max(0.6, W / 320);
      const rings = 13;
      for (let i = 1; i <= rings; i++) {
        const level = i / rings;
        traceRing(ctx!, cx, cy, maxR * (1 - level * 0.82), t, level);
        ctx!.stroke();
      }
    }

    function frame(now: number): void {
      raf = 0;
      if (!visible || document.hidden) return;
      if (now - last < 33) {
        raf = requestAnimationFrame(frame);
        return;
      }
      last = now;
      // Ease pointer toward its target for the drift-follow.
      pointer.x += (pointer.tx - pointer.x) * 0.04;
      pointer.y += (pointer.ty - pointer.y) * 0.04;
      draw(now / 1000);
      raf = requestAnimationFrame(frame);
    }

    function start(): void {
      if (running || reduced.matches || document.hidden || !visible) return;
      running = true;
      last = 0;
      raf = requestAnimationFrame(frame);
    }

    function stop(): void {
      running = false;
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
    }

    const onPointer = (event: PointerEvent): void => {
      if (event.pointerType !== "mouse") return;
      const rect = canvas.getBoundingClientRect();
      pointer.tx = Math.max(-1, Math.min(1, ((event.clientX - rect.left) / rect.width) * 2 - 1));
      pointer.ty = Math.max(-1, Math.min(1, ((event.clientY - rect.top) / rect.height) * 2 - 1));
    };
    const onLeave = (): void => {
      pointer.tx = 0;
      pointer.ty = 0;
    };

    const observer = new IntersectionObserver(
      (entries) => {
        visible = entries.some((e) => e.isIntersecting);
        if (visible) start();
        else stop();
      },
      { threshold: 0.05 },
    );
    observer.observe(canvas);

    const onVisibility = (): void => {
      if (document.hidden) stop();
      else start();
    };
    const onReduced = (): void => {
      if (reduced.matches) {
        stop();
        draw(1.7); // One settled frame; no loop.
      } else {
        draw(1.7);
        start();
      }
    };

    window.addEventListener("pointermove", onPointer, { passive: true });
    document.addEventListener("mouseleave", onLeave);
    document.addEventListener("visibilitychange", onVisibility);
    reduced.addEventListener?.("change", onReduced);

    if (reduced.matches) draw(1.7);
    else start();

    return () => {
      stop();
      observer.disconnect();
      window.removeEventListener("pointermove", onPointer);
      document.removeEventListener("mouseleave", onLeave);
      document.removeEventListener("visibilitychange", onVisibility);
      reduced.removeEventListener?.("change", onReduced);
    };
  }, [size]);

  return (
    <canvas
      ref={ref}
      className={className}
      role="img"
      aria-label="Kelp assistant artwork"
      style={{ width: size, height: size }}
    />
  );
}
