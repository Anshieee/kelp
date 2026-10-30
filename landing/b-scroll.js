/* Optional scroll layer. Include with defer after app.js and b-scroll.css.
   All hooks belong to the existing page. No markup changes are required. */
(() => {
  "use strict";

  function boot() {
    const hero = document.querySelector(".hero");
    if (!hero || !window.IntersectionObserver || !Element.prototype.animate) return;

    const preference = window.matchMedia("(prefers-reduced-motion: reduce)");
    const art = hero.querySelector(".hero-art");
    const selector = [
      ".conversation-intro",
      ".conversation > .stage",
      ".possibilities-heading",
      ".proof-list > li",
      ".trust-heading",
      ".trust-strip > article",
      ".closing > h2",
      ".closing-actions",
      ".closing > .destination-note",
      ".footer-main",
    ].join(",");
    const items = [...document.querySelectorAll(selector)];

    // Retire only the baseline scroll reveals, not its hero entrance, artwork,
    // dialog motion, or controls. Safe when the old GSAP loop has been removed.
    const legacy = [...document.querySelectorAll(".section-shell .reveal")];
    window.ScrollTrigger?.getAll().forEach((trigger) => {
      if (!legacy.includes(trigger.trigger)) return;
      trigger.animation?.progress(1);
      trigger.kill();
    });
    if (window.gsap) {
      window.gsap.killTweensOf(legacy);
      window.gsap.set(legacy, { clearProps: "opacity,transform" });
    }

    const seen = new WeakSet();
    const active = new Map();
    const originalTransform = art?.style.transform || "";
    let observer;
    let frame = 0;
    let heroVisible = false;
    let heroTop = 0;
    let heroHeight = 1;
    let previousOffset = -1;

    const paused = () =>
      preference.matches || document.body.classList.contains("motion-paused");

    function settle(element) {
      const animation = active.get(element);
      if (!animation) return;
      animation.cancel();
      active.delete(element);
    }

    function reveal(element) {
      if (seen.has(element)) return;
      seen.add(element);
      observer?.unobserve(element);
      if (paused() || element.contains(document.activeElement)) return;
      const style = getComputedStyle(element);
      const distance = style.getPropertyValue("--b-scroll-distance").trim() || "12px";
      const animation = element.animate(
        [
          { opacity: 0, transform: `translateY(${distance})` },
          { opacity: 1, transform: "translateY(0)" },
        ],
        {
          duration: parseFloat(style.getPropertyValue("--b-scroll-duration")) || 720,
          delay: parseFloat(style.getPropertyValue("--b-scroll-delay")) || 0,
          easing: style.getPropertyValue("--ease-out").trim() || "cubic-bezier(0.23, 1, 0.32, 1)",
          fill: "backwards",
        },
      );
      active.set(element, animation);
      animation.onfinish = () => settle(element);
    }

    function observeUpcoming() {
      observer?.disconnect();
      if (paused()) return;
      observer = new IntersectionObserver(
        (entries) => {
          entries.forEach((entry) => {
            if (entry.isIntersecting) reveal(entry.target);
          });
        },
        { rootMargin: "0px 0px 64px 0px", threshold: 0 },
      );
      items.forEach((element) => {
        // Never fade out something already being read (including restored
        // scroll positions and in-page anchor destinations).
        if (element.getBoundingClientRect().top < window.innerHeight) {
          seen.add(element);
        }
        if (!seen.has(element)) observer.observe(element);
      });
    }

    function paintDrift() {
      frame = 0;
      if (!art || paused() || !heroVisible || document.hidden) return;
      const progress = Math.max(0, Math.min(1, (window.scrollY - heroTop) / heroHeight));
      const offset = Math.round(progress * 18 * 100) / 100;
      if (offset === previousOffset) return;
      previousOffset = offset;
      // Only decorative artwork drifts. Copy, controls, and focus stay still.
      art.style.transform = `${originalTransform} translateY(${offset}px)`.trim();
    }

    function requestDrift() {
      if (!frame && !paused() && heroVisible && !document.hidden) {
        frame = requestAnimationFrame(paintDrift);
      }
    }

    function measure() {
      const rect = hero.getBoundingClientRect();
      heroTop = rect.top + window.scrollY;
      heroHeight = Math.max(1, rect.height);
      requestDrift();
    }

    function sync() {
      cancelAnimationFrame(frame);
      frame = 0;
      active.forEach((_, element) => settle(element));
      if (art) art.style.transform = originalTransform;
      previousOffset = -1;
      observeUpcoming();
      requestDrift();
    }

    // Keyboard focus must never land on temporarily transparent content.
    document.addEventListener("focusin", (event) => {
      items.forEach((element) => {
        if (!element.contains(event.target)) return;
        seen.add(element);
        observer?.unobserve(element);
        settle(element);
      });
    });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        cancelAnimationFrame(frame);
        frame = 0;
        active.forEach((_, element) => settle(element));
      } else requestDrift();
    });
    window.addEventListener("scroll", requestDrift, { passive: true });
    window.addEventListener("resize", measure, { passive: true });
    window.addEventListener("pageshow", sync);
    preference.addEventListener("change", sync);
    new MutationObserver(sync).observe(document.body, {
      attributes: true,
      attributeFilter: ["class"],
    });
    new IntersectionObserver(([entry]) => {
      heroVisible = entry.isIntersecting;
      if (heroVisible) requestDrift();
      else {
        cancelAnimationFrame(frame);
        frame = 0;
      }
    }).observe(hero);
    if (window.ResizeObserver) new ResizeObserver(measure).observe(hero);
    measure();
    sync();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else boot();
})();
