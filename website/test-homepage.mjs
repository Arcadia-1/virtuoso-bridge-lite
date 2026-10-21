/** Browser regression: node test-homepage.mjs [site URL] [screenshot directory]. */
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir } from "node:fs/promises";
import { join } from "node:path";

const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE_PATH ?? "playwright");
const base = new URL(process.argv[2] ?? "http://127.0.0.1:8767");
const screenshots = process.argv[3];
if (screenshots) await mkdir(screenshots, { recursive: true });
const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
const errors = [];

async function context(options = {}) {
  const result = await browser.newContext({
    reducedMotion: "reduce", extraHTTPHeaders: { DNT: "1", "Sec-GPC": "1" }, ...options,
  });
  await result.addInitScript(() => Object.defineProperty(navigator, "doNotTrack", { get: () => "1" }));
  await result.route("**/*", route => {
    const url = new URL(route.request().url());
    if (url.origin !== base.origin) return route.abort();
    if (url.pathname.startsWith("/api/track")) return route.fulfill({ status: 204 });
    return route.continue();
  });
  result.on("page", page => page.on("pageerror", error => errors.push(error.message)));
  return result;
}

try {
  const active = await context();
  const page = await active.newPage();
  for (const lang of ["en", "zh"]) {
    for (const width of [1440, 1280, 1024, 768, 640, 390, 320]) {
      const label = lang + " / " + width;
      await page.setViewportSize({ width, height: 900 });
      await page.goto(new URL("/?lang=" + lang, base).href, { waitUntil: "networkidle" });
      await page.locator("#eco-search-controls").waitFor({ state: "visible" });
      const metrics = await page.evaluate(() => {
        const primary = document.querySelector(".intro .primary");
        const guide = document.querySelector("#docs .links a");
        return {
          overflow: document.documentElement.scrollWidth > innerWidth + 1,
          header: document.querySelector(".site-header").getBoundingClientRect().height,
          ctaFont: parseFloat(getComputedStyle(primary).fontSize),
          ctaBottom: primary.getBoundingClientRect().bottom,
          headingWeight: Number(getComputedStyle(document.querySelector(".intro h1")).fontWeight),
          descriptionFont: parseFloat(getComputedStyle(document.querySelector(".hero-summary")).fontSize),
          guideFont: getComputedStyle(guide).fontSize,
          guideZhFont: getComputedStyle(guide.querySelector(".zh")).fontSize,
          guideColor: getComputedStyle(guide).color,
          guideZhColor: getComputedStyle(guide.querySelector(".zh")).color,
          start: document.querySelector("#start").getBoundingClientRect().top + scrollY,
          comparisonCell: getComputedStyle(document.querySelector(".compare td")).display,
          diagramLoaded: document.querySelectorAll(".hero-flow svg .system-routes path").length > 0,
        };
      });
      assert(!metrics.overflow, "Page overflow: " + label);
      assert(metrics.header < 60, "Header consumes the first screen: " + label);
      assert(metrics.ctaFont >= 13, "Tiny primary action: " + label);
      assert(metrics.ctaBottom < 900, "Primary action below first screen: " + label);
      assert(metrics.headingWeight >= 700, "Weak heading: " + label);
      assert(metrics.descriptionFont >= 14, "Small body text: " + label);
      assert.equal(metrics.guideZhFont, metrics.guideFont, "Chinese link font: " + label);
      assert.equal(metrics.guideZhColor, metrics.guideColor, "Chinese link contrast: " + label);
      assert(metrics.diagramLoaded, "Missing bridge signal diagram: " + label);
      if (width >= 1280) assert(metrics.start < 2100, "Quick start pushed down: " + label);
      if (width <= 640) assert.equal(metrics.comparisonCell, "block", "Mobile comparison: " + label);
      assert.equal(await page.locator("#ecosystem-index li:visible").count(), 53, label);
      assert.equal(await page.locator("#categories button").count(), 5, label);
      if (screenshots && [1440, 390].includes(width)) {
        await page.screenshot({ path: join(screenshots, lang + "-" + width + "-first-screen.png") });
        await page.locator("#ecosystem").screenshot({ path: join(screenshots, lang + "-" + width + "-ecosystem.png") });
        await page.locator("#start").screenshot({ path: join(screenshots, lang + "-" + width + "-quick-start.png") });
      }
      console.log(label, JSON.stringify(metrics));
    }
  }

  const visibleProjects = page.locator("#ecosystem-index li:visible");
  const input = page.locator("#search");
  await input.fill("  PANDA   layout ");
  assert.equal(await visibleProjects.count(), 1);
  assert((await visibleProjects.first().textContent()).includes("PKU-IDEA/PANDA"));
  assert.equal(await page.locator("#search-status").textContent(), "1 个匹配项目");
  await page.locator("[data-language-toggle]").click();
  await page.waitForFunction(() => document.querySelector("#search-status").textContent === "1 matching projects");
  assert.equal(await visibleProjects.count(), 1);
  assert.equal(await input.inputValue(), "  PANDA   layout ");
  await input.fill("版图");
  assert((await visibleProjects.count()) > 0, "Chinese search must work in English too");
  await input.fill("no-such-project-<script>");
  assert.equal(await visibleProjects.count(), 0);
  assert.equal(await page.locator("#eco-empty").isVisible(), true);
  await page.locator("#index-clear").click();
  assert.equal(await visibleProjects.count(), 53);
  assert.equal(await input.evaluate(el => el === document.activeElement), true);
  assert.equal(await page.locator("#eco-empty").isVisible(), false);
  await input.fill("no-such-project");
  await page.locator('.eco-categories a[href="#eco-tools"]').click();
  assert.equal(await input.inputValue(), "");
  assert.equal(await visibleProjects.count(), 53);
  assert.equal(await page.locator("#eco-tools").isVisible(), true);
  assert.equal(new URL(page.url()).hash, "#eco-tools");
  // Search works via keyboard alone, and its focus is visibly indicated.
  await input.focus();
  assert.equal(await input.evaluate(el => getComputedStyle(el).outlineStyle), "solid");
  await page.keyboard.type("mock-virtuoso");
  assert.equal(await visibleProjects.count(), 1);
  await page.keyboard.press("Tab");
  assert.equal(await page.locator("#index-clear").evaluate(el => el === document.activeElement), true);
  await page.keyboard.press("Enter");
  assert.equal(await visibleProjects.count(), 53);
  await active.close();

  const plain = await context({ javaScriptEnabled: false, viewport: { width: 390, height: 844 } });
  const noJs = await plain.newPage();
  await noJs.goto(base.href);
  assert.equal(await noJs.locator("#ecosystem-index li:visible").count(), 53);
  assert.equal(await noJs.locator(".eco-categories a:visible").count(), 5);
  assert.equal(await noJs.locator("#eco-search-controls").isVisible(), false);
  assert.equal(await noJs.locator(".intro .primary").isVisible(), true);
  assert.equal(await noJs.locator(".archive").isVisible(), false);
  await plain.close();

  const blocked = await context();
  await blocked.route("**/assets/home/archive.mjs*", route => route.abort());
  const withoutModule = await blocked.newPage();
  await withoutModule.goto(base.href);
  assert.equal(await withoutModule.locator("#ecosystem-index li:visible").count(), 53);
  assert.equal(await withoutModule.locator("#eco-search-controls").isVisible(), false);
  assert.equal(await withoutModule.locator(".archive").isVisible(), false);
  await blocked.close();
  assert.deepEqual(errors, []);
  console.log("PASS: 14 responsive/language layouts; search, categories, keyboard, language switch and no-script fallbacks.");
} finally {
  await browser.close();
}
