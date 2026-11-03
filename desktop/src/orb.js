// Orb port for the desk pet. Adapted from landing/e-fx.js `initOrb` on
// origin/landing-fx2 (WebGL disc + capsule eyes, pointer gaze, blink /
// glance cadence, CSS `e-orb-face` fallback). Behaviour deltas vs landing:
//  - flavour is DRIVEN by pet state (working/waiting/done/idle) instead of
//    the free ~2s drift; drift remains only inside `idle`.
//  - exposes `setPetState` + `petStateFromEvent` (pure, unit-tested) so the
//    SSE hookup never touches GL internals.
// Source of truth for the event→state table: desktop/pet_state.py
// (mirrored here; keep both in sync).

export const PET_STATES = ["working", "waiting", "done", "idle"];

// Subset of SSE event types the pet cares about. Anything unlisted keeps
// the current state (returns null).
const EVENT_TO_STATE = {
  turn_started: "working",
  turn_stopped: "idle",
  token_delta: "working",
  tool_call: "working",
  approval_created: "waiting",
  approval_pending: "waiting",
  approval_decided: "working",
  turn_finished: "done",
  turn_failed: "idle",
  stream_closed: "idle",
};

/** Map one SSE event to a pet state, or null to keep the current state. */
export function petStateFromEvent(event) {
  if (!event || typeof event.type !== "string") return null;
  return EVENT_TO_STATE[event.type] ?? null;
}

// Flavour i stands in for PET_STATES[i]: working is busy, waiting settles
// and watches, done is unhurried, idle drifts slowly. Colours match the
// landing orb; cadences match e-fx.js ORB_FLAVOURS.
export const ORB_FLAVOURS = [
  { state: "working", body: [0.286, 0.588, 0.439], deep: [0.075, 0.207, 0.157], glance: 2200, blink: 2800 },
  { state: "waiting", body: [0.878, 0.553, 0.412], deep: [0.408, 0.212, 0.145], glance: 5600, blink: 5200 },
  { state: "done", body: [0.463, 0.678, 0.776], deep: [0.129, 0.286, 0.376], glance: 3800, blink: 4200 },
  { state: "idle", body: [0.42, 0.5, 0.48], deep: [0.12, 0.18, 0.16], glance: 6000, blink: 5200 },
];

export const ORB_FRAGMENT = `
  precision mediump float;
  uniform vec2 uRes; uniform float uTime; uniform vec2 uGaze;
  uniform float uBlink; uniform vec3 uBody; uniform vec3 uDeep;
  float sdCapsule(vec2 p, vec2 a, vec2 b, float r) {
    vec2 pa = p - a; vec2 ba = b - a;
    float h = clamp(dot(pa, ba) / dot(ba, ba), 0.0, 1.0);
    return length(pa - ba * h) - r;
  }
  void main() {
    vec2 p = (gl_FragCoord.xy - 0.5 * uRes) / min(uRes.x, uRes.y) * 2.0;
    p.y = -p.y;
    float aa = 2.0 / min(uRes.x, uRes.y);
    float R = 0.82;
    float alpha = smoothstep(R + aa, R - aa, length(p));
    if (alpha <= 0.002) { gl_FragColor = vec4(0.0); return; }
    vec3 col = clamp(uBody * 1.2, 0.0, 1.0);
    vec2 g = clamp(uGaze, -1.0, 1.0) * 0.115;
    float open = mix(0.12, 1.0, uBlink);
    float eye = 10000.0;
    for (int i = 0; i < 2; i++) {
      float s = i == 0 ? -1.0 : 1.0;
      vec2 c = vec2(s * 0.255, -0.045) + g;
      vec2 q = p - c; q.y /= open;
      eye = min(eye, sdCapsule(q, vec2(-0.058, 0.0), vec2(0.058, 0.0), 0.100));
    }
    col = mix(col, vec3(0.043, 0.055, 0.051), smoothstep(0.011, -0.011, eye));
    gl_FragColor = vec4(col, alpha);
  }
`;

const clamp01 = (v) => Math.min(1, Math.max(0, v));

/**
 * Mount the orb into `host` ([data-orb] with .e-orb-canvas + .e-orb-face).
 * Returns { setPetState, dispose }. Reduced motion / no-GL → CSS face stays.
 */
export function initPetOrb(host, { muted = false } = {}) {
  const canvas = host?.querySelector(".e-orb-canvas");
  if (!host || !canvas) return null;
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");

  let target = 3; // idle index into ORB_FLAVOURS
  let current = { ...ORB_FLAVOURS[3] };
  let raf = 0;
  let gl = null;

  try {
    gl =
      canvas.getContext("webgl", { alpha: true, antialias: false, depth: false, stencil: false }) ||
      canvas.getContext("experimental-webgl");
  } catch {
    gl = null;
  }

  let program = null;
  let uniforms = null;
  if (gl && !reduce.matches && !muted) {
    const compile = (type, src) => {
      const s = gl.createShader(type);
      gl.shaderSource(s, src);
      gl.compileShader(s);
      return gl.getShaderParameter(s, gl.COMPILE_STATUS) ? s : null;
    };
    const vs = compile(gl.VERTEX_SHADER, "attribute vec2 aPos; void main(){gl_Position=vec4(aPos,0.,1.);}");
    const fs = compile(gl.FRAGMENT_SHADER, ORB_FRAGMENT);
    if (vs && fs) {
      program = gl.createProgram();
      gl.attachShader(program, vs);
      gl.attachShader(program, fs);
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) program = null;
      else {
        gl.useProgram(program);
        const buf = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, buf);
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
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
      }
    }
  }

  const gaze = { x: 0, y: 0 };
  let blinkPhase = 1;
  let blinkNext = performance.now() + 3000;
  let blinkValue = 1;

  function frame(now) {
    raf = 0;
    if (!gl || !program || reduce.matches) return;
    const want = ORB_FLAVOURS[target];
    for (const k of ["body", "deep"]) {
      for (let i = 0; i < 3; i++) current[k][i] += (want[k][i] - current[k][i]) * 0.08;
    }
    if (blinkPhase === 1 && now >= blinkNext) blinkPhase = 0;
    if (blinkPhase < 1) {
      blinkPhase = Math.min(1, blinkPhase + 1 / 14);
      blinkValue = 1 - 0.92 * blinkPhase * (2 - blinkPhase);
      if (blinkPhase === 1) blinkNext = now + want.blink * (0.8 + Math.random() * 0.7);
    } else blinkValue = 1;
    const rect = canvas.getBoundingClientRect();
    if (canvas.width !== Math.round(rect.width) || canvas.height !== Math.round(rect.height)) {
      canvas.width = Math.max(1, Math.round(rect.width));
      canvas.height = Math.max(1, Math.round(rect.height));
    }
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.uniform2f(uniforms.res, canvas.width, canvas.height);
    gl.uniform1f(uniforms.time, now / 1000);
    gl.uniform2f(uniforms.gaze, gaze.x, gaze.y);
    gl.uniform1f(uniforms.blink, blinkValue);
    gl.uniform3f(uniforms.body, current.body[0], current.body[1], current.body[2]);
    gl.uniform3f(uniforms.deep, current.deep[0], current.deep[1], current.deep[2]);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    host.classList.add("e-orb--webgl");
    raf = requestAnimationFrame(frame);
  }

  function wake() {
    if (!raf && gl && program && !reduce.matches) raf = requestAnimationFrame(frame);
  }

  const onPointer = (e) => {
    const r = canvas.getBoundingClientRect();
    if (!r.width) return;
    gaze.x = clamp01(((e.clientX - r.left) / r.width) * 2 - 1, -1, 1);
    gaze.y = clamp01(((e.clientY - r.top) / r.height) * 2 - 1, -1, 1);
  };
  window.addEventListener("pointermove", onPointer, { passive: true });

  function setPetState(state) {
    const i = PET_STATES.indexOf(state);
    if (i < 0) return false;
    target = i;
    host.dataset.petState = state;
    // CSS fallback face tints by state even with no GL.
    wake();
    return true;
  }

  host.dataset.petState = PET_STATES[target];
  wake();
  return {
    setPetState,
    dispose() {
      cancelAnimationFrame(raf);
      window.removeEventListener("pointermove", onPointer);
    },
  };
}
