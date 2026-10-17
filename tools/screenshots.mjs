// Capture the README screenshots of each role's main screen (DECISIONS D45).
//
//   npm install --prefix /tmp/pw playwright-core@1.55.0       # once, outside the repo
//   make serve PORT=8765 &                                     # the viewer
//   PLAYWRIGHT_CORE_DIR=/tmp/pw/ node tools/screenshots.mjs http://127.0.0.1:8765
//
// Drives the installed Google Chrome headless. Study IDs and alert IDs are masked on screen before
// each capture: they are pseudonymous, and they also differ with each environment's pseudonym key.
// Prints any browser console errors and exits non-zero if there were some.
import { createRequire } from "module";
import { mkdirSync } from "fs";

const require = createRequire(process.env.PLAYWRIGHT_CORE_DIR || "/tmp/pw/");
const { chromium } = require("playwright-core");
const base = process.argv[2] || "http://127.0.0.1:8000";
const out = new URL("../docs/screenshots/", import.meta.url).pathname;
const chrome = process.env.CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
mkdirSync(out, { recursive: true });

const errors = [];
const browser = await chromium.launch({ executablePath: chrome, headless: true });
const page = await (await browser.newContext({ viewport: { width: 1280, height: 860 }, deviceScaleFactor: 1 })).newPage();
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
page.on("pageerror", (e) => errors.push(String(e)));

async function mask() {
  await page.evaluate(() => {
    const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n = walk.nextNode(); n; n = walk.nextNode()) {
      n.nodeValue = n.nodeValue.replace(/SID-[0-9a-f]{12}/g, "SID-••••••")
                               .replace(/ALR-[0-9a-f]{12}/g, "ALR-••••••");
    }
  });
}

async function as(role) {
  await page.selectOption("#role", role);
  await page.click("#who button[type=submit]");
}

async function shot(name) {
  await mask();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: out + name });
}

await page.goto(base + "/");
// 1. Nurse: open alerts, with a severe-range alert opened.
await page.waitForSelector(".list li");
await page.locator(".list li", { hasText: "Severe-range reading" }).first().click();
await page.waitForSelector("svg.chart");
await shot("nurse-alerts.png");
// 2. Nurse: a patient whose only elevated reading was unconfirmed, so raised no alert.
await page.click("nav >> text=Patients");
await page.waitForSelector(".list li");
await page.locator(".list li").filter({ hasText: "unconfirmed" }).filter({ hasNotText: "open" }).first().click();
await page.waitForSelector(".panel svg.chart");
await shot("nurse-timeline-unconfirmed.png");
// 3. Engineer: recent loads.
await as("engineer");
await page.waitForSelector("table");
await shot("engineer-loads.png");
// 4. Privacy officer: alerts held back for consent.
await as("privacy_officer");
await page.waitForSelector(".flag");
await shot("privacy-held-alerts.png");

await browser.close();
console.log(JSON.stringify({ saved: out, console_errors: errors }));
if (errors.length) process.exit(1);
