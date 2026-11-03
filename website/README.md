# Virtuoso Bridge website

The landing page, guide, privacy page and SVG identity live in `../docs/`. The
landing page is the front door; `/guide/` holds the full documentation that used
to be in the README, which now only links here. `docs/guide/index.html` is plain
HTML: edit it directly.
`npm ci && npm run check && npm test && npm run build` builds this isolated
website package. `npm run preview` serves the full site with local Cloudflare
Durable Object storage. It does not connect to Cadence.

The homepage uses a cool paper archive layout, a staggered two-line headline,
and a signal diagram from AI agent through the bridge to three tools. SKILL/CLI
sits with the bridge. Spectre, Virtuoso and Maestro appear from left to right,
with local/remote and non-live illustration captions. The render-blocking CSS
starts the route entrance before the first paint, independently of module timing;
it completes even if the module fails. No-script and reduced-motion readers see
a complete static illustration. Signal geometry is sampled once; small
transform/opacity layers animate over the SVG, pausing offscreen and in hidden tabs.
The first screen names the Lite core: Spectre simulation and PDK/CDF callbacks,
everyday schematic/symbol/GDS operations, and raw SKILL for the rest.
Advanced configuration remains optional.
All public pages keep Docs beside the brand and Analog Canvas, Presented by
TokenZhang, issue reporting, GitHub and the language switch on the right.
The analytics footer is unchanged. Homepage styles and modules live in
`docs/assets/home/` and are copied by the regular build. Edit `docs/index.html`
directly: all 53 project cards, descriptions and destinations are static HTML,
organized into five columns. There is no separate detail panel or index. Architecture and Citation follow as
sections 05/06, moved verbatim from the guide; old guide anchors redirect to them.
The comparison uses paired archive leaves with matching typewriter criterion headings
and unweighted body text on both sides. Quick start and Citation share light paper
cards, orange tabs and typewriter format labels; their content and copy targets are
unchanged. All six section headings share the same responsive type size.
All five columns fit desktop widths. Selecting a collection pans it into the center
with the same eased transition at every width. Repeated side sets prevent blank
edges; only the outer half-column on each side has graduated blur.
Each column supports native vertical wheel/touch scrolling (without visible
scrollbars) and keyboard links,
even without JavaScript. On approach to the viewport, the script adds two
presentation copies per column for a seamless 20-pixel-per-second downward loop. Copies are
excluded from the accessibility tree and tab order. Hovering lifts a card without enlarging it and keeps the wall moving.
Keyboard focus on a project pauses motion. Previous/next buttons at the sides
center the adjacent collection and stay synchronized with the category labels. Touch interaction persists a
pause so that users can scroll or select a card without the wall moving.
Pause/Resume is available to mouse, keyboard and touch. Explicit pause persists
across language changes and leaving the wall. Reduced motion disables automatic
scrolling but retains manual access to every card. Offscreen and hidden-tab
motion stops. Category anchors remain valid, including existing deep links.
`homepage-content.json`
records original destinations
bilingual project records and section text hashes to prevent accidental omissions during layout changes;
update it deliberately when changing actual product content.
Run `node test-header.mjs [site URL]`, `node test-homepage.mjs [site URL]`,
`node test-archive.mjs [site URL]` and `node test-home-motion.mjs [site URL]`
for browser regressions. The motion check covers signal lifecycle and route
alignment, including delayed-module first-paint checks. Set `PLAYWRIGHT_MODULE_PATH` if Playwright is not installed locally.
These checks opt out of visitor tracking.
`docs/favicon.svg` and `docs/assets/mark.svg` hold the italic VB mark (the favicon
turns white in dark browser tabs); the PNG icons and social preview
are derived assets and must be regenerated when the mark changes.

## Analytics provenance and identity

The dashboard, styles, map, HTTP routes, SQL backend and regression tests are
adapted directly from `cascode-ai/analog-canvas`, commit
`8ac3e6baf61333952a852f876dc2d895ab0fd778`, directory
`apps/editor/analytics/`. The dashboard retains its layout, light/dark themes,
date filters, PV/UV tables, world heatmap and regional page-load percentiles.
Only the product title/navigation and Canvas-only registered-account query
are removed or changed. The React tracking hook is adapted to a small static
page entry point, so the landing page does not download React.

This is a new, independent deployment, not a migration of Canvas's data:

- Worker: `virtuoso-bridge-site`
- Durable Object binding/class/object: `ANALYTICS` / `AnalyticsDO` / `global`
- Initial migration: `v1`; do not rename these persistence identities.
- First-party cookies: `vb_vid`, `vb_optout`; retired cookie: `vb_sid`
- API: `/api/track`, `/api/stats`, `/api/analytics`, `/api/track/load`,
  `/api/track/opt-out`
- Dashboard: `/analytics/`, noindex and excluded from sitemap.

Visitor hashes and daily counts follow Canvas's retention and attribution
rules. The one-year visitor cookie is not renewed. DNT, GPC and opt-out are
honored. Raw visitor identifiers, IP addresses, referrer paths and queries are
not stored. Page-load histograms retain aggregate timings only. A redeploy
never resets counts. An optional `ANALYTICS_KEY` Worker secret gates the
dashboard API; it is not embedded into static files.

## Deployment

The production URL remains `https://virtuoso-bridge.tokenzhang.com/`.
Cloudflare serves static assets and the analytics Worker. The old GitHub Pages
source remains in `docs/` as a recoverable static fallback; it does not run
the new analytics API. `deploy-site.yml` validates changes on main and deploys
when its credentials are configured; otherwise it emits an explicit warning
without changing production. An authenticated local `npm run deploy` can
publish the same build through the existing Wrangler login.
The required GitHub secrets are `CLOUDFLARE_API_TOKEN` and
`CLOUDFLARE_ACCOUNT_ID`; never commit them.

The former DNS target was the unproxied CNAME `arcadia-1.github.io`. Reverting
the custom domain to that record restores the legacy static hosting path,
but stops the new analytics service. No old Busuanzi counts were imported.
