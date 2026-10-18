/** Browser regression: node test-header.mjs [site URL]. */
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE_PATH ?? 'playwright');
const base = process.argv[2] ?? 'http://127.0.0.1:8767';
const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
try {
  const context = await browser.newContext({ extraHTTPHeaders: { DNT: '1', 'Sec-GPC': '1' } });
  await context.addInitScript(() => Object.defineProperty(navigator, 'doNotTrack', { get: () => '1' }));
  await context.route('**/api/track**', route => route.fulfill({ status: 204 }));
  const page = await context.newPage();
  for (const path of ['/', '/guide/', '/privacy/']) {
    for (const language of ['en', 'zh']) {
      await page.goto(new URL(`${path}?lang=${language}`, base).href);
      for (const width of [1440, 1280, 1181, 1180, 1024, 768, 640, 561, 560, 390, 320]) {
        await page.setViewportSize({ width, height: 900 });
        const label = `${path}, ${language}, ${width}px`;
        const header = page.locator('.site-header');
        const links = header.locator('.header-nav .site-link');
        assert.equal(await links.count(), 4, label);
        assert.equal(await header.locator('details').count(), 0, label);
        assert.equal(await header.locator('.header-actions .nav-guide').count(), 1, label);
        for (const link of await links.all()) {
          const box = await link.boundingBox();
          assert(box && box.width >= 36 && box.height >= 36, label);
          assert(box.x >= 0 && box.x + box.width <= width, `Link clipped: ${label}`);
          assert.equal(await link.locator('.site-link-label').isVisible(), width > 1180, label);
          assert(await link.getAttribute('title'), label);
          assert(await link.getAttribute('aria-label'), label);
          {
            const logo = link.locator('.site-link-logo');
            const frame = await logo.boundingBox();
            const artwork = await logo.locator('img, svg').boundingBox();
            assert.equal(artwork.width, 24, label);
            assert.equal(artwork.height, 24, label);
            assert(artwork.x >= frame.x + 2 && artwork.y >= frame.y + 2, `Missing icon inset: ${label}`);
            assert.equal(await logo.evaluate(el => getComputedStyle(el).overflow), 'visible', label);
          }
          const clickable = await link.evaluate(el => {
            const r = el.getBoundingClientRect();
            return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
          });
          assert(clickable, `Link covered: ${label}`);
        }
        const groups = await header.locator('.brand, .header-nav, .header-actions').evaluateAll(els => els.map(el => el.getBoundingClientRect().toJSON()));
        for (let i = 1; i < groups.length; i++)
          assert(groups[i].left >= groups[i - 1].right || groups[i].top >= groups[i - 1].bottom || groups[i].bottom <= groups[i - 1].top, `Header groups overlap: ${label}`);
        assert(groups.at(-1).right <= width, `Utilities overflow: ${label}`);
        assert.equal(await header.locator('[data-language-toggle]').isVisible(), true, label);
      }
    }
  }
  console.log('PASS: 66 page/language/viewport combinations; all four direct links visible, clickable and unclipped.');
} finally {
  await browser.close();
}
