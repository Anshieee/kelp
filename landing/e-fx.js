/* e-fx.js — content pass.
   Owns: the examples player, the dithered video poster, the phone’s scroll
   spin, and the desktop companion’s WebGL orb. Everything here is additive;
   existing scripts keep their own hooks. */
(() => {
  "use strict";

  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  const clamp = (value, min = 0, max = 1) =>
    value < min ? min : value > max ? max : value;
  const idle =
    window.requestIdleCallback || ((work) => window.setTimeout(work, 60));

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

  /* ---------------------------------------------------------------------
     Phone: turn into place on entry, away on exit.
     The angle is driven by the frame’s own travel through the viewport, so
     the window is the same at every screen size and does not depend on how
     tall the section happens to be:
       p = 0    frame's top edge at the bottom of the viewport
       p = 0.5  frame centred
       p = 1    frame's bottom edge at the top of the viewport
     Entry finishes by p = 0.34 (top edge around the middle), and the exit
     turn starts at p = 0.58, while the frame is still half on screen, so both
     halves are actually reachable. ease-out cubic, one axis plus a small
     vertical tilt; the section clips the overhang.
     --------------------------------------------------------------------- */
  function initPhoneSpin() {
    const frames = [...document.querySelectorAll("[data-phone-spin]")];
    if (!frames.length) return;

    let queued = 0;
    const easeOut = (t) => 1 - Math.pow(1 - t, 3);
    const held = () =>
      reduce.matches || document.body.classList.contains("motion-paused");

    function reset() {
      frames.forEach((node) => {
        node.style.transform = "";
        node.style.willChange = "";
      });
    }

    function paint() {
      queued = 0;
      if (held()) return;
      const view = window.innerHeight || 1;
      frames.forEach((node) => {
        const rect = node.getBoundingClientRect();
        if (rect.bottom < -40 || rect.top > view + 40) return;

        const p = (view - rect.top) / Math.max(1, view + rect.height);
        const enter = easeOut(clamp(p / 0.34));
        const exit = easeOut(clamp((p - 0.58) / 0.32));
        const turn = -26 * (1 - enter) + 18 * exit;
        const tilt = -7 * (1 - enter) + 6 * exit;
        const scale = 0.955 + 0.045 * enter - 0.018 * exit;
        const settled = enter === 1 && exit === 0;

        node.style.transform = settled
          ? ""
          : `perspective(1300px) rotateY(${turn.toFixed(2)}deg) rotateX(${tilt.toFixed(
              2,
            )}deg) scale(${scale.toFixed(3)})`;
        node.style.willChange = settled ? "" : "transform";
      });
    }

    function request() {
      if (queued || document.hidden) return;
      queued = window.requestAnimationFrame(paint);
    }

    window.addEventListener("scroll", request, { passive: true });
    window.addEventListener("resize", request, { passive: true });
    document.addEventListener("visibilitychange", request);
    new MutationObserver(() => {
      if (held()) reset();
      else request();
    }).observe(document.body, { attributes: true, attributeFilter: ["class"] });
    reduce.addEventListener("change", () => {
      if (reduce.matches) reset();
      else request();
    });

    if (reduce.matches) reset();
    else request();
  }

  /* ---------------------------------------------------------------------
     Desktop companion: a WebGL orb.
     One sphere, two capsule eyes. The eyes follow the pointer, blink on their
     own cadence, and glance about when the pointer rests; the orb itself never
     moves. The body crossfades between three palettes on a two-second cadence
     (the old working / waiting / done flavours) so the surface keeps drifting
     without ever cutting. Ivory rim light keeps it seated on the page.
     When GL is unavailable, reduced motion is on, or the context is lost, the
     CSS face in the markup is what shows: eyes on a jade disc, a still frame.
     --------------------------------------------------------------------- */
  /* Each flavour keeps the cadence of the state it stands in for: working is
     busy, waiting settles and watches, done is unhurried. The colours and the
     timing crossfade together, so the mood changes without a cut. */
  const ORB_FLAVOURS = [
    {
      body: [0.286, 0.588, 0.439],
      deep: [0.075, 0.207, 0.157],
      glance: 2200,
      blink: 2800,
    },
    {
      body: [0.878, 0.553, 0.412],
      deep: [0.408, 0.212, 0.145],
      glance: 5600,
      blink: 5200,
    },
    {
      body: [0.463, 0.678, 0.776],
      deep: [0.129, 0.286, 0.376],
      glance: 3800,
      blink: 4200,
    },
  ];

  const ORB_VERTEX = `
    attribute vec2 aPos;
    void main() { gl_Position = vec4(aPos, 0.0, 1.0); }
  `;

  const ORB_FRAGMENT = `
    #ifdef GL_FRAGMENT_PRECISION_HIGH
    precision highp float;
    #else
    precision mediump float;
    #endif
    uniform vec2 uRes;
    uniform float uTime;
    uniform vec2 uGaze;
    uniform float uBlink;
    uniform vec3 uBody;
    uniform vec3 uDeep;

    float sdCapsule(vec2 p, vec2 a, vec2 b, float r) {
      vec2 pa = p - a;
      vec2 ba = b - a;
      float h = clamp(dot(pa, ba) / dot(ba, ba), 0.0, 1.0);
      return length(pa - ba * h) - r;
    }

    float bayer2(vec2 a) {
      a = floor(a);
      return fract(a.x / 2.0 + a.y * a.y * 0.75);
    }
    float bayer4(vec2 a) {
      return bayer2(0.5 * a) * 0.25 + bayer2(a);
    }

    void main() {
      vec2 p = (gl_FragCoord.xy - 0.5 * uRes) / min(uRes.x, uRes.y) * 2.0;
      p.y = -p.y;

      float aa = 2.0 / min(uRes.x, uRes.y);
      float R = 0.82;
      float d = length(p);
      float alpha = smoothstep(R + aa, R - aa, d);
      if (alpha <= 0.002) {
        gl_FragColor = vec4(0.0);
        return;
      }

      float z = sqrt(max(0.0, R * R - d * d)) / R;
      vec3 n = normalize(vec3(p / R, z));

      float key = clamp(dot(n, normalize(vec3(-0.55, 0.62, 0.7))), 0.0, 1.0);
      float fill = clamp(dot(n, normalize(vec3(0.5, -0.55, 0.4))), 0.0, 1.0);

      vec3 col = mix(uDeep, uBody, pow(key, 1.05));
      col += uBody * 0.14 * fill * z;

      // The underside settles into ink, so the orb carries weight on ivory.
      col *= 0.86 + 0.14 * smoothstep(-0.9, 0.55, n.y);

      float rim = pow(1.0 - z, 2.4);
      col = mix(col, vec3(0.984, 0.976, 0.961), rim * 0.42);

      float breath = 0.5 + 0.5 * sin(uTime * 0.9);
      col += pow(clamp(n.y, 0.0, 1.0), 3.0) * (0.03 + 0.04 * breath);

      vec2 g = clamp(uGaze, -1.0, 1.0) * 0.115;
      float open = mix(0.12, 1.0, uBlink);
      float eye = 10000.0;
      float glint = 0.0;
      for (int i = 0; i < 2; i++) {
        float s = i == 0 ? -1.0 : 1.0;
        vec2 c = vec2(s * 0.255, -0.045) + g;
        vec2 q = p - c;
        q.y /= open;
        eye = min(eye, sdCapsule(q, vec2(-0.058, 0.0), vec2(0.058, 0.0), 0.100));
        glint = max(glint, smoothstep(0.030, 0.0, length(p - (c + vec2(-0.036, 0.048)))));
      }

      float mask = smoothstep(0.011, -0.011, eye);
      col = mix(col, vec3(0.043, 0.055, 0.051), mask);
      col += glint * mask * uBlink * vec3(0.5, 0.48, 0.43);

      col += (bayer4(gl_FragCoord.xy) - 0.5) * 0.028;

      gl_FragColor = vec4(col, alpha);
    }
  `;

  function initOrb() {
    const host = document.querySelector("[data-orb]");
    const canvas = host && host.querySelector(".e-orb-canvas");
    if (!host || !canvas) return;

    let gl = null;
    let program = null;
    let uniforms = null;
    let buffer = null;
    let started = false;
    let lost = false;
    let last = 0;
    let raf = 0;
    let visible = false;

    const gaze = { x: 0, y: 0, tx: 0, ty: 0 };
    const blink = { value: 1, phase: 1, next: 0 };
    const flavour = { from: 0, to: 0, mix: 1, at: 0, index: 0 };
    const palette = {
      body: ORB_FLAVOURS[0].body.slice(),
      deep: ORB_FLAVOURS[0].deep.slice(),
    };
    const cadence = { glance: ORB_FLAVOURS[0].glance, blink: ORB_FLAVOURS[0].blink };

    let pointerAt = -1e9;
    let glanceAt = 0;
    const GLANCES = [
      [0.55, -0.35],
      [-0.5, 0.32],
      [0.16, 0.5],
      [-0.18, -0.12],
      [0, 0],
    ];
    let glance = 0;

    const pageHeld = () =>
      reduce.matches || document.body.classList.contains("motion-paused");

    function compile(type, source) {
      const shader = gl.createShader(type);
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
        gl.deleteShader(shader);
        return null;
      }
      return shader;
    }

    function build() {
      const vertex = compile(gl.VERTEX_SHADER, ORB_VERTEX);
      const fragment = compile(gl.FRAGMENT_SHADER, ORB_FRAGMENT);
      if (!vertex || !fragment) return false;

      const next = gl.createProgram();
      gl.attachShader(next, vertex);
      gl.attachShader(next, fragment);
      gl.linkProgram(next);
      gl.deleteShader(vertex);
      gl.deleteShader(fragment);
      if (!gl.getProgramParameter(next, gl.LINK_STATUS)) {
        gl.deleteProgram(next);
        return false;
      }

      program = next;
      gl.useProgram(program);

      buffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(
        gl.ARRAY_BUFFER,
        new Float32Array([-1, -1, 3, -1, -1, 3]),
        gl.STATIC_DRAW,
      );
      const slot = gl.getAttribLocation(program, "aPos");
      gl.enableVertexAttribArray(slot);
      gl.vertexAttribPointer(slot, 2, gl.FLOAT, false, 0, 0);

      uniforms = {
        res: gl.getUniformLocation(program, "uRes"),
        time: gl.getUniformLocation(program, "uTime"),
        gaze: gl.getUniformLocation(program, "uGaze"),
        blink: gl.getUniformLocation(program, "uBlink"),
        body: gl.getUniformLocation(program, "uBody"),
        deep: gl.getUniformLocation(program, "uDeep"),
      };

      gl.enable(gl.BLEND);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      gl.clearColor(0, 0, 0, 0);
      return true;
    }

    function resize() {
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      const rect = canvas.getBoundingClientRect();
      const width = Math.max(1, Math.round(rect.width * ratio));
      const height = Math.max(1, Math.round(rect.height * ratio));
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      gl.viewport(0, 0, canvas.width, canvas.height);
    }

    function mixPalette(now) {
      const STEP = 2000;
      if (!flavour.at) {
        // Open on the ink-jade body and hold it for a beat before the drift.
        flavour.at = now + 1000;
        flavour.from = 0;
        flavour.to = 1;
        flavour.mix = 0;
        flavour.index = 1;
      }
      if (now < flavour.at) return;
      const elapsed = now - flavour.at;
      if (elapsed >= STEP) {
        flavour.at = now;
        flavour.from = flavour.to;
        flavour.index = (flavour.index + 1) % ORB_FLAVOURS.length;
        flavour.to = flavour.index;
        flavour.mix = 0;
      }
      // Crossfade the first 60% of each step, then hold: no hard cut, and a
      // moment of rest before the next drift.
      flavour.mix = clamp((now - flavour.at) / (STEP * 0.6));
      const t = flavour.mix * flavour.mix * (3 - 2 * flavour.mix);
      const a = ORB_FLAVOURS[flavour.from];
      const b = ORB_FLAVOURS[flavour.to];
      for (let i = 0; i < 3; i++) {
        palette.body[i] = a.body[i] + (b.body[i] - a.body[i]) * t;
        palette.deep[i] = a.deep[i] + (b.deep[i] - a.deep[i]) * t;
      }
      cadence.glance = a.glance + (b.glance - a.glance) * t;
      cadence.blink = a.blink + (b.blink - a.blink) * t;
    }

    function stepEyes(now, delta) {
      const still = now - pointerAt > 2200;
      if (still && now > glanceAt) {
        glanceAt = now + cadence.glance * (0.8 + Math.random() * 0.6);
        glance = (glance + 1) % GLANCES.length;
        gaze.tx = GLANCES[glance][0];
        gaze.ty = GLANCES[glance][1];
      }
      const ease = Math.min(1, (delta / 16) * 0.16);
      gaze.x += (gaze.tx - gaze.x) * ease;
      gaze.y += (gaze.ty - gaze.y) * ease;

      if (blink.next === 0) blink.next = now + cadence.blink;
      if (blink.phase === 1 && now >= blink.next) {
        blink.phase = 0;
        blink.next = now + cadence.blink * (0.8 + Math.random() * 0.7);
      }
      if (blink.phase < 1) {
        blink.phase = Math.min(1, blink.phase + delta / 220);
        const half = blink.phase < 0.5 ? blink.phase * 2 : (1 - blink.phase) * 2;
        blink.value = 1 - 0.92 * (half * half * (3 - 2 * half));
      } else {
        blink.value = 1;
      }
    }

    function draw(now) {
      raf = 0;
      if (!gl || !program || lost || pageHeld() || !visible || document.hidden)
        return;
      // A 30fps budget: the body drifts slowly, so more frames buy nothing.
      if (last && now - last < 28) {
        raf = window.requestAnimationFrame(draw);
        return;
      }
      const delta = last ? Math.min(80, now - last) : 16;
      last = now;

      resize();
      mixPalette(now);
      stepEyes(now, delta);

      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.uniform2f(uniforms.res, canvas.width, canvas.height);
      gl.uniform1f(uniforms.time, now / 1000);
      gl.uniform2f(uniforms.gaze, gaze.x, gaze.y);
      gl.uniform1f(uniforms.blink, blink.value);
      gl.uniform3f(uniforms.body, palette.body[0], palette.body[1], palette.body[2]);
      gl.uniform3f(uniforms.deep, palette.deep[0], palette.deep[1], palette.deep[2]);
      gl.drawArrays(gl.TRIANGLES, 0, 3);

      if (!host.classList.contains("e-orb--webgl")) {
        host.classList.add("e-orb--webgl");
      }
      raf = window.requestAnimationFrame(draw);
    }

    function wake() {
      if (!raf && started && !lost) raf = window.requestAnimationFrame(draw);
    }

    function start() {
      if (started || reduce.matches) return;
      try {
        gl =
          canvas.getContext("webgl", {
            alpha: true,
            antialias: false,
            depth: false,
            stencil: false,
            premultipliedAlpha: false,
            powerPreference: "low-power",
          }) ||
          canvas.getContext("experimental-webgl", {
            alpha: true,
            antialias: false,
            depth: false,
            stencil: false,
          });
      } catch {
        gl = null;
      }
      if (!gl) return;
      if (!build()) {
        gl = null;
        return;
      }
      started = true;
      lost = false;
      canvas.addEventListener("webglcontextlost", onLost, false);
      canvas.addEventListener("webglcontextrestored", onRestored, false);
      // Give the canvas its layout box before the first draw, so frame one is
      // the real size rather than a one-pixel stretch.
      host.classList.add("e-orb--webgl");
      resize();
      wake();
    }

    function onLost(event) {
      // Let the browser try to hand the context back.
      event.preventDefault();
      lost = true;
      window.cancelAnimationFrame(raf);
      raf = 0;
      host.classList.remove("e-orb--webgl");
    }

    function onRestored() {
      if (!gl || build() === false) return;
      lost = false;
      wake();
    }

    // The eyes follow the pointer anywhere on the page, not only over the
    // orb: the companion is meant to notice you. Work is skipped while the
    // orb is off screen.
    window.addEventListener(
      "pointermove",
      (event) => {
        if (!visible) return;
        const rect = canvas.getBoundingClientRect();
        if (!rect.width || !rect.height) return;
        gaze.tx = clamp(((event.clientX - rect.left) / rect.width) * 2 - 1, -1, 1);
        gaze.ty = clamp(((event.clientY - rect.top) / rect.height) * 2 - 1, -1, 1);
        pointerAt = performance.now();
      },
      { passive: true },
    );

    // Touch and keyboard users never hover: a slow idle drift is the whole
    // performance, and the glance list keeps it alive.
    if ("IntersectionObserver" in window) {
      new IntersectionObserver(
        ([entry]) => {
          visible = entry.isIntersecting;
          if (visible) wake();
        },
        { rootMargin: "120px 0px" },
      ).observe(host);
    } else {
      visible = true;
    }

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) wake();
    });

    window.addEventListener("resize", () => {
      if (!started || lost) return;
      wake();
    });

    new MutationObserver(() => {
      if (pageHeld()) {
        window.cancelAnimationFrame(raf);
        raf = 0;
      } else wake();
    }).observe(document.body, { attributes: true, attributeFilter: ["class"] });

    reduce.addEventListener("change", () => {
      if (reduce.matches) {
        window.cancelAnimationFrame(raf);
        raf = 0;
        host.classList.remove("e-orb--webgl");
        return;
      }
      start();
      wake();
    });

    start();
  }

  /* ------------------------------------------------------------------- */

  initExamples();
  initPhoneSpin();
  initOrb();

  idle(paintAllDithers);
})();
