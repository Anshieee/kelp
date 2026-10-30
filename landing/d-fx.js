/* Effects layer (d). Load with defer after c-copy.js, with d-fx.css beside it.
 *
 * Adds five optional effects to the existing page and never edits its markup:
 * a whole-page dither field that turns to mosaic under the pointer, a hero
 * load dissolve, roster hover accents, section settles, and scroll-linked
 * phone/pet states.
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

  /* cubic-bezier(x1, y1, x2, y2) as a 128-step table: exact enough for a
     reveal, and cheaper than solving the curve on every frame. */
  function bezier(x1, y1, x2, y2) {
    const steps = 128;
    const table = new Float32Array(steps + 1);
    const at = (t, a, b) =>
      3 * a * t * (1 - t) * (1 - t) + 3 * b * t * t * (1 - t) + t * t * t;
    for (let index = 0; index <= steps; index++) {
      const x = index / steps;
      let low = 0;
      let high = 1;
      for (let pass = 0; pass < 22; pass++) {
        const middle = (low + high) / 2;
        if (at(middle, x1, x2) < x) low = middle;
        else high = middle;
      }
      table[index] = at((low + high) / 2, y1, y2);
    }
    return (x) => {
      const position = clamp(x) * steps;
      const index = Math.min(steps - 1, Math.floor(position));
      return table[index] + (table[index + 1] - table[index]) * (position - index);
    };
  }

  // CSS ease-in-out, so the reveal settles in and out instead of snapping.
  const REVEAL_EASE = bezier(0.42, 0, 0.58, 1);

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

  /* One density field feeds both registers: the grain samples it at every
     canvas pixel, the mosaic samples it once per brick, so the two agree about
     where the ink sits even though one is eight times coarser. Every term is
     periodic over the tile, so the repeat has no seam. */
  const TILE = 128;        // canvas px: repeats every 384 CSS px on the page
  const MOSAIC_CELL = 8;   // canvas px: 24 CSS px bricks, seams included

  function densityAt(motif, u, v) {
    const tau = Math.PI * 2;
    if (motif === 0) {
      // Marbled veins: nested sines, all whole cycles across the tile.
      const cells =
        Math.sin(tau * (u + Math.sin(tau * v) * 0.18)) *
        Math.cos(tau * (v * 2 + Math.sin(tau * u) * 0.22));
      const marble = Math.sin(tau * (u * 2 + v + cells * 0.35));
      return 0.12 + Math.pow(Math.abs(marble), 8) * 0.5;
    }
    if (motif === 1) {
      // Cellular halftone: one disc per lattice cell.
      const gx = u * 8;
      const gy = v * 8;
      const distance = Math.hypot(gx - Math.floor(gx) - 0.5, gy - Math.floor(gy) - 0.5);
      return clamp(0.52 - distance * 1.25, 0, 0.6);
    }
    if (motif === 2) {
      // Tonal bands, drifting with the tile.
      const band = Math.sin(tau * (v * 4 + Math.sin(tau * u) * 0.12));
      return (
        0.09 +
        Math.pow(Math.abs(band), 3) * 0.3 * (0.55 + 0.45 * Math.sin(tau * u * 2))
      );
    }
    // Soft cloud.
    const cloud =
      Math.sin(tau * u + Math.sin(tau * v * 2) * 0.5) *
      Math.cos(tau * v * 2 + Math.sin(tau * u) * 0.4);
    return 0.07 + (cloud * 0.5 + 0.5) * 0.3;
  }

  /* Grain: the density thresholded per pixel, ordered through bayer, with a
     per-pixel hash to break the lattice. */
  function tile(from, to, motif) {
    const size = TILE;
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
        const weight = densityAt(motif, u, v);
        // Ordered threshold plus a little per-pixel scatter: dither, not weave.
        const scatter = Math.abs(Math.sin(x * 12.9898 + y * 78.233) * 43758.5453) % 1;
        const threshold =
          (BAYER[(y % 4) * 4 + (x % 4)] + 0.5) / 16 + (scatter - 0.5) * 0.18;
        if (weight <= threshold) continue;
        const mix = 0.5 + 0.5 * Math.sin(tau * v);
        const index = (y * size + x) * 4;
        for (let channel = 0; channel < 3; channel++) {
          data[index + channel] = from[channel] + (to[channel] - from[channel]) * mix;
        }
        data[index + 3] = 255;
      }
    }
    context.putImageData(image, 0, 0);
    return source;
  }

  /* Mosaic: the same density read one brick at a time, in running bond. Each
     brick is a cut of the section ink with its own slight shade, the ground
     shows through a one-pixel seam, and the occasional chipped brick leaves a
     corner open — tile, not grid. */
  function mosaicTile(from, to, motif) {
    const size = TILE;
    const cell = MOSAIC_CELL;
    const source = document.createElement("canvas");
    source.width = source.height = size;
    const context = source.getContext("2d", { alpha: true });
    if (!context) return null;
    const rows = Math.round(size / cell);
    const wide = cell * 2;
    const seam = 1;
    const tau = Math.PI * 2;

    for (let row = 0; row < rows; row++) {
      const y = row * cell;
      const offset = row % 2 ? cell : 0;
      for (let column = 0; column < size / wide; column++) {
        const x = column * wide + offset;
        // Sample the brick centre, wrapped so the repeat stays continuous.
        const u = ((((x + wide / 2) % size) + size) % size) / size;
        const v = (y + cell / 2) / size;
        const weight = densityAt(motif, u, v);
        const hash =
          Math.abs(Math.sin((x + 13.7) * 91.3 + (y + 7.1) * 47.7) * 24634.6345) % 1;
        // A brick the density barely reaches is left out, so the patch keeps
        // the section's shape instead of becoming a flat sheet of tiles.
        if (weight < 0.11) continue;

        const mix = 0.5 + 0.5 * Math.sin(tau * v);
        // Denser ink sets as a deeper brick: the mosaic reads as tone, not
        // just as texture.
        const shade = 0.94 - weight * 0.3 - hash * 0.05;
        const ink = [0, 1, 2].map((channel) =>
          Math.max(
            0,
            Math.min(
              255,
              Math.round(
                (from[channel] + (to[channel] - from[channel]) * mix) * shade,
              ),
            ),
          ),
        );
        context.fillStyle = `rgb(${ink[0]}, ${ink[1]}, ${ink[2]})`;

        // Bricks in the thin parts of the motif are cut smaller: the joints
        // widen and the ground comes through, without breaking the bond.
        const chip =
          weight < 0.3 ? cell * 0.3 : hash > 0.86 ? cell * 0.22 : 0;
        const brickWidth = wide - seam - chip;
        const brickHeight = cell - seam - chip;
        context.fillRect(x, y, brickWidth, brickHeight);
        // A brick that runs off the tile edge repeats on the other side.
        if (x + wide > size) context.fillRect(x - size, y, brickWidth, brickHeight);
      }
    }
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
      const grain = tile(from, to, motif);
      const bricks = mosaicTile(from, to, motif);
      const pattern = grain && context.createPattern(grain, "repeat");
      if (!pattern) return null;
      const mosaic = bricks && context.createPattern(bricks, "repeat");
      return {
        element,
        pattern,
        mosaic: mosaic || pattern,
        top: 0,
        height: 1,
        end: 0,
      };
    }).filter(Boolean);
    if (!entries.length) return;

    const scratch = document.createElement("canvas");
    const paint = scratch.getContext("2d", { alpha: true });
    if (!paint) return;

    document.body.append(canvas);

    /* Three CSS pixels per canvas pixel: a hard cap on the backing store, so a
       2x or 3x display never pays for a larger field. */
    const CELL = 3;
    const FOCUS = 210; // CSS px of mosaic reach around the pointer

    let width = 1;
    let height = 1;
    let frame = 0;
    let last = 0;
    let start = performance.now();
    let time = 0;
    let driftX = 0;
    let driftY = 0;
    let frames = 0;
    let cost = 0;

    // Focus patch in CSS pixels, plus how strongly it is showing.
    const focus = { x: 0, y: 0, e: 0 };
    const target = { x: 0, y: 0, on: 0 };
    let moved = -Infinity;

    const fine = matchMedia("(hover: hover) and (pointer: fine)");
    let radius = 70; // canvas px
    let mask = null;

    /* A soft radial mask, built once per size: full strength in the middle,
       easing to nothing at the rim, so the mosaic arrives as a patch rather
       than as a disc edge. */
    function buildMask() {
      const wanted = Math.min(
        FOCUS,
        Math.min(window.innerWidth, window.innerHeight) * 0.46,
      );
      radius = Math.max(36, Math.round(wanted / CELL));
      const size = radius * 2;
      if (scratch.width !== size || scratch.height !== size) {
        scratch.width = size;
        scratch.height = size;
      }
      if (mask && mask.width === size) return;
      mask = document.createElement("canvas");
      mask.width = mask.height = size;
      const ink = mask.getContext("2d", { alpha: true });
      if (!ink) return;
      const gradient = ink.createRadialGradient(
        radius,
        radius,
        0,
        radius,
        radius,
        radius,
      );
      gradient.addColorStop(0, "rgba(0, 0, 0, 1)");
      gradient.addColorStop(0.42, "rgba(0, 0, 0, 0.94)");
      gradient.addColorStop(0.66, "rgba(0, 0, 0, 0.62)");
      gradient.addColorStop(0.84, "rgba(0, 0, 0, 0.24)");
      gradient.addColorStop(0.95, "rgba(0, 0, 0, 0.05)");
      gradient.addColorStop(1, "rgba(0, 0, 0, 0)");
      ink.fillStyle = gradient;
      ink.fillRect(0, 0, size, size);
    }

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
      buildMask();
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

    function fill(ctx, pattern) {
      try {
        pattern.setTransform(new DOMMatrix([1, 0, 0, 1, driftX, driftY]));
      } catch {
        /* Without pattern transforms the grain simply holds still. */
      }
      ctx.fillStyle = pattern;
      ctx.fillRect(0, 0, width, height);
    }

    /* The patch buffer is filled in its own coordinates, so the pattern origin
       moves with the destination and the bricks stay put on the page. */
    function fillPatch(pattern, destinationX, destinationY) {
      try {
        pattern.setTransform(
          new DOMMatrix([1, 0, 0, 1, driftX - destinationX, driftY - destinationY]),
        );
      } catch {
        /* As above: a still field is still a field. */
      }
      paint.fillStyle = pattern;
      paint.fillRect(0, 0, radius * 2, radius * 2);
    }

    /* The pointer patch: the same two section mosaics, crossfaded inside the
       soft mask and composited over the grain. One small buffer means the cost
       stays with the patch, not with the viewport. */
    function patch(current, next, progress) {
      const strength = focus.e;
      if (strength < 0.012) return;
      const size = radius * 2;
      const destinationX = Math.round(focus.x / CELL) - radius;
      const destinationY = Math.round(focus.y / CELL) - radius;
      if (
        destinationX > width ||
        destinationY > height ||
        destinationX + size < 0 ||
        destinationY + size < 0
      )
        return;

      paint.globalCompositeOperation = "source-over";
      paint.globalAlpha = 1;
      paint.clearRect(0, 0, size, size);
      paint.globalAlpha = (1 - progress) * strength;
      fillPatch(current.mosaic, destinationX, destinationY);
      if (progress > 0.002) {
        paint.globalAlpha = progress * strength;
        fillPatch(next.mosaic, destinationX, destinationY);
      }
      paint.globalAlpha = 1;
      paint.globalCompositeOperation = "destination-in";
      paint.drawImage(mask, 0, 0);
      paint.globalCompositeOperation = "source-over";
      context.drawImage(scratch, destinationX, destinationY);
    }

    function draw() {
      if (!context || !canvas.isConnected) return;
      const [current, next, progress] = blend();
      driftX = Math.sin(time * 0.09) * 3;
      driftY = Math.cos(time * 0.07) * 2.4;
      context.clearRect(0, 0, width, height);
      context.globalAlpha = 1 - progress;
      fill(context, current.pattern);
      if (progress > 0.002) {
        context.globalAlpha = progress;
        fill(context, next.pattern);
      }
      context.globalAlpha = 1;
      patch(current, next, progress);
      frames++;
      canvas.__frames = frames;
    }

    /* Where the patch wants to be. A fine pointer leads it; a coarse one lets
       it wander on its own, slowly enough to read as drift rather than motion. */
    function advance(delta, now) {
      if (reduced.matches) {
        target.on = 0;
      } else if (fine.matches) {
        target.on = now - moved < 4200 ? 1 : 0;
      } else {
        target.on = 0.66;
        const seconds = (now - start) / 1000;
        target.x =
          window.innerWidth *
          (0.5 + Math.sin(seconds * 0.13) * 0.29 + Math.sin(seconds * 0.043) * 0.07);
        target.y = window.innerHeight * (0.42 + Math.cos(seconds * 0.1) * 0.24);
      }

      // Start on the pointer rather than sweeping in from the corner.
      if (target.on > 0 && focus.e < 0.01) {
        focus.x = target.x;
        focus.y = target.y;
      }
      // Exponential easing: calm, and the same at any frame rate.
      const follow = 1 - Math.exp(-delta / 190);
      const settle = 1 - Math.exp(-delta / 320);
      if (target.on > 0) {
        focus.x += (target.x - focus.x) * follow;
        focus.y += (target.y - focus.y) * follow;
      }
      focus.e += (target.on - focus.e) * settle;
      if (focus.e < 0.008) focus.e = 0;
    }

    function tick(now) {
      frame = 0;
      if (!running()) return;
      frame = requestAnimationFrame(tick);
      const delta = last ? Math.min(80, now - last) : 16.7;
      last = now;
      time = (now - start) / 1000;
      advance(delta, now);
      const began = performance.now();
      draw();
      cost += (performance.now() - began - cost) * 0.08;
      canvas.__cost = cost;
    }

    function sync() {
      cancelAnimationFrame(frame);
      frame = 0;
      last = 0;
      if (running()) {
        frame = requestAnimationFrame(tick);
        return;
      }
      // A still frame: no patch for reduced motion, one held patch otherwise.
      if (reduced.matches) focus.e = 0;
      draw();
    }

    function onPointer(event) {
      if (event.pointerType === "touch") return;
      moved = performance.now();
      target.x = event.clientX;
      target.y = event.clientY;
    }

    window.addEventListener("pointermove", onPointer, { passive: true });
    window.addEventListener("pointerdown", onPointer, { passive: true });
    document.addEventListener("mouseleave", () => {
      moved = -Infinity;
    });
    window.addEventListener("blur", () => {
      moved = -Infinity;
    });
    window.addEventListener("resize", resize, { passive: true });
    window.addEventListener("pageshow", sync);
    document.addEventListener("visibilitychange", sync);
    reduced.addEventListener("change", sync);
    fine.addEventListener?.("change", sync);
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
    // Slower than a snap and eased in and out, so it reads as the page
    // settling rather than as a wipe.
    const HOLD = 0;
    const SPAN = 1360;
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
    // Long enough for the slowest path (veil cap + span) and no longer.
    const watchdog = setTimeout(finish, 3000);

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
      const cut = REVEAL_EASE(progress) * 1.18;
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
