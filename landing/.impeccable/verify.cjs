/* Run with Playwright and axe-core available through NODE_PATH. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

(async () => {
  const out = path.join(__dirname, "review");
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || "/usr/bin/chromium", args: ["--no-sandbox"] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const results = { widths: [], axe: [], keyboard: [], interactions: [], reducedMotion: false, errors };
  const url = process.env.KELP_QA_URL || "http://127.0.0.1:4277/landing/";
  const axeSource = fs.readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");
  async function axe(name) {
    if (!(await page.evaluate(() => !!window.axe))) await page.addScriptTag({ content: axeSource });
    const violations = await page.evaluate(async () => (await axe.run(document, { runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21aa"] } })).violations.map(({ id, impact, nodes }) => ({ id, impact, targets: nodes.map((node) => node.target) })));
    results.axe.push({ name, violations });
    assert.equal(violations.length, 0, `${name}: ${JSON.stringify(violations)}`);
  }
  async function checkOverflow(name) {
    const value = await page.evaluate(() => {
      const dialog = document.querySelector("dialog[open]");
      return { page: document.documentElement.scrollWidth <= innerWidth, dialog: !dialog || dialog.scrollWidth <= dialog.clientWidth + 1 };
    });
    assert(value.page && value.dialog, `${name}: ${JSON.stringify(value)}`);
  }
  await page.goto(url);
  await page.evaluate(() => document.fonts.ready);
  await axe("desktop with storage notice");
  await page.locator("#dismiss-cookie").click();
  await page.reload();
  assert(await page.locator(".cookie-bar").isHidden());
  results.interactions.push("storage notice dismisses and persists");
  await page.keyboard.press("Tab");
  assert(await page.locator(".skip-link").evaluate((element) => element === document.activeElement));
  await page.keyboard.press("Enter");
  await page.locator("#prompt").fill("");
  await page.locator("#prompt").press("Enter");
  assert(await page.locator("#preview-response").innerText().then((text) => text.includes("Start with one small thing.")));
  assert(await page.locator("#prompt").evaluate((element) => element === document.activeElement));
  results.keyboard.push("skip link reachable; empty submit returns focus to input");
  for (const name of ["bill", "spend", "doctor", "scam"]) {
    const button = page.locator(`[data-starter=${name}]`);
    await button.click();
    assert.equal(await button.getAttribute("aria-pressed"), "true");
    assert.equal(await page.locator('[data-starter][aria-pressed="true"]').count(), 1);
    assert(await page.locator("#preview-response").isVisible());
  }
  await page.locator("#prompt").fill("A custom question <script>alert(1)</script>");
  assert.equal(await page.locator('[data-starter][aria-pressed="true"]').count(), 0);
  let requests = 0;
  page.on("request", () => requests++);
  await page.locator("#prompt").press("Enter");
  assert.match(await page.locator("#preview-response").innerText(), /has not been sent anywhere/);
  await page.locator("#attach-button").click();
  assert.match(await page.locator("#preview-response").innerText(), /not connected/);
  await page.locator("#mic-button").click();
  assert.match(await page.locator("#preview-response").innerText(), /microphone is not on/);
  assert.equal(requests, 0, "Local interactions must not send network requests");
  results.interactions.push("all four starters, custom input, empty input, attachment and microphone disclosures; no network requests");
  await axe("active response");
  await page.locator("#motion-toggle").click();
  assert.equal(await page.locator("#motion-toggle").getAttribute("aria-pressed"), "true");
  await page.locator('[data-starter="bill"]').click();
  assert.equal(await page.evaluate(() => document.getAnimations().length), 0);
  await page.locator("#a-conversation").screenshot({ path: path.join(out, "response.png") });
  results.interactions.push("manual motion pause suppresses response animation");

  for (const width of [320, 360, 390, 540, 768, 800, 1024, 1280, 1440, 1920]) {
    await page.setViewportSize({ width, height: 1000 });
    await checkOverflow(`page ${width}`);
    for (const name of ["chat", "approvals", "scheduled"]) {
      const opener = page.locator(`[data-dossier=${name}]`);
      await opener.focus();
      await page.keyboard.press("Enter");
      const dialog = page.locator(`#dossier-${name}`);
      assert(await dialog.evaluate((element) => element.open));
      assert(await dialog.locator(".close-dialog").evaluate((element) => element === document.activeElement));
      await checkOverflow(`${name} ${width}`);
      await page.keyboard.press("Shift+Tab");
      assert(await dialog.evaluate((element) => element.contains(document.activeElement)), `${name} ${width} Shift+Tab: ${await page.evaluate(() => document.activeElement.tagName)}`);
      await page.keyboard.press("Tab");
      assert(await dialog.evaluate((element) => element.contains(document.activeElement)), `${name} ${width} Tab: ${await page.evaluate(() => document.activeElement.tagName)}`);
      if (name === "approvals") {
        await dialog.locator(".demo-approve").click();
        assert.match(await dialog.locator(".example-result").innerText(), /No file was created/);
        await dialog.locator(".demo-reject").click();
        assert.match(await dialog.locator(".example-result").innerText(), /Nothing was changed/);
      }
      if ([390, 1440].includes(width)) await axe(`${name} ${width}`);
      await page.keyboard.press("Escape");
      assert(await dialog.evaluate((element) => !element.open));
      assert(await opener.evaluate((element) => element === document.activeElement));
    }
    results.widths.push({ width, overflow: false, allDialogs: "pass" });
  }
  results.keyboard.push("all dialogs: Enter, initial close focus, forward/reverse focus containment, Escape, opener focus restoration at all widths");
  results.interactions.push("approve and reject are explicit simulations");
  await page.locator(".footer-bottom [data-privacy]").click();
  await axe("privacy dialog");
  await page.locator("#privacy .close-dialog").click();
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.locator('[data-starter="doctor"]').click();
  assert.equal(await page.evaluate(() => document.getAnimations().length), 0);
  assert.equal(await page.locator("#motion-toggle").isDisabled(), true);
  assert.equal(await page.evaluate(() => getComputedStyle(document.documentElement).scrollBehavior), "auto");
  results.reducedMotion = true;

  // Final resting screenshots: reload clears local example state.
  await page.reload();
  await page.evaluate(() => document.fonts.ready);
  for (const [width, height, name] of [[1440, 1000, "desktop"], [390, 844, "mobile"], [768, 1024, "tablet"]]) {
    await page.setViewportSize({ width, height });
    await page.evaluate(() => scrollTo(0, 0));
    await page.screenshot({ path: path.join(out, `${name}.png`), fullPage: true });
  }
  for (const [width, name] of [[1440, "desktop"], [390, "mobile"]]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    await page.locator('[data-dossier="approvals"]').click();
    await page.screenshot({ path: path.join(out, `approvals-${name}.png`) });
    await page.keyboard.press("Escape");
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await axe("final mobile");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await axe("final desktop");
  assert.equal(errors.length, 0);
  fs.writeFileSync(path.join(out, "verification.json"), JSON.stringify(results, null, 2) + "\n");
  console.log(JSON.stringify(results, null, 2));
  await browser.close();
})().catch((error) => { console.error(error); process.exit(1); });
