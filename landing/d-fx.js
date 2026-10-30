/* Effects layer (d). Load with defer after c-copy.js, with d-fx.css beside it.
 *
 * Adds five optional effects to the existing page and never edits its markup:
 * a whole-page dither field, a hero load dissolve, roster hover accents,
 * section settles, and scroll-linked phone/pet states.
 *
 * Rules this file keeps: nothing is created before the page is parsed, every
 * effect is decorative, every failure is silent and leaves the page visible,
 * reduced motion paints one still frame and stops, and nothing runs while the
 * tab is hidden or while the page's own "Pause motion" is on.
 */
(() => {
  "use strict";

  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const CANVAS = typeof HTMLCanvasElement === "function";
  const BAYER = [0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5];

  const clamp = (value, min = 0, max = 1) =>
    value < min ? min : value > max ? max : value;
  const smooth = (t) => t * t * (3 - 2 * t);
  const pause = () => document.body?.classList.contains("motion-paused");
  const running = () => !document.hidden && !reduced.matches && !pause();

  /* The one-tag install: if only the script is included, take the stylesheet
     with it. Harmless when the page already links d-fx.css itself. */
  function stylesheet() {
    try {
      const linked = [...document.querySelectorAll('link[rel="stylesheet"]')];
      if (linked.some((link) => (link.getAttribute("href") || "").includes("d-fx.css")))
        return;
      const link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = "d-fx.css";
      document.head.append(link);
    } catch {
      /* A missing stylesheet only costs decoration. */
    }
  }

  /* ---------------------------------------------------------------- field --
   * Section palettes: two inks and one motif each, in document order. The two
   * inks blend across the tile so a section reads as one colour temperature.
   * Motifs are marbled veins, cellular dots, tonal bands, and soft cloud. */
  const SECTIONS = [
    [".hero", 0, [246, 169, 122], [247, 217, 116]],
    [".c-computer", 1, [143, 208, 216], [46, 139, 103]],
    [".c-examples", 2, [247, 217, 116], [246, 169, 122]],
    [".conversation", 3, [214, 232, 216], [143, 208, 216]],
    [".c-comparison", 1, [234, 223, 242], [115, 85, 59]],
    [".marquee-wrap", 2, [228, 87, 46], [247, 217, 116]],
    [".possibilities", 3, [220, 235, 247], [46, 139, 103]],
    [".c-companion", 1, [252, 225, 216], [115, 85, 59]],
    [".trust", 0, [214, 232, 216], [46, 139, 103]],
    [".closing", 2, [247, 217, 116], [228, 87, 46]],
    [".site-footer", 3, [115, 85, 59], [251, 249, 245]],
  ];

  /* A 128px tile in canvas pixels, repeating on a 384px grid on the page.
     Every function below is periodic over the tile, so the repeat has no seam;
     a per-pixel hash then breaks the lattice into grain. */
  function tile(from, to, motif) {
    const size = 128;
    const source = document.createElement("canvas");
    source.width = source.height = size;
    const context = source.getContext("2d", { alpha: true });
    if (!context) return null;
    const image = context.createImageData(size, size);
    const data = image.data;
    const tau = Math.PI * 2;
    for (let y = 0; y < size; y++) {
      const v = y / size;
      for (let x = 0; x < size; x++) {
        const u = x / size;
        let density;
        if (motif === 0) {
          // Marbled veins: nested sines, all whole cycles across the tile.
          const cells =
            Math.sin(tau * (u + Math.sin(tau * v) * 0.18)) *
            Math.cos(tau * (v * 2 + Math.sin(tau * u) * 0.22));
          const marble = Math.sin(tau * (u * 2 + v + cells * 0.35));
          density = 0.12 + Math.pow(Math.abs(marble), 8) * 0.5;
        } else if (motif === 1) {
          // Cellular halftone: one disc per lattice cell.
          const gx = u * 8;
          const gy = v * 8;
          const distance = Math.hypot(gx - Math.floor(gx) - 0.5, gy - Math.floor(gy) - 0.5);
          density = clamp(0.52 - distance * 1.25, 0, 0.6);
        } else if (motif === 2) {
          // Tonal bands, drifting with the tile.
          const band = Math.sin(tau * (v * 4 + Math.sin(tau * u) * 0.12));
          density =
            0.09 +
            Math.pow(Math.abs(band), 3) * 0.3 * (0.55 + 0.45 * Math.sin(tau * u * 2));
        } else {
          // Soft cloud.
          const cloud =
            Math.sin(tau * u + Math.sin(tau * v * 2) * 0.5) *
            Math.cos(tau * v * 2 + Math.sin(tau * u) * 0.4);
          density = 0.07 + (cloud * 0.5 + 0.5) * 0.3;
        }
        // Ordered threshold plus a little per-pixel scatter: dither, not weave.
        const scatter = Math.abs(Math.sin(x * 12.9898 + y * 78.233) * 43758.5453) % 1;
        const threshold =
          (BAYER[(y % 4) * 4 + (x % 4)] + 0.5) / 16 + (scatter - 0.5) * 0.18;
        if (density <= threshold) continue;
        const mix = 0.5 + 0.5 * Math.sin(tau * v);
        const index = (y * size + x) * 4;
        for (let channel = 0; channel < 3; channel++) {
          data[index + channel] =
            from[channel] + (to[channel] - from[channel]) * mix;
        }
        data[index + 3] = 255;
      }
    }
    context.putImageData(image, 0, 0);
    return source;
  }

  function field() {
    if (!CANVAS) return;
    const canvas = document.createElement("canvas");
    canvas.id = "d-fx-field";
    canvas.setAttribute("aria-hidden", "true");
    const context = canvas.getContext("2d", { alpha: true });
    if (!context) return;

    const entries = SECTIONS.map(([selector, motif, from, to]) => {
      const element = document.querySelector(selector);
      if (!element) return null;
      const source = tile(from, to, motif);
      const pattern = source && context.createPattern(source, "repeat");
      if (!pattern) return null;
      return { element, pattern, top: 0, height: 1, end: 0 };
    }).filter(Boolean);
    if (!entries.length) return;

    document.body.append(canvas);

    // Three CSS pixels per dither pixel: the grain stays chunky and the
    // backing store never grows with device pixel ratio.
    const CELL = 3;
    let width = 0;
    let height = 0;
    let frame = 0;
    let last = 0;
    let start = performance.now();
    let time = 0;

    function measure() {
      entries.forEach((entry) => {
        const rect = entry.element.getBoundingClientRect();
        entry.top = rect.top + window.scrollY;
        entry.height = Math.max(1, rect.height);
        entry.end = entry.top + entry.height;
      });
      entries.sort((a, b) => a.top - b.top);
    }

    function resize() {
      width = Math.max(1, Math.ceil(window.innerWidth / CELL));
      height = Math.max(1, Math.ceil(window.innerHeight / CELL));
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      measure();
      draw();
    }

    /* Which two sections the middle of the viewport sits between, and how far
       the crossfade has come. The blend completes exactly at the boundary, so
       the change of ink reads as the section arriving, not as a switch. */
    function blend() {
      const middle = window.scrollY + window.innerHeight * 0.5;
      let index = 0;
      entries.forEach((entry, position) => {
        if (middle >= entry.top) index = position;
      });
      const current = entries[index];
      const next = entries[index + 1];
      if (!next) return [current, current, 0];
      const span = Math.max(1, current.height * 0.6);
      const progress = clamp((middle - (current.end - span)) / span);
      return [current, next, smooth(progress)];
    }

    function fill(pattern, seconds) {
      const driftX = Math.sin(seconds * 0.09) * 3;
      const driftY = Math.cos(seconds * 0.07) * 2.4;
      try {
        pattern.setTransform(new DOMMatrix([1, 0, 0, 1, driftX, driftY]));
      } catch {
        /* Without pattern transforms the grain simply holds still. */
      }
      context.fillStyle = pattern;
      context.fillRect(0, 0, width, height);
    }

    function draw() {
      if (!context || !canvas.isConnected) return;
      const [current, next, progress] = blend();
      context.clearRect(0, 0, width, height);
      context.globalAlpha = 1 - progress;
      fill(current.pattern, time);
      if (progress > 0.002) {
        context.globalAlpha = progress;
        fill(next.pattern, time);
      }
      context.globalAlpha = 1;
    }

    function tick(now) {
      frame = 0;
      if (!running()) return;
      frame = requestAnimationFrame(tick);
      const interval = 1000 / 30;
      if (last && now - last < interval) return;
      last = now - ((now - last) % interval);
      time = (now - start) / 1000;
      draw();
    }

    function sync() {
      cancelAnimationFrame(frame);
      frame = 0;
      last = 0;
      if (running()) frame = requestAnimationFrame(tick);
      else draw();
    }

    window.addEventListener("resize", resize, { passive: true });
    window.addEventListener("pageshow", sync);
    document.addEventListener("visibilitychange", sync);
    reduced.addEventListener("change", sync);
    new MutationObserver(sync).observe(document.body, {
      attributes: true,
      attributeFilter: ["class"],
    });
    if (window.ResizeObserver) {
      const main = document.querySelector("main") || document.body;
      let timer = 0;
      new ResizeObserver(() => {
        clearTimeout(timer);
        timer = setTimeout(() => {
          measure();
          draw();
        }, 180);
      }).observe(main);
    }
    try {
      document.fonts?.ready.then(() => {
        measure();
        draw();
      });
    } catch {
      /* Font metrics only refine the section boundaries. */
    }
    start = performance.now();
    resize();
    sync();
  }

  /* -------------------------------------------------------------- dissolve --
   * The hero assembles through a bayer dissolve on every reload: a cream
   * canvas over the hero, drawn at seven CSS pixels per cell, whose cells fall
   * away in dither order. It holds covered until the page's own load veil has
   * lifted, so the two reveal in sequence rather than under each other. Any
   * input, a watchdog timer, an error, or a hidden tab removes it outright.
   * It is never created for reduced motion. */
  function dissolve() {
    if (!CANVAS || reduced.matches || document.hidden) return;
    const hero = document.querySelector(".hero");
    if (!hero || hero.clientWidth < 60 || hero.clientHeight < 60) return;

    let canvas;
    let context;
    try {
      canvas = document.createElement("canvas");
      canvas.id = "d-fx-dissolve";
      canvas.setAttribute("aria-hidden", "true");
      context = canvas.getContext("2d", { alpha: true });
    } catch {
      return;
    }
    if (!context) return;

    const CELL = 7;
    // The dissolve itself, timed for the space after the page's own veil.
    const HOLD = 0;
    const SPAN = 520;
    const width = Math.max(1, Math.ceil(hero.clientWidth / CELL));
    const height = Math.max(1, Math.ceil(hero.clientHeight / CELL));
    canvas.width = width;
    canvas.height = height;

    const canvasStyle = getComputedStyle(document.documentElement);
    const declared = (canvasStyle.getPropertyValue("--canvas") || "").trim();
    const cream = /^#([0-9a-f]{6})$/i.exec(declared);
    const ink = cream
      ? [
          parseInt(cream[1].slice(0, 2), 16),
          parseInt(cream[1].slice(2, 4), 16),
          parseInt(cream[1].slice(4, 6), 16),
        ]
      : [251, 249, 245];

    // One cream buffer, built once; each frame only opens cells.
    const image = context.createImageData(width, height);
    const data = image.data;
    const keys = new Float32Array(width * height);
    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        const index = y * width + x;
        const noise = Math.abs(Math.sin((x * 127.1 + y * 311.7) * 0.017)) % 1;
        keys[index] =
          ((BAYER[(y % 4) * 4 + (x % 4)] + 0.5) / 16) * 0.56 +
          (1 - y / height) * 0.22 +
          noise * 0.22;
        const pixel = index * 4;
        data[pixel] = ink[0];
        data[pixel + 1] = ink[1];
        data[pixel + 2] = ink[2];
        data[pixel + 3] = 255;
      }
    }

    let frame = 0;
    let start = 0;
    let started = false;
    let finished = false;
    let watcher = null;
    let cap = 0;
    const watchdog = setTimeout(finish, 2200);

    function finish() {
      if (finished) return;
      finished = true;
      clearTimeout(watchdog);
      clearTimeout(cap);
      cancelAnimationFrame(frame);
      frame = 0;
      watcher?.disconnect();
      window.removeEventListener("pointerdown", finish, true);
      window.removeEventListener("keydown", finish, true);
      window.removeEventListener("wheel", finish, true);
      window.removeEventListener("touchstart", finish, true);
      window.removeEventListener("scroll", finish, true);
      window.removeEventListener("error", finish);
      document.removeEventListener("visibilitychange", watch);
      reduced.removeEventListener("change", finish);
      canvas.remove();
    }

    function watch() {
      if (document.hidden) finish();
    }

    /* Start once the page's own veil has lifted, so the dissolve is seen
       rather than hidden behind it. With no veil, start at once; the cap
       keeps a stuck veil from holding a covered hero. */
    function begin() {
      if (started || finished) return;
      started = true;
      watcher?.disconnect();
      start = performance.now();
      frame = requestAnimationFrame(paint);
    }

    function paint(now) {
      frame = 0;
      if (finished) return;
      const progress = clamp((now - start - HOLD) / SPAN);
      const cut = smooth(progress) * 1.18;
      for (let index = 0; index < keys.length; index++) {
        data[index * 4 + 3] = keys[index] < cut ? 0 : 255;
      }
      context.putImageData(image, 0, 0);
      if (progress >= 1) {
        finish();
        return;
      }
      frame = requestAnimationFrame(paint);
    }

    // The buffer already reads opaque cream: draw it before the veil leaves.
    context.putImageData(image, 0, 0);

    try {
      hero.append(canvas);
      window.addEventListener("pointerdown", finish, { capture: true, passive: true });
      window.addEventListener("keydown", finish, true);
      window.addEventListener("wheel", finish, { capture: true, passive: true });
      window.addEventListener("touchstart", finish, { capture: true, passive: true });
      window.addEventListener("scroll", finish, { capture: true, passive: true });
      window.addEventListener("error", finish, { once: true });
      document.addEventListener("visibilitychange", watch);
      reduced.addEventListener("change", finish);
      const veil = document.querySelector(".a-load-veil");
      if (veil) {
        watcher = new MutationObserver(() => {
          if (!veil.isConnected) begin();
        });
        watcher.observe(document.body, { childList: true });
        cap = setTimeout(begin, 1200);
      } else begin();
    } catch {
      finish();
    }
  }

  /* -------------------------------------------------------------- settles --
   * Threshold-based ticks: each shell draws its rule once, on first entry.
   * Content already in view is settled immediately, never faded in. */
  function settles() {
    if (!document.body) return;
    document.documentElement.classList.add("d-fx");
    const shells = [...document.querySelectorAll(".section-shell")];
    if (!shells.length) return;
    if (reduced.matches || !("IntersectionObserver" in window)) {
      shells.forEach((shell) => shell.classList.add("d-in"));
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          entry.target.classList.add("d-in");
          observer.unobserve(entry.target);
        });
      },
      { threshold: 0.12, rootMargin: "0px 0px -8% 0px" },
    );
    shells.forEach((shell) => {
      if (shell.getBoundingClientRect().top < window.innerHeight * 0.92) {
        shell.classList.add("d-in");
        return;
      }
      observer.observe(shell);
    });
  }

  /* ---------------------------------------------------------------- phone --
   * Activates on .phone-mockup or [data-phone-mockup] whenever that markup
   * arrives. Rotates up into place on entry and away on exit, on one axis, so
   * the page cannot gain horizontal overflow. */
  function phones() {
    const nodes = [...document.querySelectorAll(".phone-mockup, [data-phone-mockup]")];
    if (!nodes.length || reduced.matches || !("IntersectionObserver" in window))
      return;

    const visible = new WeakSet();
    let frame = 0;

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            visible.add(entry.target);
            entry.target.style.willChange = "transform";
          } else {
            visible.delete(entry.target);
            entry.target.style.willChange = "";
          }
        });
        paint();
      },
      { rootMargin: "120px 0px" },
    );

    function paint() {
      frame = 0;
      if (reduced.matches) return;
      nodes.forEach((node) => {
        if (!visible.has(node)) return;
        const rect = node.getBoundingClientRect();
        const progress = clamp(
          (window.innerHeight - rect.top) / Math.max(1, window.innerHeight + rect.height),
        );
        const enter = smooth(clamp(progress / 0.42));
        const exit = smooth(clamp((progress - 0.72) / 0.28));
        const tilt = -9 * (1 - enter) + 7 * exit;
        const scale = 0.964 + 0.036 * enter - 0.012 * exit;
        node.style.transform = `perspective(1400px) rotateX(${tilt.toFixed(2)}deg) scale(${scale.toFixed(3)})`;
      });
    }

    function request() {
      if (!frame && !document.hidden && !pause()) frame = requestAnimationFrame(paint);
    }

    nodes.forEach((node) => {
      observer.observe(node);
    });
    window.addEventListener("scroll", request, { passive: true });
    window.addEventListener("resize", request, { passive: true });
    document.addEventListener("visibilitychange", request);
    reduced.addEventListener("change", () => {
      if (!reduced.matches) return;
      nodes.forEach((node) => {
        node.style.transform = "";
        node.style.willChange = "";
      });
    });
    request();
  }

  /* ----------------------------------------------------------------- boot --
   * Each effect owns its own failure: a broken one must not stop the others
   * or the page's own renderer. */
  function boot() {
    stylesheet();
    [
      field,
      dissolve,
      settles,
      phones,
    ].forEach((effect) => {
      try {
        effect();
      } catch {
        // Optional decoration must never conceal or stop the page.
        document.getElementById("d-fx-dissolve")?.remove();
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else boot();
})();
