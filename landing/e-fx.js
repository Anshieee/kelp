/* e-fx.js — content pass.
   Owns: the examples player, the dithered video poster, the desktop pet’s
   identity, and nothing else. Everything here is additive; existing scripts
   keep their own hooks. */
(() => {
  "use strict";

  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");

  /* ---------------------------------------------------------------------
     Dither artwork — the same ordered dither as the hero field in app.js,
     drawn at whatever size a canvas asks for. app.js keeps its own private
     copy for the WebGL field, so this stays independent of its internals.
     --------------------------------------------------------------------- */
  const BAYER = [0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5];
  const IVORY = [251, 249, 245];
  const CORAL = [246, 169, 122];
  const BUTTER = [247, 217, 116];
  const GREEN = [146, 210, 165];

  /* mode "mark" draws the hero bloom on a transparent field.
     mode "field" lets the same dither run edge to edge, so it can flood a
     16:9 frame instead of cropping the round mark. */
  function paintDither(canvas, mode) {
    const width = Number(canvas.getAttribute("width")) || 220;
    const height = Number(canvas.getAttribute("height")) || width;
    const context = canvas.getContext("2d", { alpha: mode === "mark" });
    if (!context) return;

    const image = context.createImageData(width, height);
    // The hero artwork is framed in an 800px square; keep that framing here.
    const scale = Math.min(width, height) / 800;
    const centreX = 415 * scale;
    const centreY = 390 * scale;
    const radiusX = 290 * scale;
    const radiusY = 325 * scale;
    const field = mode === "field";

    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        const index = (y * width + x) * 4;
        const nx = field ? (x / width) * 2 - 1 : (x - centreX) / radiusX;
        const ny = field ? (y / height) * 2 - 1 : (y - centreY) / radiusY;
        const angle = Math.atan2(ny, nx);
        const warp =
          0.07 * Math.sin(angle * 5 + 1.2) + 0.055 * Math.cos(angle * 3);
        const radius = Math.sqrt(nx * nx + ny * ny);
        const boundary = 1 + warp;

        if (!field && radius > boundary) continue;

        const u =
          nx + 0.11 * Math.sin(ny * 5) + 0.04 * Math.sin(ny * 13 + nx * 4);
        const v = ny + 0.1 * Math.sin(nx * 6 + ny * 3);
        const cells =
          Math.sin(u * 6 + Math.sin(v * 4) * 1.7) *
          Math.cos(v * 5 + Math.sin(u * 3));
        const marble = Math.sin((u * 1.2 + v * 0.6 + cells * 0.5) * 19);
        const ridges = Math.pow(Math.abs(marble), 9);
        const blend = Math.max(0, Math.min(1, (ny + 1) / 2 + cells * 0.17));
        const from = blend < 0.5 ? CORAL : BUTTER;
        const to = blend < 0.5 ? BUTTER : GREEN;
        const mix = blend < 0.5 ? blend * 2 : (blend - 0.5) * 2;
        const edge = field ? 1 : Math.min(1, (boundary - radius) * 23);
        const hole = field
          ? 0
          : Math.exp(-((nx + 0.2) ** 2 / 0.05 + (ny - 0.02) ** 2 / 0.09));
        const density = Math.max(0, (0.83 - ridges * 0.43 - hole * 0.8) * edge);
        const threshold = (BAYER[(y % 4) * 4 + (x % 4)] + 0.5) / 16;
        const noise = (Math.sin(x * 127.1 + y * 311.7) * 43758.5453) % 1;

        for (let channel = 0; channel < 3; channel++) {
          const colour = from[channel] + (to[channel] - from[channel]) * mix;
          image.data[index + channel] =
            threshold < density ? colour + noise * 6 : IVORY[channel];
        }
        image.data[index + 3] = 255;
      }
    }

    context.putImageData(image, 0, 0);
  }

  function paintAllDithers() {
    document.querySelectorAll("[data-dither]").forEach((canvas) => {
      paintDither(
        canvas,
        canvas.dataset.dither === "poster" ? "field" : "mark",
      );
    });
  }

  /* ---------------------------------------------------------------------
     Examples player.
     c-copy.js binds the first copy of this carousel and starts its own timer.
     Clone the stage so those listeners drop with the detached original, then
     run a single, complete cycle from here: scroll-triggered, dot-driven and
     genuinely pausable.
     --------------------------------------------------------------------- */
  function initExamples() {
    const stage = document.querySelector(".c-example-stage");
    if (!stage) return;

    const player = stage.cloneNode(true);
    player.classList.add("e-player");
    stage.replaceWith(player);

    const slides = [
      ...player.querySelectorAll('[aria-roledescription="slide"]'),
    ];
    const dots = [...player.querySelectorAll(".c-example-dots button")];
    const pause = player.querySelector(".c-example-pause");
    const copy = player.querySelector(".c-example-copy");
    const controls = player.querySelector(".c-example-controls");
    if (!slides.length || !dots.length || !pause || !copy || !controls) return;

    const EVERY = 6500;
    let index = 0;
    let timer = 0;
    let playing = !reduce.matches;
    let inView = false;

    const pagePaused = () => document.body.classList.contains("motion-paused");

    function show(next) {
      index = (next + slides.length) % slides.length;
      slides.forEach((slide, i) => {
        slide.hidden = i !== index;
      });
      dots.forEach((dot, i) =>
        dot.setAttribute("aria-pressed", String(i === index)),
      );
    }

    function update() {
      window.clearInterval(timer);
      timer = 0;

      const held = reduce.matches || pagePaused();
      const running = playing && !held && inView && !document.hidden;

      pause.disabled = held;
      pause.textContent = reduce.matches
        ? "Paused for reduced motion"
        : pagePaused()
          ? "Examples paused"
          : playing
            ? "Pause examples"
            : "Play examples";
      pause.setAttribute("aria-pressed", String(!playing || held));
      copy.setAttribute("aria-live", running ? "off" : "polite");

      if (running) {
        timer = window.setInterval(() => show(index + 1), EVERY);
      }
    }

    dots.forEach((dot, i) =>
      dot.addEventListener("click", () => {
        show(i);
        update();
      }),
    );

    pause.addEventListener("click", () => {
      playing = !playing;
      update();
    });

    document.addEventListener("visibilitychange", update);
    new MutationObserver(update).observe(document.body, {
      attributes: true,
      attributeFilter: ["class"],
    });

    if ("IntersectionObserver" in window) {
      new IntersectionObserver(
        ([entry]) => {
          inView = entry.isIntersecting;
          update();
        },
        { threshold: 0.35 },
      ).observe(player);
    } else {
      inView = true;
    }

    reduce.addEventListener("change", () => {
      playing = !reduce.matches;
      if (reduce.matches) show(0);
      update();
    });

    controls.hidden = false;
    show(0);
    update();
  }

  /* ------------------------------------------------------------------- */

  initExamples();

  const idle =
    window.requestIdleCallback || ((work) => window.setTimeout(work, 60));
  idle(paintAllDithers);
})();
