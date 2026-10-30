/* Standalone preview. No API calls, analytics, or product state. */
(() => {
  'use strict';

  const motionPreference = window.matchMedia('(prefers-reduced-motion: reduce)');
  const intro = document.querySelector('.intro');
  const dialogs = [...document.querySelectorAll('dialog')];
  let lastTrigger = null;
  let timeline = null;
  let scrollAnimations = [];

  function finishIntro() {
    intro.hidden = true;
    document.querySelectorAll('.hud, .reveal').forEach((element) => {
      element.style.removeProperty('opacity');
      element.style.removeProperty('visibility');
      element.style.removeProperty('transform');
    });
  }

  function setupMotion() {
    if (!window.gsap || motionPreference.matches) return;
    intro.hidden = false;
    // The page stays readable if the CDN fails or the sequence is interrupted.
    const safetyTimer = window.setTimeout(finishIntro, 2200);
    try {
      timeline = window.gsap.timeline({ onComplete: () => {
        clearTimeout(safetyTimer);
        finishIntro();
      } });
      timeline.fromTo('.intro-mark', { opacity: 0, scale: .92 }, { opacity: 1, scale: 1, duration: .45, ease: 'power2.out' })
        .to('.intro-grain', { x: 9, opacity: .15, duration: .12, repeat: 3, yoyo: true }, .25)
        .to(intro, { opacity: 0, duration: .35 }, .9)
        .from('.hud', { opacity: 0, duration: .3 }, 1.05)
        .from('.reveal', { opacity: 0, y: 12, duration: .45, stagger: .08, ease: 'power2.out' }, 1.05);
      if (window.ScrollTrigger) {
        window.gsap.registerPlugin(window.ScrollTrigger);
        scrollAnimations = [...document.querySelectorAll('.roster-item, .trust-intro, .trust-details')].map((element) => window.gsap.from(element, {
          opacity: 0, y: 15, duration: .5, ease: 'power2.out',
          scrollTrigger: { trigger: element, start: 'top 94%', once: true }
        }));
      }
    } catch {
      finishIntro();
    }
  }

  const wordmark = document.querySelector('.wordmark');
  function waveWordmark() {
    if (motionPreference.matches || !window.gsap) return;
    window.gsap.fromTo('.wordmark > span', { y: 0 }, { y: -6, duration: .18, stagger: .055, repeat: 1, yoyo: true, overwrite: true, ease: 'sine.inOut' });
  }
  wordmark.addEventListener('pointerenter', waveWordmark);
  wordmark.addEventListener('focus', waveWordmark);

  function openDialog(dialog, trigger) {
    if (!dialog || dialog.open) return;
    lastTrigger = trigger;
    dialog.showModal();
    document.body.classList.add('modal-open');
    dialog.scrollTop = 0;
  }

  document.querySelectorAll('[data-dossier]').forEach((trigger) => {
    trigger.addEventListener('click', () => openDialog(document.getElementById(`dossier-${trigger.dataset.dossier}`), trigger));
  });
  document.querySelectorAll('[data-privacy]').forEach((trigger) => {
    trigger.addEventListener('click', (event) => {
      event.preventDefault();
      openDialog(document.getElementById('privacy'), trigger);
    });
  });
  dialogs.forEach((dialog) => {
    dialog.querySelector('.close-dialog').addEventListener('click', () => dialog.close());
    dialog.addEventListener('keydown', (event) => {
      if (event.key !== 'Tab') return;
      const controls = [...dialog.querySelectorAll('a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])')].filter((element) => element.getClientRects().length);
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog)) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    });
    dialog.addEventListener('click', (event) => {
      if (event.target !== dialog) return;
      const bounds = dialog.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
    });
    dialog.addEventListener('close', () => {
      document.body.classList.remove('modal-open');
      lastTrigger?.focus({ preventScroll: true });
    });
  });

  const cookieBar = document.querySelector('.cookie-bar');
  try { cookieBar.hidden = localStorage.getItem('kelp-site-notice-dismissed') === '1'; }
  catch { cookieBar.hidden = false; }
  document.getElementById('dismiss-cookie').addEventListener('click', () => {
    cookieBar.hidden = true;
    try { localStorage.setItem('kelp-site-notice-dismissed', '1'); } catch { /* Storage is optional. */ }
  });

  const roster = document.querySelector('.roster');
  function highlight(trigger) {
    roster.classList.toggle('has-active', Boolean(trigger));
    roster.querySelectorAll('.roster-trigger').forEach((item) => item.classList.toggle('is-active', item === trigger));
    fieldEnergy = trigger ? .8 : 0;
  }
  roster.querySelectorAll('.roster-trigger').forEach((trigger) => {
    trigger.addEventListener('pointerenter', (event) => { if (event.pointerType !== 'touch') highlight(trigger); });
    trigger.addEventListener('focus', () => highlight(trigger));
  });
  roster.addEventListener('pointerleave', () => highlight(roster.contains(document.activeElement) ? document.activeElement : null));
  roster.addEventListener('focusout', (event) => { if (!roster.contains(event.relatedTarget)) highlight(null); });

  // One low-resolution halftone field. Parametric ribbons suggest growing kelp.
  const canvas = document.getElementById('kelp-field');
  const context = canvas.getContext('2d', { alpha: true });
  const hero = document.querySelector('.hero');
  let width = 0;
  let height = 0;
  let frame = 0;
  let previousTime = 0;
  let visible = true;
  let pointerX = 0;
  let pointerY = 0;
  let fieldEnergy = 0;
  let phase = 0;
  const cell = 3;
  const bayer = [0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5];

  function resizeCanvas() {
    width = Math.min(950, Math.round(canvas.clientWidth));
    height = Math.round(canvas.clientHeight * width / Math.max(1, canvas.clientWidth));
    canvas.width = width;
    canvas.height = height;
    if (!frame) drawField(phase);
  }

  function drawField(time) {
    if (!context || width <= 0 || height <= 0) return;
    context.clearRect(0, 0, width, height);
    const drift = motionPreference.matches ? 0 : Math.sin(time * .00015) * .035;
    const px = motionPreference.matches ? 0 : pointerX * .03;
    const py = motionPreference.matches ? 0 : pointerY * .025;
    for (let y = 0; y < height; y += cell) {
      const v = y / height;
      const ribbons = Array.from({ length: 5 }, (_, stem) => {
        const offset = stem * 1.7;
        const bend = Math.sin(v * 5.6 + offset + time * .00012) * (.08 + v * .045);
        const center = .25 + stem * .13 + bend + drift + px * (1 - v);
        const taper = Math.sin(Math.PI * Math.min(1, Math.max(0, (v - stem * .055) / (1 - stem * .055))));
        const leafWave = Math.sin(v * 19 + offset + time * .00008 + py);
        const spread = .008 + Math.pow(Math.abs(leafWave), 1.5) * .065 * Math.max(0, taper);
        return { center, taper, spread };
      });
      for (let x = 0; x < width; x += cell) {
        const u = x / width;
        let density = 0;
        let edge = 0;
        for (const { center, taper, spread } of ribbons) {
          const distance = Math.abs(u - center);
          const ribbon = Math.max(0, 1 - distance / spread);
          density = Math.max(density, ribbon * (.27 + .62 * taper));
          if (distance < spread && distance > spread * .7) edge = Math.max(edge, .18 * taper);
        }
        const halo = Math.max(0, 1 - Math.hypot((u - .6) * 1.3, (v - .5) * .9));
        const noise = ((Math.sin(x * 12.9898 + y * 78.233) * 43758.5453) % 1 + 1) % 1;
        const threshold = bayer[((y / cell) % 4) * 4 + ((x / cell) % 4)] / 16;
        const grain = density + halo * .035 + fieldEnergy * .035 * noise;
        if (grain < threshold + noise * .13) continue;
        const alpha = Math.min(.82, .12 + density * .7 + noise * .11);
        context.fillStyle = edge > .1 && stemColor(u, v) ? `rgba(226,84,63,${alpha * .55})` : `rgba(111,191,160,${alpha})`;
        const size = density > .6 ? 2 : 1.3;
        context.fillRect(x, y, size, size);
      }
    }
  }

  function stemColor(u, v) { return Math.sin(u * 14 + v * 8) > .7; }
  function animate(time) {
    frame = 0;
    if (motionPreference.matches || !visible || document.hidden || dialogs.some((dialog) => dialog.open)) return;
    if (time - previousTime > 65) {
      phase = time;
      drawField(time);
      previousTime = time;
    }
    frame = requestAnimationFrame(animate);
  }
  function startField() {
    if (!frame && !motionPreference.matches && visible && !document.hidden && !dialogs.some((dialog) => dialog.open)) frame = requestAnimationFrame(animate);
  }
  hero.addEventListener('pointermove', (event) => {
    const bounds = hero.getBoundingClientRect();
    pointerX = (event.clientX - bounds.left) / bounds.width - .5;
    pointerY = (event.clientY - bounds.top) / bounds.height - .5;
  }, { passive: true });
  hero.addEventListener('pointerleave', () => { pointerX = 0; pointerY = 0; });
  if ('ResizeObserver' in window) new ResizeObserver(resizeCanvas).observe(canvas);
  else window.addEventListener('resize', resizeCanvas, { passive: true });
  if ('IntersectionObserver' in window) new IntersectionObserver((entries) => {
    visible = entries[0].isIntersecting;
    if (visible) startField();
  }).observe(hero);
  document.addEventListener('visibilitychange', startField);
  dialogs.forEach((dialog) => dialog.addEventListener('close', startField));
  motionPreference.addEventListener('change', () => {
    if (motionPreference.matches) {
      cancelAnimationFrame(frame);
      frame = 0;
      timeline?.kill();
      scrollAnimations.forEach((animation) => {
        animation.scrollTrigger?.kill();
        animation.progress(1).kill();
      });
      window.gsap?.set('.trust-intro, .trust-details', { clearProps: 'all' });
      finishIntro();
      phase = 0;
      drawField(0);
    } else startField();
  });

  setupMotion();
  resizeCanvas();
  startField();
})();
