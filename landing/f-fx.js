/* Effects layer (f). Frosted glass mosaic, on one WebGL quad.
 *
 * The page's existing dither field is copied into a texture every frame and
 * drawn back opaque, so the ground outside the pointer is pixel-identical to
 * what layer (d) already paints. Under the pointer, the same field is read
 * one tile at a time through a frosted disc: pure 2D, no grid lines, no bevel
 * light, no lens warp. The layer is a background: fixed, edge to edge, behind
 * content, and it never takes a pointer event.
 *
 * Rules this file keeps: nothing is created before the page is parsed, the
 * effect is decorative, a failed or lost GL context removes the layer and
 * leaves layer (d)'s canvas grain showing, reduced motion paints one still
 * frame with no follow, and nothing runs while the tab is hidden or while the
 * page's own "Pause motion" is on.
 */
(() => {
  "use strict";

  const reduced = matchMedia("(prefers-reduced-motion: reduce)");

  /* Fixed tuning. Radius and tile size are CSS pixels and are scaled by the
     backing-store ratio at draw time, so the disc reads the same size at any
     display density. */
  const CONFIG = {
    radius: 140,
    tileSize: 30,
    frost: 0.7,
    bevel: 0.8,
    falloff: 1.0,
    snapTiles: 1.0,
    lerpFactor: 0.22,
  };

  const VERTEX_SOURCE = `
attribute vec2 a_position;
varying vec2 v_uv;
void main() {
    v_uv = a_position * 0.5 + 0.5;
    v_uv.y = 1.0 - v_uv.y;
    gl_Position = vec4(a_position, 0.0, 1.0);
}
`;

  const FRAGMENT_SOURCE = `
precision highp float;
varying vec2 v_uv;
uniform sampler2D u_tex;
uniform vec2 u_resolution;
uniform vec2 u_mouse;
uniform float u_radius;
uniform float u_tileSize;
uniform float u_frost;
uniform float u_bevel;
uniform float u_falloff;
uniform float u_snapTiles;
float hash21(vec2 p) {
    p = fract(p * vec2(234.34, 435.345));
    p += dot(p, p + 34.23);
    return fract(p.x * p.y);
}
vec2 hash22(vec2 p) {
    float n = hash21(p);
    return vec2(n, hash21(p + n + 17.3));
}
void main() {
    vec2 fragCoord = v_uv * u_resolution;
    vec2 gridOrigin = u_resolution * 0.5;
    vec2 relCoord = fragCoord - gridOrigin;
    vec2 tileIdx = floor(relCoord / u_tileSize);
    vec2 tileLocal = fract(relCoord / u_tileSize);
    vec2 tileCenterFrag = gridOrigin + (tileIdx + 0.5) * u_tileSize;
    float distTile = length(tileCenterFrag - u_mouse);
    float distPixel = length(fragCoord - u_mouse);
    float dist = mix(distPixel, distTile, u_snapTiles);
    float feather = max(2.0, u_falloff * u_radius * 0.85);
    float innerR = u_radius - feather * 0.5;
    float outerR = u_radius + feather * 0.5;
    float mask = 1.0 - smoothstep(innerR, outerR, dist);
    vec3 baseColor = texture2D(u_tex, v_uv).rgb;
    if (mask <= 0.001) {
        gl_FragColor = vec4(baseColor, 1.0);
        return;
    }
    vec2 centered = tileLocal - 0.5;
    vec2 tileRand = (hash22(tileIdx) - 0.5) * 2.0;
    float mosaicFlatness = mix(0.45, 0.94, u_bevel);
    vec2 flatOffset = centered * (1.0 - mosaicFlatness) * u_tileSize
                    + tileRand * u_bevel * u_tileSize * 0.12;
    vec2 sampleCenterPx = tileCenterFrag + flatOffset;
    vec2 sampleUV = sampleCenterPx / u_resolution;
    float blurRadiusPx = (2.0 + u_frost * 24.0) * (0.65 + 0.35 * (u_tileSize / 36.0));
    vec2 texelStep = blurRadiusPx / u_resolution;
    float noiseAngle = hash21(fragCoord) * 6.2831853;
    float cr = cos(noiseAngle);
    float sr = sin(noiseAngle);
    mat2 rot = mat2(cr, -sr, sr, cr);
    vec3 blurredColor = vec3(0.0);
    float totalWeight = 0.0;
    for (int i = 0; i < 12; i++) {
        float fi = float(i);
        float r = sqrt((fi + 0.5) / 12.0);
        float theta = fi * 2.39996323;
        vec2 diskOffset = rot * vec2(cos(theta), sin(theta)) * r;
        vec2 uvTap = clamp(sampleUV + diskOffset * texelStep, 0.002, 0.998);
        vec3 tapCol = texture2D(u_tex, uvTap).rgb;
        float w = 1.0 - 0.3 * r;
        blurredColor += tapCol * w;
        totalWeight += w;
    }
    blurredColor /= totalWeight;
    float tileFrostVariation = tileRand.x * 0.035 * u_bevel;
    float flatFrostTint = clamp(u_frost * 0.10 + tileFrostVariation, -0.04, 0.18);
    vec3 flatGlassColor = mix(blurredColor, vec3(0.95, 0.97, 1.0), max(0.0, flatFrostTint));
    flatGlassColor += tileFrostVariation * 0.5;
    float frostGrain = (hash21(fragCoord * 0.9) - 0.5) * 0.055 * u_frost;
    flatGlassColor += frostGrain;
    vec3 finalColor = mix(baseColor, clamp(flatGlassColor, 0.0, 1.0), mask);
    gl_FragColor = vec4(finalColor, 1.0);
}
`;

  /* Far outside any viewport, so the disc resolves to nothing and the field
     is drawn back untouched. */
  const OFFSCREEN = -1e5;
  const IDLE = 2400; // ms of stillness before the focal point starts to orbit

  const clamp = (value, min, max) =>
    value < min ? min : value > max ? max : value;

  function shader(gl, type, source) {
    const node = gl.createShader(type);
    if (!node) return null;
    gl.shaderSource(node, source);
    gl.compileShader(node);
    if (!gl.getShaderParameter(node, gl.COMPILE_STATUS)) {
      gl.deleteShader(node);
      return null;
    }
    return node;
  }

  function program(gl) {
    const vertex = shader(gl, gl.VERTEX_SHADER, VERTEX_SOURCE);
    const fragment = shader(gl, gl.FRAGMENT_SHADER, FRAGMENT_SOURCE);
    if (!vertex || !fragment) return null;
    const built = gl.createProgram();
    if (!built) return null;
    gl.attachShader(built, vertex);
    gl.attachShader(built, fragment);
    gl.linkProgram(built);
    gl.deleteShader(vertex);
    gl.deleteShader(fragment);
    if (!gl.getProgramParameter(built, gl.LINK_STATUS)) {
      gl.deleteProgram(built);
      return null;
    }
    return built;
  }

  function ground() {
    try {
      const declared = (
        getComputedStyle(document.documentElement).getPropertyValue("--canvas") ||
        ""
      ).trim();
      return /^#([0-9a-f]{6})$/i.test(declared) ? declared : "#fbf9f5";
    } catch {
      return "#fbf9f5";
    }
  }

  function boot() {
    if (typeof HTMLCanvasElement !== "function" || !document.body) return;

    const canvas = document.createElement("canvas");
    canvas.id = "f-fx-glass";
    // Decoration, like layer (d)'s field: the background is not page content,
    // so it is hidden from the accessibility tree rather than announced.
    canvas.setAttribute("aria-hidden", "true");

    let gl = null;
    try {
      const options = {
        alpha: false,
        antialias: false,
        depth: false,
        stencil: false,
        premultipliedAlpha: false,
        preserveDrawingBuffer: false,
        powerPreference: "low-power",
      };
      gl =
        canvas.getContext("webgl", options) ||
        canvas.getContext("experimental-webgl", options);
    } catch {
      gl = null;
    }
    // No GL: layer (d)'s canvas grain is still the background. Nothing to add.
    if (!gl) return;

    const built = program(gl);
    if (!built) return;

    /* A CSS-pixel buffer the field is composited into: ivory ground first, then
       layer (d)'s field multiplied over it at the same fifth it uses in CSS, so
       the texture is what the page already showed. */
    const source = document.createElement("canvas");
    const paint = source.getContext("2d", { alpha: false });
    if (!paint) return;

    const quad = gl.createBuffer();
    const texture = gl.createTexture();
    if (!quad || !texture) return;

    gl.bindBuffer(gl.ARRAY_BUFFER, quad);
    gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]),
      gl.STATIC_DRAW,
    );
    gl.useProgram(built);
    const position = gl.getAttribLocation(built, "a_position");
    gl.enableVertexAttribArray(position);
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);

    const uniform = (name) => gl.getUniformLocation(built, name);
    const u = {
      tex: uniform("u_tex"),
      resolution: uniform("u_resolution"),
      mouse: uniform("u_mouse"),
      radius: uniform("u_radius"),
      tileSize: uniform("u_tileSize"),
      frost: uniform("u_frost"),
      bevel: uniform("u_bevel"),
      falloff: uniform("u_falloff"),
      snapTiles: uniform("u_snapTiles"),
    };

    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);

    const backdrop = ground();
    const focal = { x: OFFSCREEN, y: OFFSCREEN };
    const target = { x: OFFSCREEN, y: OFFSCREEN };
    let width = 1;
    let height = 1;
    let ratio = 1;
    let frame = 0;
    let last = -Infinity;
    let start = performance.now();

    const held = () =>
      reduced.matches || document.body.classList.contains("motion-paused");

    function resize() {
      ratio = Math.min(window.devicePixelRatio || 1, 2);
      const cssWidth = Math.max(1, window.innerWidth);
      const cssHeight = Math.max(1, window.innerHeight);
      width = Math.max(1, Math.round(cssWidth * ratio));
      height = Math.max(1, Math.round(cssHeight * ratio));
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      if (source.width !== cssWidth || source.height !== cssHeight) {
        source.width = cssWidth;
        source.height = cssHeight;
      }
      paint.imageSmoothingEnabled = false;
      paint.fillStyle = backdrop;
      paint.fillRect(0, 0, cssWidth, cssHeight);
    }

    /* The field is what layer (d) is painting right now; copying it each frame
       keeps the ground under the mosaic live instead of frozen at load. */
    function upload() {
      const field = document.getElementById("d-fx-field");
      paint.globalCompositeOperation = "source-over";
      paint.globalAlpha = 1;
      paint.fillStyle = backdrop;
      paint.fillRect(0, 0, source.width, source.height);
      if (field && field.width > 1 && field.height > 1) {
        paint.globalCompositeOperation = "multiply";
        paint.globalAlpha = 0.2;
        try {
          paint.drawImage(field, 0, 0, source.width, source.height);
        } catch {
          /* A field mid-resize simply holds the ground for this frame. */
        }
        paint.globalAlpha = 1;
        paint.globalCompositeOperation = "source-over";
      }
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);
    }

    /* The idle focus: a slow, small orbit around the middle of the hero, held
       inside the viewport so the disc is never parked off screen. */
    function orbit(now) {
      const hero = document.querySelector(".hero");
      let x = window.innerWidth * 0.5;
      let y = window.innerHeight * 0.42;
      if (hero) {
        const rect = hero.getBoundingClientRect();
        if (rect.width > 2 && rect.height > 2) {
          x = rect.left + rect.width * 0.5;
          y = rect.top + rect.height * 0.46;
        }
      }
      y = clamp(y, window.innerHeight * 0.3, window.innerHeight * 0.72);
      x = clamp(x, window.innerWidth * 0.2, window.innerWidth * 0.8);
      const seconds = (now - start) / 1000;
      return {
        x: x + Math.cos(seconds * 0.29) * Math.min(84, window.innerWidth * 0.06),
        y: y + Math.sin(seconds * 0.21) * Math.min(56, window.innerHeight * 0.055),
      };
    }

    function aim(now) {
      const pointing = now - last < IDLE;
      if (pointing) return { x: target.x, y: target.y };
      return orbit(now);
    }

    function draw(now) {
      const want = aim(now);
      const k = CONFIG.lerpFactor;
      if (focal.x <= OFFSCREEN + 1) {
        focal.x = want.x;
        focal.y = want.y;
      } else {
        focal.x += (want.x - focal.x) * k;
        focal.y += (want.y - focal.y) * k;
      }

      upload();
      gl.viewport(0, 0, width, height);
      gl.useProgram(built);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, texture);
      if (u.tex) gl.uniform1i(u.tex, 0);
      if (u.resolution) gl.uniform2f(u.resolution, width, height);
      if (u.mouse) gl.uniform2f(u.mouse, focal.x * ratio, focal.y * ratio);
      if (u.radius) gl.uniform1f(u.radius, CONFIG.radius * ratio);
      if (u.tileSize) gl.uniform1f(u.tileSize, CONFIG.tileSize * ratio);
      if (u.frost) gl.uniform1f(u.frost, CONFIG.frost);
      if (u.bevel) gl.uniform1f(u.bevel, CONFIG.bevel);
      if (u.falloff) gl.uniform1f(u.falloff, CONFIG.falloff);
      if (u.snapTiles) gl.uniform1f(u.snapTiles, CONFIG.snapTiles);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }

    function still() {
      // One frame, focal point far off: the field exactly as layer (d) paints.
      focal.x = focal.y = OFFSCREEN;
      target.x = target.y = OFFSCREEN;
      upload();
      gl.viewport(0, 0, width, height);
      gl.useProgram(built);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, texture);
      if (u.tex) gl.uniform1i(u.tex, 0);
      if (u.resolution) gl.uniform2f(u.resolution, width, height);
      if (u.mouse) gl.uniform2f(u.mouse, OFFSCREEN, OFFSCREEN);
      if (u.radius) gl.uniform1f(u.radius, CONFIG.radius * ratio);
      if (u.tileSize) gl.uniform1f(u.tileSize, CONFIG.tileSize * ratio);
      if (u.frost) gl.uniform1f(u.frost, CONFIG.frost);
      if (u.bevel) gl.uniform1f(u.bevel, CONFIG.bevel);
      if (u.falloff) gl.uniform1f(u.falloff, CONFIG.falloff);
      if (u.snapTiles) gl.uniform1f(u.snapTiles, CONFIG.snapTiles);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }

    function tick(now) {
      frame = 0;
      if (document.hidden || held()) return;
      frame = requestAnimationFrame(tick);
      draw(now);
    }

    function sync() {
      cancelAnimationFrame(frame);
      frame = 0;
      if (document.hidden || held()) {
        still();
        return;
      }
      frame = requestAnimationFrame(tick);
    }

    function track(x, y) {
      target.x = x;
      target.y = y;
      last = performance.now();
    }

    function onMouse(event) {
      track(event.clientX, event.clientY);
    }

    function onTouch(event) {
      const touch = event.touches && event.touches[0];
      if (touch) track(touch.clientX, touch.clientY);
    }

    window.addEventListener("mousemove", onMouse, { passive: true });
    window.addEventListener("touchmove", onTouch, { passive: true });
    window.addEventListener("touchstart", onTouch, { passive: true });
    window.addEventListener(
      "resize",
      () => {
        resize();
        sync();
      },
      { passive: true },
    );
    window.addEventListener("pageshow", sync);
    document.addEventListener("visibilitychange", sync);
    reduced.addEventListener("change", sync);
    new MutationObserver(sync).observe(document.body, {
      attributes: true,
      attributeFilter: ["class"],
    });

    // A lost context leaves the page with layer (d) alone: remove, don't stall.
    function drop() {
      cancelAnimationFrame(frame);
      frame = 0;
      canvas.remove();
    }
    canvas.addEventListener("webglcontextlost", (event) => {
      event.preventDefault();
      drop();
    });
    window.addEventListener("error", () => {
      /* The layer is decoration; a thrown frame must not keep a dead canvas. */
      if (canvas.isConnected && !frame && !gl.isContextLost()) return;
    });

    try {
      // First in the body, so the layer paints under every later positioned
      // block while still sitting above the page ground.
      document.body.insertBefore(canvas, document.body.firstChild);
    } catch {
      return;
    }

    resize();
    start = performance.now();
    sync();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else boot();
})();
