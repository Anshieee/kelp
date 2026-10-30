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
     One owner: this block. c-copy.js used to bind the same carousel and start
     a second timer; it is now inert, so the stage is driven from here only.

     Motion: slides crossfade with a slight rise, and the four dots share a
     single indicator that slides between their centres, stretching mid-flight
     so each dot reads as morphing into the next. Everything animated is
     transform/opacity. Reduced motion keeps the selected state and drops the
     travel.
     --------------------------------------------------------------------- */
  function initExamples() {
    const player = document.querySelector(".c-example-stage");
    if (!player) return;

    const slides = [
      ...player.querySelectorAll('[aria-roledescription="slide"]'),
    ];
    const dotsBox = player.querySelector(".c-example-dots");
    const dots = dotsBox ? [...dotsBox.querySelectorAll("button")] : [];
    const pause = player.querySelector(".c-example-pause");
    const copy = player.querySelector(".c-example-copy");
    const controls = player.querySelector(".c-example-controls");
    if (!slides.length || !dots.length || !pause || !copy || !controls) return;
    if (!dotsBox) return;

    player.classList.add("e-player");

    /* The travelling indicator. Purely decorative: the pressed dot carries
       the state for assistive tech. */
    const pill = document.createElement("span");
    pill.className = "c-example-pill";
    pill.setAttribute("aria-hidden", "true");
    dotsBox.prepend(pill);

    const EVERY = 4000;
    const TRAVEL_MIN = 360;
    const TRAVEL_MAX = 640;
    let index = 0;
    let timer = 0;
    let release = 0;
    let playing = !reduce.matches;
    let inView = false;
    let hovering = false;
    let pointerFocus = false;

    const pagePaused = () => document.body.classList.contains("motion-paused");
    const held = () => reduce.matches || pagePaused();

    function centreOf(button) {
      return button.offsetLeft + button.offsetWidth / 2;
    }

    function placePill(animate, distance) {
      const button = dots[index];
      if (!button) return;
      const travel = animate && !reduce.matches;
      if (travel) {
        pill.style.setProperty(
          "--pill-dur",
          Math.min(TRAVEL_MAX, TRAVEL_MIN + Math.abs(distance) * 1.7) + "ms",
        );
      } else {
        pill.style.transition = "none";
      }
      dotsBox.style.setProperty("--pill-x", centreOf(button) + "px");
      if (!travel) {
        void pill.offsetWidth;
        pill.style.transition = "";
      }
    }

    /* Dots lean towards the indicator as it lands, then ease back. */
    function pullDots(pillX) {
      dots.forEach((dot, i) => {
        if (i === index) {
          dot.style.setProperty("--pull", "0px");
          return;
        }
        const pull = (pillX - centreOf(dot)) * 0.16;
        dot.style.setProperty(
          "--pull",
          Math.max(-4, Math.min(4, pull)).toFixed(2) + "px",
        );
      });
      window.clearTimeout(release);
      release = window.setTimeout(() => {
        dots.forEach((dot) => dot.style.setProperty("--pull", "0px"));
      }, 420);
    }

    function show(next, options) {
      const count = slides.length;
      const previous = index;
      const target = ((next % count) + count) % count;
      const forward =
        options && options.forward !== undefined
          ? options.forward
          : target > previous;
      const animate = !(options && options.animate === false);

      index = target;

      const enter = forward ? 14 : -14;
      const exit = forward ? -12 : 12;
      slides.forEach((slide, i) => {
        const active = i === index;
        slide.hidden = false;
        slide.style.setProperty("--slide-in", (active ? enter : exit) + "px");
        slide.classList.toggle("is-active", active);
      });

      dots.forEach((dot, i) =>
        dot.setAttribute("aria-pressed", String(i === index)),
      );

      const from = previous === index ? null : centreOf(dots[previous]);
      const to = centreOf(dots[index]);
      placePill(animate, from === null ? 0 : to - from);

      if (animate && !reduce.matches) {
        pill.classList.remove("is-travelling");
        void pill.offsetWidth;
        pill.classList.add("is-travelling");
      }

      if (animate) pullDots(to);
      else dots.forEach((dot) => dot.style.setProperty("--pull", "0px"));
    }

    function update() {
      window.clearInterval(timer);
      timer = 0;

      const motionHeld = held();
      /* Keyboard focus holds the rotation, as WCAG 2.2.2 expects. A pointer
         tap on a dot also focuses it, but should not leave the player stuck
         on touch, so pointer-driven focus does not count. */
      const focusHolds =
        player.contains(document.activeElement) && !pointerFocus;
      const running =
        playing &&
        !motionHeld &&
        inView &&
        !hovering &&
        !focusHolds &&
        !document.hidden;

      pause.disabled = motionHeld;
      pause.textContent = reduce.matches
        ? "Paused for reduced motion"
        : pagePaused()
          ? "Examples paused"
          : playing
            ? "Pause examples"
            : "Play examples";
      pause.setAttribute("aria-pressed", String(!playing || motionHeld));
      copy.setAttribute("aria-live", running ? "off" : "polite");

      if (running) {
        timer = window.setInterval(
          () => show(index + 1, { forward: true }),
          EVERY,
        );
      }
    }

    function choose(next) {
      const target = ((next % dots.length) + dots.length) % dots.length;
      if (target !== index) show(target, { forward: target > index });
      update();
    }

    dots.forEach((dot, i) =>
      dot.addEventListener("click", () => {
        choose(i);
      }),
    );

    /* Arrow keys walk the dots; Tab already reaches them. */
    dotsBox.addEventListener("keydown", (event) => {
      const key = event.key;
      const current = dots.indexOf(event.target.closest("button"));
      if (current === -1) return;
      let next = null;
      if (key === "ArrowRight" || key === "ArrowDown")
        next = (current + 1) % dots.length;
      else if (key === "ArrowLeft" || key === "ArrowUp")
        next = (current - 1 + dots.length) % dots.length;
      else if (key === "Home") next = 0;
      else if (key === "End") next = dots.length - 1;
      if (next === null) return;
      event.preventDefault();
      choose(next);
      dots[next].focus();
    });

    pause.addEventListener("click", () => {
      playing = !playing;
      update();
    });

    player.addEventListener("pointerenter", (event) => {
      if (event.pointerType !== "mouse") return;
      hovering = true;
      update();
    });
    player.addEventListener("pointerleave", (event) => {
      if (event.pointerType !== "mouse") return;
      hovering = false;
      update();
    });
    player.addEventListener("focusin", update);
    player.addEventListener("focusout", () => {
      queueMicrotask(update);
    });
    player.addEventListener("pointerdown", () => {
      pointerFocus = true;
    });
    document.addEventListener(
      "keydown",
      () => {
        pointerFocus = false;
      },
      true,
    );

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

    /* Re-measure when the layout settles, never animated. */
    const settle = () => placePill(false, 0);
    window.addEventListener("resize", settle);
    if ("ResizeObserver" in window) {
      new ResizeObserver(settle).observe(dotsBox);
    }
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(settle);
    }

    reduce.addEventListener("change", () => {
      playing = !reduce.matches;
      placePill(false, 0);
      update();
    });

    controls.hidden = false;
    show(0, { animate: false });
    update();
    settle();
  }

  /* ------------------------------------------------------------------- */

  initExamples();

  const idle =
    window.requestIdleCallback || ((work) => window.setTimeout(work, 60));
  idle(paintAllDithers);
})();
