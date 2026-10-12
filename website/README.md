# Virtuoso Bridge website

The landing page, privacy page and SVG identity live in `../docs/`. The landing
page is a short front door: setup and reference material stay in the repository
README, and `/guide/` only redirects there so old links keep working.
`npm ci && npm run check && npm test && npm run build` builds this isolated
website package. `npm run preview` serves the full site with local Cloudflare
Durable Object storage. It does not connect to Cadence.

Public pages use a light palette, a compact 48px product header and a quiet
footer, aligned with Analog Canvas and Analog Design Bench. The shared chrome
is checked across the landing and privacy pages by `chrome.test.ts`.
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
