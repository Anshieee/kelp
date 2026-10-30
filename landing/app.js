(() => {
  "use strict";

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const canvas = document.getElementById("kelp-field");

  // A seeded, ivory-backed marbled field. Render once; only its transform moves.
  // Ordered dithering leaves soft colour and a tactile edge without image assets.
  function drawField() {
    if (!canvas) return;
    const size = 800;
    canvas.width = size;
    canvas.height = size;
    const context = canvas.getContext("2d");
    if (!context) return;
    const image = context.createImageData(size, size);
    const ivory = [251, 249, 245];
    const coral = [246, 169, 122];
    const butter = [247, 217, 116];
    const green = [146, 210, 165];
    const bayer = [0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5];
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        const nx = (x - 415) / 290;
        const ny = (y - 390) / 325;
        const angle = Math.atan2(ny, nx);
        const warp =
          0.07 * Math.sin(angle * 5 + 1.2) + 0.055 * Math.cos(angle * 3);
        const radius = Math.sqrt(nx * nx + ny * ny);
        const boundary = 1 + warp;
        if (radius > boundary) continue;
        const u =
          nx + 0.11 * Math.sin(ny * 5) + 0.04 * Math.sin(ny * 13 + nx * 4);
        const v = ny + 0.1 * Math.sin(nx * 6 + ny * 3);
        const cells =
          Math.sin(u * 6 + Math.sin(v * 4) * 1.7) *
          Math.cos(v * 5 + Math.sin(u * 3));
        const marble = Math.sin((u * 1.2 + v * 0.6 + cells * 0.5) * 19);
        const ridges = Math.pow(Math.abs(marble), 9);
        const blend = Math.max(0, Math.min(1, (ny + 1) / 2 + cells * 0.17));
        const from = blend < 0.5 ? coral : butter;
        const to = blend < 0.5 ? butter : green;
        const mix = blend < 0.5 ? blend * 2 : (blend - 0.5) * 2;
        const edge = Math.min(1, (boundary - radius) * 23);
        const hole = Math.exp(
          -((nx + 0.2) ** 2 / 0.05 + (ny - 0.02) ** 2 / 0.09),
        );
        const density = Math.max(0, (0.83 - ridges * 0.43 - hole * 0.8) * edge);
        const threshold = (bayer[(y % 4) * 4 + (x % 4)] + 0.5) / 16;
        const noise = (Math.sin(x * 127.1 + y * 311.7) * 43758.5453) % 1;
        const index = (y * size + x) * 4;
        for (let c = 0; c < 3; c++) {
          const colour = from[c] + (to[c] - from[c]) * mix;
          image.data[index + c] =
            threshold < density ? colour + noise * 6 : ivory[c];
        }
        image.data[index + 3] = 255;
      }
    }
    context.putImageData(image, 0, 0);
  }
  drawField();

  let revealTween;
  let scrollTriggers = [];
  function startMotion() {
    if (reducedMotion.matches || !window.gsap || !window.ScrollTrigger) return;
    gsap.registerPlugin(ScrollTrigger);
    revealTween = gsap.from(".hero .reveal", {
      y: 24,
      opacity: 0,
      duration: 1.1,
      stagger: 0.13,
      ease: "power2.out",
      clearProps: "all",
    });
    document.querySelectorAll(".section-shell .reveal").forEach((element) => {
      const tween = gsap.from(element, {
        y: 28,
        opacity: 0,
        duration: 0.8,
        ease: "power2.out",
        clearProps: "all",
        scrollTrigger: { trigger: element, start: "top 93%", once: true },
      });
      scrollTriggers.push(tween);
    });
  }
  function clearMotion() {
    revealTween?.progress(1);
    scrollTriggers.forEach((tween) => {
      tween.scrollTrigger?.kill();
      tween.progress(1);
    });
    scrollTriggers = [];
  }
  startMotion();
  const motionButton = document.getElementById("motion-toggle");
  let manuallyPaused = false;
  function updateMotion() {
    const paused = reducedMotion.matches || manuallyPaused;
    document.body.classList.toggle("motion-paused", paused);
    motionButton.setAttribute("aria-pressed", String(paused));
    motionButton.innerHTML = paused
      ? 'Motion paused <span aria-hidden="true">▷</span>'
      : 'Pause motion <span aria-hidden="true">Ⅱ</span>';
    motionButton.disabled = reducedMotion.matches;
    if (paused) clearMotion();
  }
  motionButton.addEventListener("click", () => {
    manuallyPaused = !manuallyPaused;
    updateMotion();
  });
  reducedMotion.addEventListener("change", updateMotion);
  updateMotion();

  // Native modal dialogs provide focus containment, Escape, and an inert page.
  let previousFocus = null;
  function openDialog(dialog) {
    if (!dialog || dialog.open) return;
    previousFocus = document.activeElement;
    dialog.showModal();
    document.body.classList.add("modal-open");
    dialog.scrollTop = 0;
    // Animate without GSAP transform measurement: reparenting a native dialog
    // during measurement removes it from the browser's modal top layer.
    if (!reducedMotion.matches && !manuallyPaused)
      dialog.animate(
        [
          { opacity: 0, transform: "translateY(20px)" },
          { opacity: 1, transform: "translateY(0)" },
        ],
        { duration: 280, easing: "ease-out" },
      );
  }
  function closeDialog(dialog) {
    if (!dialog?.open) return;
    if (!reducedMotion.matches && !manuallyPaused) {
      const animation = dialog.animate(
        [
          { opacity: 1, transform: "translateY(0)" },
          { opacity: 0, transform: "translateY(12px)" },
        ],
        { duration: 180, easing: "ease-in" },
      );
      animation.finished.then(() => dialog.close()).catch(() => dialog.close());
    } else dialog.close();
  }
  document
    .querySelectorAll("[data-dossier]")
    .forEach((button) =>
      button.addEventListener("click", () =>
        openDialog(
          document.getElementById(`dossier-${button.dataset.dossier}`),
        ),
      ),
    );
  document
    .querySelectorAll("[data-privacy]")
    .forEach((button) =>
      button.addEventListener("click", () =>
        openDialog(document.getElementById("privacy")),
      ),
    );
  document.querySelectorAll("dialog").forEach((dialog) => {
    dialog
      .querySelector(".close-dialog")
      .addEventListener("click", () => closeDialog(dialog));
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      closeDialog(dialog);
    });
    dialog.addEventListener("close", () => {
      document.body.classList.remove("modal-open");
      previousFocus?.focus({ preventScroll: true });
    });
    dialog.addEventListener("click", (event) => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      if (
        event.clientX < rect.left ||
        event.clientX > rect.right ||
        event.clientY < rect.top ||
        event.clientY > rect.bottom
      )
        closeDialog(dialog);
    });
  });

  const input = document.getElementById("prompt");
  const response = document.getElementById("preview-response");
  const starters = {
    bill: [
      "Help me understand a bill.",
      "A bill, in plain language.",
      "In the dashboard, attach the bill and ask about its charges, dates, or unfamiliar terms. This preview does not read files or make payments.",
    ],
    spend: [
      "Help me review my weekly spending.",
      "Start with the week you want to see.",
      "In the dashboard, share a spending CSV and ask for a summary. Here, no bank is connected and no financial information is collected.",
    ],
    doctor: [
      "Help me prepare a note for my doctor.",
      "A place to organise your questions.",
      "You could ask for help arranging your notes before an appointment. Kelp is not a medical professional. Keep personal health information out of this preview.",
    ],
    scam: [
      "Help me look for warning signs in a message.",
      "Pause before you act.",
      "A request for money, a password, or an urgent response can be a reason to check. Contact the organisation using details you already trust. This preview cannot verify a message.",
    ],
  };
  function showResponse(title, text) {
    response.replaceChildren();
    const heading = document.createElement("strong");
    heading.textContent = title;
    const paragraph = document.createElement("p");
    paragraph.textContent = text;
    response.append(heading, paragraph);
    response.hidden = false;
  }
  document.querySelectorAll("[data-starter]").forEach((button) => {
    button.setAttribute("aria-pressed", "false");
    button.addEventListener("click", () => {
      const [prompt, title, text] = starters[button.dataset.starter];
      input.value = prompt;
      document
        .querySelectorAll("[data-starter]")
        .forEach((other) =>
          other.setAttribute("aria-pressed", String(other === button)),
        );
      showResponse(title, text);
    });
  });
  document
    .getElementById("conversation-form")
    .addEventListener("submit", (event) => {
      event.preventDefault();
      if (!input.value.trim()) {
        input.focus();
        showResponse(
          "Start with one small thing.",
          "Type a question or tap a starting point. This is a local preview, so please leave out personal details.",
        );
        return;
      }
      showResponse(
        "A good place to begin.",
        "This is a preview, not a live assistant. Your message has not been sent anywhere. The dashboard link is a placeholder until the service is connected.",
      );
    });
  document
    .getElementById("attach-button")
    .addEventListener("click", () =>
      showResponse(
        "Files stay with you here.",
        "File uploads are not connected in this preview. In the dashboard, a document can give your conversation a starting point.",
      ),
    );
  document
    .getElementById("mic-button")
    .addEventListener("click", () =>
      showResponse(
        "Your microphone is not on.",
        "Voice input is not connected in this preview. You can type a question or choose a starting point instead.",
      ),
    );

  document.querySelector(".demo-approve").addEventListener("click", () => {
    document.querySelector(".example-result").textContent =
      "Example approved. No file was created and no action was taken.";
  });
  document.querySelector(".demo-reject").addEventListener("click", () => {
    document.querySelector(".example-result").textContent =
      "Example rejected. Nothing was changed.";
  });

  const notice = document.querySelector(".cookie-bar");
  try {
    notice.hidden =
      localStorage.getItem("kelp-light-notice-dismissed") === "yes";
  } catch {
    notice.hidden = false;
  }
  document.getElementById("dismiss-cookie").addEventListener("click", () => {
    notice.hidden = true;
    try {
      localStorage.setItem("kelp-light-notice-dismissed", "yes");
    } catch {
      /* Dismissal still works when browser storage is unavailable. */
    }
  });
})();
