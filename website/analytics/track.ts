import { startAnalytics, type VisitStats } from "./client";

let displayed: VisitStats | null = null;
startAnalytics((stats) => {
  if (!Number.isSafeInteger(stats.pv) || !Number.isSafeInteger(stats.uv))
    return;
  // GET and POST may finish out of order; never paint an older all-time total.
  if (displayed && (stats.pv < displayed.pv || stats.uv < displayed.uv)) return;
  displayed = stats;
  const format = new Intl.NumberFormat(document.documentElement.lang);
  document.querySelectorAll("[data-visitors]").forEach((node) => {
    node.textContent = format.format(stats.uv);
  });
  document.querySelectorAll("[data-views]").forEach((node) => {
    node.textContent = format.format(stats.pv);
  });
});
