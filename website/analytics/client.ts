/** Where the privacy notice reads and sets "Stop counting me". */
export const ANALYTICS_OPT_OUT_PATH = "/api/track/opt-out";
/** Where a counted page load reports how long it took (#1581). */
export const ANALYTICS_PAGE_LOAD_PATH = "/api/track/load";

let pageLoadReported = false;

/**
 * Once per page load, report how long it took (#1581): the document's first
 * byte, and when the page was shown (its largest paint, or the load event
 * where a browser has none). A tab that opened in the background is left
 * out, since its timings say nothing about the network.
 */
function reportPageLoad(): void {
  if (pageLoadReported) return;
  pageLoadReported = true;
  if (document.visibilityState === "hidden") return;
  let largestPaint = 0;
  let observer: PerformanceObserver | null = null;
  try {
    observer = new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) largestPaint = entry.startTime;
    });
    observer.observe({ type: "largest-contentful-paint", buffered: true });
  } catch {
    observer = null;
  }
  const send = () => {
    observer?.disconnect();
    const navigation = performance.getEntriesByType("navigation")[0] as
      | PerformanceNavigationTiming
      | undefined;
    if (!navigation) return;
    const shown = largestPaint || navigation.loadEventEnd;
    if (!(navigation.responseStart > 0) || !(shown > 0)) return;
    void fetch(ANALYTICS_PAGE_LOAD_PATH, {
      method: "POST",
      headers: { "content-type": "application/json" },
      credentials: "same-origin",
      keepalive: true,
      cache: "no-store",
      body: JSON.stringify({
        t: Math.round(navigation.responseStart),
        l: Math.round(shown),
      }),
    }).catch(() => {
      // The report is fire-and-forget.
    });
  };
  // The largest paint settles shortly after load.
  const afterLoad = () => window.setTimeout(send, 3000);
  if (document.readyState === "complete") afterLoad();
  else window.addEventListener("load", afterLoad, { once: true });
}

export type VisitStats = { pv: number; uv: number; scope: "all" };

export function startAnalytics(update: (stats: VisitStats) => void): void {
  const host = location.hostname;
  if (
    !(
      host === "virtuoso-bridge.tokenzhang.com" ||
      host.endsWith(".workers.dev") ||
      host === "127.0.0.1" ||
      host === "localhost"
    )
  )
    return;
  const path = location.pathname;
  if (/^\/(analytics|privacy)\/?$/.test(path)) return;
  void fetch("/api/stats", { cache: "no-store" })
    .then(async (response) =>
      response.ok ? (response.json() as Promise<VisitStats>) : null,
    )
    .then((stats) => {
      if (stats) update(stats);
    })
    .catch(() => {});
  if (
    navigator.doNotTrack === "1" ||
    (navigator as Navigator & { globalPrivacyControl?: boolean })
      .globalPrivacyControl === true
  )
    return;
  let referrerOrigin = "";
  try {
    const referrer = new URL(document.referrer);
    if (/^https?:$/.test(referrer.protocol)) referrerOrigin = referrer.origin;
  } catch {}
  const source =
    new URLSearchParams(location.search)
      .get("utm_source")
      ?.trim()
      .toLowerCase()
      .slice(0, 40) ?? "";
  void fetch("/api/track", {
    method: "POST",
    headers: { "content-type": "application/json" },
    credentials: "same-origin",
    keepalive: true,
    cache: "no-store",
    body: JSON.stringify({ p: path, r: referrerOrigin, s: source }),
  })
    .then(async (response) =>
      response.ok && response.status !== 204
        ? (response.json() as Promise<VisitStats>)
        : null,
    )
    .then((stats) => {
      if (stats) update(stats);
    })
    .catch(() => {});
  reportPageLoad();
}
