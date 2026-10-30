(() => {
  "use strict";

  const carousel = document.querySelector(".c-example-stage");
  if (!carousel) return;

  const slides = [
    ...carousel.querySelectorAll('[aria-roledescription="slide"]'),
  ];
  const dots = [...carousel.querySelectorAll(".c-example-dots button")];
  const pause = carousel.querySelector(".c-example-pause");
  const copy = carousel.querySelector(".c-example-copy");
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
  let index = 0;
  let timer;
  let userPaused = false;
  let hovering = false;
  let inView = false;

  function show(next) {
    index = next;
    slides.forEach((slide, i) => {
      slide.hidden = i !== index;
    });
    dots.forEach((dot, i) =>
      dot.setAttribute("aria-pressed", String(i === index)),
    );
  }

  function sync() {
    clearInterval(timer);
    const motionPaused =
      reducedMotion.matches ||
      document.body.classList.contains("motion-paused");
    const paused = userPaused || motionPaused;
    pause.disabled = motionPaused;
    pause.textContent = motionPaused
      ? "Examples paused"
      : userPaused
        ? "Play examples"
        : "Pause examples";
    pause.setAttribute("aria-pressed", String(paused));
    const running =
      !paused &&
      !hovering &&
      inView &&
      !document.hidden &&
      !carousel.contains(document.activeElement);
    copy.setAttribute("aria-live", running ? "off" : "polite");
    if (running)
      timer = setInterval(() => show((index + 1) % slides.length), 6500);
  }

  dots.forEach((dot, i) =>
    dot.addEventListener("click", () => {
      userPaused = true;
      sync();
      show(i);
    }),
  );
  pause.addEventListener("click", () => {
    userPaused = !userPaused;
    sync();
  });
  carousel.addEventListener("mouseenter", () => {
    hovering = true;
    sync();
  });
  carousel.addEventListener("mouseleave", () => {
    hovering = false;
    sync();
  });
  carousel.addEventListener("focusin", sync);
  carousel.addEventListener("focusout", () => queueMicrotask(sync));
  document.addEventListener("visibilitychange", sync);
  reducedMotion.addEventListener("change", () => {
    if (reducedMotion.matches) show(0);
    sync();
  });
  new MutationObserver(sync).observe(document.body, {
    attributes: true,
    attributeFilter: ["class"],
  });
  new IntersectionObserver(([entry]) => {
    inView = entry.isIntersecting;
    sync();
  }).observe(carousel);
  carousel.querySelector(".c-example-controls").hidden = false;
  sync();
})();
