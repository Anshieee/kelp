/* g-phone.js — scroll-linked 3D phone.
   Owns: a procedural Three.js handset that replaces .phone-shell and rotates
   with scroll. Figure-based journey: 0deg when the figure first appears,
   360deg (front again) at viewport center, 720deg (front again) on exit.
   Direct 1:1 scrub, no easing. CSS .phone-shell stays as the fallback when
   WebGL is missing or motion is held. */
import * as THREE from "three";

(() => {
  "use strict";

  const TURNS = 2;
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  const held = () =>
    reduce.matches || document.body.classList.contains("motion-paused");

  function boot() {
    const figure = document.querySelector("figure.e-phone");
    const shell = figure?.querySelector(".phone-shell");
    if (!figure || !shell || typeof WebGLRenderingContext === "undefined") return;
    if (held()) return;

    let renderer;
    try {
      renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });
    } catch {
      return; // fallback: CSS shell stays
    }
    if (!renderer) return;

    // Match the CSS shell box exactly, then hide the DOM shell.
    const box = shell.getBoundingClientRect();
    let w = Math.max(1, Math.round(box.width || 268));
    let h = Math.max(1, Math.round(box.height || 550));
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setSize(w, h, false);
    renderer.domElement.className = "g-phone-canvas";
    renderer.domElement.setAttribute("aria-hidden", "true");
    shell.style.display = "none";
    figure.insertBefore(renderer.domElement, shell);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(30, w / h, 0.1, 50);
    camera.position.set(0, 0.1, 7.2);
    camera.lookAt(0, 0, 0);

    scene.add(new THREE.AmbientLight(0xffffff, 0.9));
    const key = new THREE.DirectionalLight(0xffffff, 1.1);
    key.position.set(3, 4, 5);
    scene.add(key);
    const rim = new THREE.DirectionalLight(0xd6e8d8, 0.5);
    rim.position.set(-4, 1, -3);
    scene.add(rim);

    const phone = new THREE.Group();
    // Scaled to fit: the swept circle at 90/270deg must stay inside the
    // canvas, so the handset never clips mid-turn.
    phone.scale.setScalar(0.72);
    scene.add(phone);

    // Body: 9:18.5 handset, extruded rounded rect so the corners read like
    // the CSS shell's 54px radius even though the slab is thin.
    const BW = 1.62, BH = 3.32, BD = 0.14;
    const bodyMat = new THREE.MeshStandardMaterial({
      color: 0x17171a, metalness: 0.55, roughness: 0.38,
    });
    const bodyShape = roundedRectShape(BW, BH, 0.19);
    const bodyGeo = new THREE.ExtrudeGeometry(bodyShape, {
      depth: BD - 0.06, bevelEnabled: true, bevelThickness: 0.03,
      bevelSize: 0.03, bevelSegments: 2, curveSegments: 24,
    });
    bodyGeo.center();
    const body = new THREE.Mesh(bodyGeo, bodyMat);
    phone.add(body);

    // Screen: canvas texture with a mini Kelp chat, sits just proud of the face.
    const screenTex = makeScreenTexture(renderer);
    const screen = new THREE.Mesh(
      new THREE.PlaneGeometry(BW * 0.88, BH * 0.92),
      new THREE.MeshBasicMaterial({ map: screenTex.texture }),
    );
    screen.position.z = BD / 2 + 0.002;
    phone.add(screen);

    // Back: continuous black metal, same material as the body sides.
    const back = new THREE.Mesh(
      new THREE.PlaneGeometry(BW * 0.94, BH * 0.95),
      bodyMat,
    );
    back.position.z = -BD / 2 - 0.002;
    back.rotation.y = Math.PI;
    phone.add(back);

    // Side keys: silent / volume up / volume down (left), power (right).
    const keyMat = new THREE.MeshStandardMaterial({ color: 0x2b2b30, metalness: 0.6, roughness: 0.4 });
    const keyGeo = new THREE.BoxGeometry(0.02, 0.22, 0.04);
    [[-BW / 2, 0.75], [-BW / 2, 0.35], [-BW / 2, 0.0]].forEach(([x, y]) => {
      const k = new THREE.Mesh(keyGeo, keyMat);
      k.position.set(x - 0.005, y, 0);
      phone.add(k);
    });
    const power = new THREE.Mesh(new THREE.BoxGeometry(0.02, 0.32, 0.04), keyMat);
    power.position.set(BW / 2 + 0.005, 0.45, 0);
    phone.add(power);

    // Journey: figure-based. 0 when figure top touches viewport bottom,
    // 0.5 when figure center hits viewport center, 1 when figure bottom
    // leaves viewport top. Angle = progress * TURNS * 360 on Y.
    let scheduled = false;
    function paint() {
      scheduled = false;
      if (document.hidden || held()) return;
      const rect = figure.getBoundingClientRect();
      const vh = window.innerHeight || 1;
      const journey = rect.height + vh;
      const travelled = vh - rect.top;
      const progress = Math.min(1, Math.max(0, travelled / journey));
      phone.rotation.y = progress * TURNS * Math.PI * 2;
      renderer.render(scene, camera);
    }
    function request() {
      if (!scheduled && !document.hidden && !held()) {
        scheduled = true;
        requestAnimationFrame(paint);
      }
    }
    function sync() {
      // Resize to the (possibly changed) shell box, then repaint.
      const next = shell.getBoundingClientRect();
      const nw = Math.max(1, Math.round(next.width || w));
      const nh = Math.max(1, Math.round(next.height || h));
      if (nw !== w || nh !== h) {
        w = nw; h = nh;
        renderer.setSize(nw, nh, false);
        camera.aspect = nw / nh;
        camera.updateProjectionMatrix();
      }
      if (held()) {
        shell.style.display = "";
        renderer.domElement.style.display = "none";
      } else {
        shell.style.display = "none";
        renderer.domElement.style.display = "";
        paint();
      }
    }

    window.addEventListener("scroll", request, { passive: true });
    window.addEventListener("resize", sync, { passive: true });
    window.addEventListener("pageshow", sync);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) paint();
    });
    new MutationObserver(sync).observe(document.body, {
      attributes: true, attributeFilter: ["class"],
    });
    reduce.addEventListener("change", sync);
    if (window.ResizeObserver) new ResizeObserver(sync).observe(figure);

    reduce.addEventListener("change", () => {
      if (held()) {
        shell.style.display = "";
        renderer.domElement.style.display = "none";
      }
    });

    paint();
  }

  function makeScreenTexture(renderer) {
    const canvas = document.createElement("canvas");
    // Logical layout is 880x1840; the backing store is 2x that so the screen
    // stays crisp at the handset's largest size and on high-DPI displays.
    const UW = 880, UH = 1840, S = 2;
    canvas.width = UW * S; canvas.height = UH * S;
    const ctx = canvas.getContext("2d");
    ctx.scale(S, S);
    const tex = new THREE.CanvasTexture(canvas);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.anisotropy = renderer ? renderer.capabilities.getMaxAnisotropy() : 8;
    tex.minFilter = THREE.LinearMipmapLinearFilter;
    tex.magFilter = THREE.LinearFilter;
    tex.generateMipmaps = true;
    function draw() {
      const W = UW, H = UH;
      ctx.fillStyle = "#FBF9F5";
      ctx.fillRect(0, 0, W, H);
      // Chat header: sprout + Kelp name + online dot.
      ctx.fillStyle = "#191817";
      ctx.font = "600 34px Inter, sans-serif";
      ctx.fillText("kelp", 96, 92);
      ctx.fillStyle = "#2E7D46";
      ctx.beginPath(); ctx.arc(W - 70, 80, 12, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = "#E4DED2";
      ctx.fillRect(0, 128, W, 4);
      let y = 200;
      const bubble = (text, me, lines) => {
        ctx.font = "30px Inter, sans-serif";
        const bw = me ? 470 : 560;
        const bh = 70 + lines * 44;
        const x = me ? W - 48 - bw : 48;
        ctx.fillStyle = me ? "#D6E8D6" : "#ECE7DB";
        roundRect(ctx, x, y, bw, bh, 30); ctx.fill();
        ctx.fillStyle = "#191817";
        text.forEach((line, i) => ctx.fillText(line, x + 30, y + 58 + i * 44));
        y += bh + 28;
      };
      bubble(["Sort my inbox?"], false, 1);
      bubble(["On it — scanning", "24 messages…"], true, 2);
      bubble(["Done. 3 need", "a reply."], true, 2);
      bubble(["Draft the first?"], false, 1);
      // Typing indicator: three dots in a bubble.
      ctx.fillStyle = "#ECE7DB";
      roundRect(ctx, 48, y, 170, 84, 30); ctx.fill();
      ctx.fillStyle = "#8A867C";
      [88, 133, 178].forEach((x) => {
        ctx.beginPath(); ctx.arc(x, y + 42, 11, 0, Math.PI * 2); ctx.fill();
      });
      // Status bar.
      ctx.fillStyle = "#191817";
      ctx.font = "600 30px Inter, sans-serif";
      ctx.fillText("9:41", 48, 88);
      // Composer pill with send arrow.
      ctx.strokeStyle = "#191817";
      ctx.lineWidth = 4;
      roundRect(ctx, 40, H - 190, W - 80, 120, 60); ctx.stroke();
      ctx.fillStyle = "#8A867C";
      ctx.font = "italic 32px Newsreader, serif";
      ctx.fillText("Message…", 84, H - 114);
      ctx.fillStyle = "#191817";
      ctx.beginPath(); ctx.arc(W - 130, H - 130, 44, 0, Math.PI * 2); ctx.fill();
      ctx.strokeStyle = "#FBF9F5";
      ctx.lineWidth = 7;
      ctx.beginPath();
      ctx.moveTo(W - 148, H - 130); ctx.lineTo(W - 112, H - 130);
      ctx.moveTo(W - 124, H - 142); ctx.lineTo(W - 108, H - 130);
      ctx.lineTo(W - 124, H - 118);
      ctx.stroke();
    }
    draw();
    tex.needsUpdate = true;
    return { texture: tex };
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function roundedRectShape(w, h, r) {
    const s = new THREE.Shape();
    const x = -w / 2, y = -h / 2;
    s.moveTo(x + r, y);
    s.lineTo(x + w - r, y);
    s.quadraticCurveTo(x + w, y, x + w, y + r);
    s.lineTo(x + w, y + h - r);
    s.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    s.lineTo(x + r, y + h);
    s.quadraticCurveTo(x, y + h, x, y + h - r);
    s.lineTo(x, y + r);
    s.quadraticCurveTo(x, y, x + r, y);
    return s;
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else boot();
})();
