/* Include with defer, before app.js, plus a-motion.css after styles.css.
 * The original renderer still owns the canvas, shader, 30fps draw budget and
 * DPR cap (1.5). This layer only composites that artwork; it never redraws it.
 */
(() => {
  "use strict";

  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const mouse = matchMedia("(hover: hover) and (pointer: fine)");

  function loadVeil() {
    if (reduced.matches || document.hidden) return;
    const veil = document.createElement("div");
    veil.className = "a-load-veil";
    veil.setAttribute("aria-hidden", "true");
    // Arm cleanup before insertion. CSS also ends hidden, even if JS fails.
    const timeout = setTimeout(remove, 1000);
    function remove() {
      clearTimeout(timeout);
      veil.remove();
      window.removeEventListener("pointerdown", remove, true);
      window.removeEventListener("click", remove, true);
      window.removeEventListener("keydown", remove, true);
      window.removeEventListener("error", remove);
      document.removeEventListener("focusin", remove);
      reduced.removeEventListener("change", remove);
    }
    try {
      veil.addEventListener("animationend", remove, { once: true });
      window.addEventListener("pointerdown", remove, { capture: true, passive: true });
      window.addEventListener("click", remove, { capture: true, passive: true });
      window.addEventListener("keydown", remove, true);
      window.addEventListener("error", remove, { once: true });
      document.addEventListener("focusin", remove, { once: true });
      reduced.addEventListener("change", remove, { once: true });
      document.body.append(veil);
    } catch {
      remove();
    }
  }

  function ambientField() {
    const art = document.querySelector(".hero-art");
    const hero = art?.closest(".hero");
    let canvas = document.getElementById("kelp-field");
    if (!art || !hero || !canvas) return;

    const interval = 1000 / 30;
    let frame = 0;
    let last = 0;
    let previous = 0;
    let time = 0;
    let visible = false;
    let targetX = 0;
    let targetY = 0;
    let x = 0;
    let y = 0;

    function canRun() {
      return visible && !document.hidden && !reduced.matches &&
        !document.body.classList.contains("motion-paused");
    }

    function tick(now) {
      frame = 0;
      if (!canRun()) return;
      frame = requestAnimationFrame(tick);
      if (last && now - last < interval) return;
      const elapsed = previous ? Math.min(now - previous, 64) : interval;
      previous = now;
      last = now - ((now - last) % interval);
      time += elapsed / 1000;

      // app.js may substitute its static canvas after WebGL/context failure.
      if (!canvas.isConnected) {
        canvas = document.getElementById("kelp-field");
        if (!canvas) {
          cancelAnimationFrame(frame);
          frame = 0;
          return;
        }
      }
      if (!canvas.classList.contains("a-field-active")) {
        canvas.classList.add("a-field-active");
      }
      const follow = 1 - Math.exp(-elapsed / 180);
      x += (targetX - x) * follow;
      y += (targetY - y) * follow;

      // Two slow, independent tides sit beneath the original shader's finer
      // dither warp. Total travel stays under 8px; the copy never moves.
      const tideX = Math.sin(time * 0.16) * 2.6 + Math.sin(time * 0.29) * 1.2;
      const tideY = Math.sin(time * 0.13) * 1.8 + Math.sin(time * 0.23) * 0.8;
      const roll = Math.sin(time * 0.11) * 0.24;
      canvas.style.transform = `translate3d(${tideX + x}px, ${tideY + y}px, 0) rotate(${roll}deg) scale(1.024)`;
    }

    function sync() {
      cancelAnimationFrame(frame);
      frame = 0;
      last = 0;
      previous = 0;
      canvas.classList.remove("a-field-active");
      if (reduced.matches) {
        time = x = y = targetX = targetY = 0;
        canvas.style.removeProperty("transform");
      }
      if (canRun()) frame = requestAnimationFrame(tick);
    }

    function releasePointer() {
      targetX = targetY = 0;
    }

    hero.addEventListener("pointermove", (event) => {
      if (!canRun() || !mouse.matches || event.pointerType !== "mouse") return;
      const rect = hero.getBoundingClientRect();
      targetX = Math.max(-1, Math.min(1, (event.clientX - rect.left) / rect.width * 2 - 1)) * 3.5;
      targetY = Math.max(-1, Math.min(1, (event.clientY - rect.top) / rect.height * 2 - 1)) * 2.5;
    }, { passive: true });
    hero.addEventListener("pointerleave", releasePointer, { passive: true });
    mouse.addEventListener("change", releasePointer);
    reduced.addEventListener("change", sync);
    document.addEventListener("visibilitychange", () => {
      releasePointer();
      sync();
    });
    new MutationObserver(sync).observe(document.body, {
      attributes: true,
      attributeFilter: ["class"],
    });
    if ("IntersectionObserver" in window) {
      new IntersectionObserver(([entry]) => {
        visible = entry.isIntersecting;
        sync();
      }).observe(art);
    }
    // Without IntersectionObserver leave the original artwork untouched.
  }

  function init() {
    loadVeil();
    try {
      ambientField();
    } catch {
      // Optional motion must never stop app.js or conceal the page.
      document.querySelector(".a-load-veil")?.remove();
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
