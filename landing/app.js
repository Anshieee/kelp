(() => {
  "use strict";

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const motionButton = document.getElementById("motion-toggle");
  let manuallyPaused = false;
  function motionAllowed() {
    return !reducedMotion.matches && !manuallyPaused;
  }
  function updateMotion() {
    const paused = !motionAllowed();
    document.documentElement.classList.toggle("motion-paused", paused);
    motionButton.setAttribute("aria-pressed", String(paused));
    motionButton.textContent = paused ? "Motion paused" : "Pause motion";
    motionButton.disabled = reducedMotion.matches;
    if (paused) document.getAnimations().forEach((animation) => animation.finish());
  }
  motionButton.addEventListener("click", () => {
    manuallyPaused = !manuallyPaused;
    updateMotion();
  });
  reducedMotion.addEventListener("change", updateMotion);
  updateMotion();

  // Native dialogs keep the background inert and contain keyboard focus.
  const previousFocus = new WeakMap();
  function openDialog(dialog) {
    if (!dialog || dialog.open) return;
    previousFocus.set(dialog, document.activeElement);
    dialog.showModal();
    document.body.classList.add("modal-open");
    dialog.scrollTop = 0;
  }
  function closeDialog(dialog) {
    if (dialog?.open) dialog.close();
  }
  document.querySelectorAll("[data-dossier]").forEach((button) => {
    button.addEventListener("click", () => {
      openDialog(document.getElementById(`dossier-${button.dataset.dossier}`));
    });
  });
  document.querySelectorAll("[data-privacy]").forEach((button) => {
    button.addEventListener("click", () => openDialog(document.getElementById("privacy")));
  });
  document.querySelectorAll("dialog").forEach((dialog) => {
    dialog.querySelector(".close-dialog").addEventListener("click", () => closeDialog(dialog));
    // Keep boundary tabs on the dialog's controls, including desktop Chromium
    // versions that otherwise send the next tab into browser chrome.
    dialog.addEventListener("keydown", (event) => {
      if (event.key !== "Tab") return;
      const controls = [...dialog.querySelectorAll("a[href], button:not([disabled]), input:not([disabled]), [tabindex='0']")]
        .filter((element) => element.getClientRects().length);
      const first = controls[0];
      const last = controls.at(-1);
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog)) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    });
    dialog.addEventListener("close", () => {
      document.body.classList.remove("modal-open");
      previousFocus.get(dialog)?.focus({ preventScroll: true });
    });
    dialog.addEventListener("click", (event) => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeDialog(dialog);
    });
  });

  const input = document.getElementById("prompt");
  const response = document.getElementById("preview-response");
  const starterButtons = [...document.querySelectorAll("[data-starter]")];
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
    response.getAnimations().forEach((animation) => animation.cancel());
    const heading = document.createElement("strong");
    heading.textContent = title;
    const paragraph = document.createElement("p");
    paragraph.textContent = text;
    response.replaceChildren(heading, paragraph);
    response.hidden = false;
    if (motionAllowed()) {
      response.animate(
        [{ opacity: 0.65, transform: "translateY(8px)" }, { opacity: 1, transform: "translateY(0)" }],
        { duration: 240, easing: "cubic-bezier(.16,1,.3,1)" },
      );
    }
  }
  starterButtons.forEach((button) => {
    button.setAttribute("aria-pressed", "false");
    button.addEventListener("click", () => {
      const [prompt, title, text] = starters[button.dataset.starter];
      input.value = prompt;
      starterButtons.forEach((other) => other.setAttribute("aria-pressed", String(other === button)));
      showResponse(title, text);
    });
  });
  input.addEventListener("input", () => {
    starterButtons.forEach((button) => button.setAttribute("aria-pressed", "false"));
  });
  document.getElementById("conversation-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!input.value.trim()) {
      input.focus();
      showResponse("Start with one small thing.", "Type a question or tap a starting point. This is a local preview, so please leave out personal details.");
      return;
    }
    showResponse("A good place to begin.", "This is a preview, not a live assistant. Your message has not been sent anywhere. The dashboard link is a placeholder until the service is connected.");
  });
  document.getElementById("attach-button").addEventListener("click", () => {
    showResponse("Files stay with you here.", "File uploads are not connected in this preview. In the dashboard, a document can give your conversation a starting point.");
  });
  document.getElementById("mic-button").addEventListener("click", () => {
    showResponse("Your microphone is not on.", "Voice input is not connected in this preview. You can type a question or choose a starting point instead.");
  });
  document.querySelector(".demo-approve").addEventListener("click", () => {
    document.querySelector(".example-result").textContent = "Example approved. No file was created and no action was taken.";
  });
  document.querySelector(".demo-reject").addEventListener("click", () => {
    document.querySelector(".example-result").textContent = "Example rejected. Nothing was changed.";
  });

  const notice = document.querySelector(".cookie-bar");
  try {
    notice.hidden = localStorage.getItem("kelp-light-notice-dismissed") === "yes";
  } catch {
    notice.hidden = false;
  }
  document.getElementById("dismiss-cookie").addEventListener("click", () => {
    notice.hidden = true;
    try {
      localStorage.setItem("kelp-light-notice-dismissed", "yes");
    } catch {
      // Dismissal also works when browser storage is unavailable.
    }
  });
})();
