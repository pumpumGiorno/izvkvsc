// Прогоняет vk_export.js на HTML-фикстурах в headless Chromium и печатает JSON с результатами.
// Запускается из tests/test_vk_export_js.py; нужен Node и пакет playwright.
const path = require("path");
const fs = require("fs");
const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "..", "..");
const SCRIPT = process.env.VK2SC_SCRIPT || path.join(ROOT, "vk_export.js");
const FIXTURES = path.join(__dirname, "fixtures");

(async () => {
  const launchOpts = {};
  if (process.env.VK2SC_CHROMIUM) launchOpts.executablePath = process.env.VK2SC_CHROMIUM;
  const browser = await chromium.launch(launchOpts);
  const results = {};
  const only = process.env.VK2SC_ONLY ? process.env.VK2SC_ONLY.split(",") : null;
  for (const name of fs.readdirSync(FIXTURES).filter((f) => f.endsWith(".html") && (!only || only.includes(f))).sort()) {
    const context = await browser.newContext({ acceptDownloads: true });
    const page = await context.newPage();
    const dialogs = [];
    const console_ = [];
    page.on("dialog", (d) => {
      dialogs.push(d.message());
      d.dismiss().catch(() => {});
    });
    page.on("console", (m) => console_.push(`${m.type()}: ${m.text()}`));
    await page.goto("file://" + path.join(FIXTURES, name));
    await page.evaluate(() => {
      window.VK2SC_OPTIONS = { stepMs: 150, idleRounds: 4, maxMinutes: 1 };
    });
    const downloadPromise = page.waitForEvent("download", { timeout: 60000 }).catch(() => null);
    await page.addScriptTag({ path: SCRIPT });
    const result = await page.evaluate(() => window.vk2scDone);
    const error = await page.evaluate(() => window.vk2scError || null);
    let file = null;
    if (result) {
      const dl = await downloadPromise;
      if (dl) {
        file = { name: dl.suggestedFilename(), text: fs.readFileSync(await dl.path(), "utf-8") };
      }
    }
    results[name] = { result, error, dialogs, file, console: console_ };
    await context.close();
  }
  await browser.close();
  process.stdout.write(JSON.stringify(results));
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
