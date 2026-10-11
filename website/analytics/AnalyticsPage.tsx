import { useEffect, useId, useMemo, useState, type ReactNode } from "react";
import land from "./land-110m.json";
import {
  landPathsForWorld,
  WORLD_BOUNDS,
  type FeatureCollection,
} from "./worldMap";

type DayRow = { date: string; pv: number; uv: number };
type BreakdownRow = { pv?: number; uv?: number; count?: number };
type BreakdownTotal = { pv: number; uv: number };
type Summary = {
  generatedAt: string;
  totals: { pv: number; uv: number };
  today: DayRow;
  days: DayRow[];
  countries: ({ code: string } & BreakdownRow)[];
  points: { lat: number; lng: number; count: number }[];
  paths: ({ path: string } & BreakdownRow)[];
  sources: ({ source: string } & BreakdownRow)[];
  breakdownStartedAt: string;
  breakdownTotals: {
    countries: BreakdownTotal;
    sources: BreakdownTotal;
    pages: BreakdownTotal;
  };
  /** Absent from a Worker older than the page-load table (#1581). */
  loadTimes?: {
    days: number;
    all: LoadTimeRow | null;
    countries: LoadTimeRow[];
  };
};
type LoadTimeRow = {
  code: string;
  n: number;
  ttfbP50: number;
  ttfbP75: number;
  shownP50: number;
  shownP75: number;
};

const DEFAULT_RANGE_DAYS = 90;
const DEFAULT_BREAKDOWN_ROWS = 10;
const MAP_W = 3600;
const MAP_H = Math.round(
  MAP_W *
    ((WORLD_BOUNDS.north - WORLD_BOUNDS.south) /
      (WORLD_BOUNDS.east - WORLD_BOUNDS.west)),
);
const LAND_PATHS = landPathsForWorld(
  land as unknown as FeatureCollection,
  MAP_W,
  MAP_H,
);
const fmt = new Intl.NumberFormat("en");

const SOURCE_LABELS: Record<string, string> = {
  "direct-or-unknown": "Direct / unknown",
  "search:google": "Google Search",
  "search:bing": "Bing Search",
  "search:baidu": "Baidu Search",
  "search:duckduckgo": "DuckDuckGo Search",
  "search:yahoo": "Yahoo Search",
  "search:yandex": "Yandex Search",
  "search:ecosia": "Ecosia Search",
  "search:naver": "Naver Search",
  "search:sogou": "Sogou Search",
  "search:360": "360 Search",
  "social:wechat": "WeChat",
  "social:linkedin": "LinkedIn",
  "social:x": "X / Twitter",
  "social:facebook": "Facebook",
  "social:instagram": "Instagram",
  "social:reddit": "Reddit",
  "social:github": "GitHub",
  "social:zhihu": "Zhihu",
  "social:bilibili": "Bilibili",
  "social:xiaohongshu": "Xiaohongshu",
  "campaign:email": "Email / newsletter",
  "campaign:qr": "QR code",
  "campaign:rss": "RSS",
  "campaign:other": "Other campaign",
  "ref:other": "Other website",
  __other__: "Other sources",
};

function shiftUtcDay(iso: string, delta: number): string {
  const date = new Date(`${iso}T00:00:00.000Z`);
  date.setUTCDate(date.getUTCDate() + delta);
  return date.toISOString().slice(0, 10);
}

function dayCount(from: string, to: string): number {
  const start = Date.parse(`${from}T00:00:00.000Z`);
  const end = Date.parse(`${to}T00:00:00.000Z`);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return 0;
  return Math.round((end - start) / 86_400_000) + 1;
}

function rowMetrics(row: BreakdownRow): BreakdownTotal {
  return { pv: row.pv ?? row.count ?? 0, uv: row.uv ?? 0 };
}

export function AnalyticsPage() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [error, setError] = useState(false);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [theme, setTheme] = useState<"dark" | "light">(() =>
    document.documentElement.classList.contains("light") ? "light" : "dark",
  );

  useEffect(() => {
    const previousTitle = document.title;
    document.title = "Analytics — Virtuoso Bridge";
    document.body.classList.add("analytics-body");
    let robots = document.querySelector<HTMLMetaElement>('meta[name="robots"]');
    const previousRobots = robots?.content;
    if (!robots) {
      robots = document.createElement("meta");
      robots.name = "robots";
      document.head.append(robots);
    }
    robots.content = "noindex, follow";
    return () => {
      document.title = previousTitle;
      document.body.classList.remove("analytics-body");
      if (previousRobots == null) robots?.remove();
      else if (robots) robots.content = previousRobots;
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams(window.location.search);
    const key = params.get("key");
    const url = key
      ? `/api/analytics?key=${encodeURIComponent(key)}`
      : "/api/analytics";
    void fetch(url, {
      headers: { Accept: "application/json" },
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json() as Promise<Summary>;
      })
      .then((data) => {
        setSummary(data);
        if (data.days.length) {
          const end = data.days[data.days.length - 1]!.date;
          const earliest = data.days[0]!.date;
          const start = shiftUtcDay(end, -(DEFAULT_RANGE_DAYS - 1));
          setFrom(start < earliest ? earliest : start);
          setTo(end);
        }
      })
      .catch((reason: unknown) => {
        if (!(reason instanceof DOMException && reason.name === "AbortError"))
          setError(true);
      });
    return () => controller.abort();
  }, []);

  const visibleDays = useMemo(() => {
    if (!summary || !from || !to) return [];
    const start = from < to ? from : to;
    const end = from < to ? to : from;
    return summary.days.filter((day) => day.date >= start && day.date <= end);
  }, [from, summary, to]);

  const rangeDays =
    from && to ? dayCount(from < to ? from : to, from < to ? to : from) : 90;
  const minDate = summary?.days[0]?.date;
  const maxDate = summary?.days[summary.days.length - 1]?.date;

  function resetRange() {
    if (!summary?.days.length) return;
    const end = summary.days[summary.days.length - 1]!.date;
    const earliest = summary.days[0]!.date;
    const start = shiftUtcDay(end, -(DEFAULT_RANGE_DAYS - 1));
    setFrom(start < earliest ? earliest : start);
    setTo(end);
  }

  function toggleTheme() {
    const next = theme === "dark" ? "light" : "dark";
    document.documentElement.classList.toggle("light", next === "light");
    localStorage.setItem("theme", next);
    setTheme(next);
  }

  const generated = summary
    ? new Date(summary.generatedAt).toLocaleString("en", {
        dateStyle: "medium",
        timeStyle: "short",
      })
    : "—";

  return (
    <div className="analytics-shell">
      <header className="analytics-top-bar" aria-label="Page controls">
        <div className="analytics-top-bar-left">
          <button
            type="button"
            className="analytics-theme-switch"
            onClick={toggleTheme}
            aria-label={
              theme === "dark"
                ? "Switch to light theme"
                : "Switch to dark theme"
            }
            title={
              theme === "dark"
                ? "Switch to light theme"
                : "Switch to dark theme"
            }
          >
            {theme === "dark" ? <SunIcon /> : <MoonIcon />}
          </button>
        </div>
        <div className="analytics-top-bar-right">
          <a className="analytics-bug-report" href="https://github.com/Arcadia-1/virtuoso-bridge-lite/issues">Report an issue ↗</a>
          <a className="analytics-home" href="/">
            ← Virtuoso Bridge
          </a>
        </div>
      </header>

      <main className="analytics-page" id="content">
        <div className="analytics-container">
          <header className="analytics-head">
            <h1>Analytics</h1>
            <p className="analytics-sub">
              First-party counts · UTC days · updated{" "}
              <time dateTime={summary?.generatedAt}>{generated}</time>
            </p>
          </header>

          {error && (
            <p className="analytics-error">
              Could not load analytics. If a dashboard key is set, append{" "}
              <code>?key=…</code> to the URL.
            </p>
          )}

          <dl className="analytics-metrics" aria-label="Totals">
            <Metric label="Unique visitors" value={summary?.totals.uv} />
            <Metric label="Page views" value={summary?.totals.pv} />
            <Metric label="Visitors today" value={summary?.today.uv} />
            <Metric label="Views today" value={summary?.today.pv} />
          </dl>

          <section
            className="analytics-section"
            aria-labelledby="analytics-map-h"
          >
            <SectionHead
              title="Origins"
              id="analytics-map-h"
              aside="Map"
              meta={<p className="analytics-note">≈1° request buckets</p>}
            />
            <WorldHeatmap points={summary?.points ?? []} />
          </section>

          <section
            className="analytics-section"
            aria-labelledby="analytics-chart-h"
          >
            <SectionHead
              title="Daily traffic"
              id="analytics-chart-h"
              meta={
                <>
                  <ul className="analytics-legend" aria-hidden="true">
                    <li>
                      <span className="analytics-swatch analytics-swatch--pv" />
                      Views
                    </li>
                    <li>
                      <span className="analytics-swatch analytics-swatch--uv" />
                      Visitors
                    </li>
                  </ul>
                  <span className="analytics-aside" aria-hidden="true">
                    {rangeDays === 1 ? "1 day" : `${rangeDays} days`}
                  </span>
                </>
              }
            />
            <form
              className="analytics-range"
              onSubmit={(event) => event.preventDefault()}
            >
              <label className="analytics-range-field">
                <span>From</span>
                <input
                  className="analytics-range-input"
                  type="date"
                  name="from"
                  value={from}
                  min={minDate}
                  max={maxDate}
                  onChange={(event) => setFrom(event.target.value)}
                  required
                />
              </label>
              <label className="analytics-range-field">
                <span>To</span>
                <input
                  className="analytics-range-input"
                  type="date"
                  name="to"
                  value={to}
                  min={minDate}
                  max={maxDate}
                  onChange={(event) => setTo(event.target.value)}
                  required
                />
              </label>
              <button
                type="button"
                className="analytics-range-reset"
                onClick={resetRange}
              >
                Last 90 days
              </button>
              <span className="analytics-range-hint">UTC</span>
            </form>
            <DailyChart days={visibleDays} />
          </section>

          <div className="analytics-cols">
            <section
              className="analytics-section"
              aria-labelledby="analytics-countries-h"
            >
              <SectionHead
                title="ISO 3166 Code"
                id="analytics-countries-h"
                aside="Top"
              />
              <BreakdownTable
                heading="Region"
                rows={summary?.countries ?? []}
                total={summary?.breakdownTotals.countries ?? { pv: 0, uv: 0 }}
                label={(row) =>
                  countryName((row as Summary["countries"][number]).code)
                }
                loading={!summary && !error}
                unavailable={error}
              />
            </section>
            <section
              className="analytics-section"
              aria-labelledby="analytics-sources-h"
            >
              <SectionHead
                title="Sources"
                id="analytics-sources-h"
                aside="Top"
              />
              <BreakdownTable
                heading="Source"
                rows={summary?.sources ?? []}
                total={summary?.breakdownTotals.sources ?? { pv: 0, uv: 0 }}
                label={(row) =>
                  sourceName((row as Summary["sources"][number]).source)
                }
                loading={!summary && !error}
                unavailable={error}
              />
            </section>
            <section
              className="analytics-section analytics-section--wide"
              aria-labelledby="analytics-pages-h"
            >
              <SectionHead title="Pages" id="analytics-pages-h" aside="Top" />
              <BreakdownTable
                heading="Path"
                rows={summary?.paths ?? []}
                total={summary?.breakdownTotals.pages ?? { pv: 0, uv: 0 }}
                label={(row) => {
                  const path = (row as Summary["paths"][number]).path;
                  return path === "__other__" ? "Other pages" : path;
                }}
                mono
                loading={!summary && !error}
                unavailable={error}
              />
            </section>
            <section
              className="analytics-section analytics-section--wide"
              aria-labelledby="analytics-load-h"
            >
              <SectionHead
                title="Page load"
                id="analytics-load-h"
                aside={`Last ${summary?.loadTimes?.days ?? 30} days`}
              />
              <LoadTimeTable
                loadTimes={summary?.loadTimes}
                loading={!summary && !error}
                unavailable={error}
              />
            </section>
          </div>
        </div>
      </main>
    </div>
  );
}

/** A histogram bound in ms, as a reader takes it in: "≤ 0.3 s". */
function formatLoadTime(ms: number): string {
  if (ms <= 0) return "—";
  const seconds = ms / 1000;
  return `≤ ${seconds.toLocaleString("en-US", { maximumFractionDigits: 2 })} s`;
}

/**
 * How long counted page loads took, by region (#1581): the document's first
 * byte, and when the page was shown, as median and 75th percentile.
 */
function LoadTimeTable({
  loadTimes,
  loading,
  unavailable,
}: {
  loadTimes: Summary["loadTimes"];
  loading: boolean;
  unavailable: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const rows = loadTimes?.countries ?? [];
  const visibleRows = expanded ? rows : rows.slice(0, DEFAULT_BREAKDOWN_ROWS);
  const cells = (row: LoadTimeRow, label: string, strong = false) => (
    <tr key={label} className={strong ? "analytics-load-all" : undefined}>
      <td>{label}</td>
      <td className="num">{fmt.format(row.n)}</td>
      <td className="num">
        {formatLoadTime(row.ttfbP50)} · {formatLoadTime(row.ttfbP75)}
      </td>
      <td className="num">
        {formatLoadTime(row.shownP50)} · {formatLoadTime(row.shownP75)}
      </td>
    </tr>
  );
  return (
    <div className="analytics-breakdown">
      <div className="analytics-table-scroll">
        <table className="analytics-table" data-testid="analytics-load-table">
          <thead>
            <tr>
              <th scope="col">Region</th>
              <th scope="col" className="num">
                Page loads
              </th>
              <th scope="col" className="num">
                First byte (median · 75%)
              </th>
              <th scope="col" className="num">
                Page shown (median · 75%)
              </th>
            </tr>
          </thead>
          <tbody>
            {loading || unavailable || !loadTimes?.all ? (
              <tr>
                <td colSpan={4} className="analytics-empty">
                  {loading
                    ? "Loading…"
                    : unavailable
                      ? "Unavailable."
                      : "No data yet."}
                </td>
              </tr>
            ) : (
              <>
                {cells(loadTimes.all, "All regions", true)}
                {visibleRows.map((row) => cells(row, countryName(row.code)))}
              </>
            )}
          </tbody>
        </table>
      </div>
      {rows.length > DEFAULT_BREAKDOWN_ROWS ? (
        <button
          type="button"
          className="analytics-table-toggle"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          <span>
            {expanded
              ? `Show top ${DEFAULT_BREAKDOWN_ROWS}`
              : `Show all ${rows.length}`}
          </span>
          <ChevronIcon expanded={expanded} />
        </button>
      ) : null}
    </div>
  );
}

function Metric({
  label,
  value,
}: {
  label: string;
  value: number | undefined;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value == null ? "—" : fmt.format(value)}</dd>
    </div>
  );
}

function SectionHead({
  title,
  id,
  aside,
  meta,
}: {
  title: string;
  id: string;
  aside?: string;
  meta?: ReactNode;
}) {
  return (
    <div className="analytics-section-head">
      <h2 id={id}>{title}</h2>
      <div className="analytics-section-head-meta">
        {meta}
        {aside && (
          <span className="analytics-aside" aria-hidden="true">
            {aside}
          </span>
        )}
      </div>
    </div>
  );
}

function DailyChart({ days }: { days: DayRow[] }) {
  const width = 900;
  const height = 220;
  const padTop = 16;
  const padBottom = 22;
  const padX = 2;
  const plotHeight = height - padTop - padBottom;

  if (!days.length) {
    return (
      <svg
        className="analytics-chart"
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label="Daily page views and unique visitors"
      >
        <text
          className="analytics-chart-tick"
          x={width / 2}
          y={height / 2}
          textAnchor="middle"
        >
          No days in this range
        </text>
      </svg>
    );
  }

  const max = Math.max(1, ...days.map((day) => day.pv));
  const slot = (width - padX * 2) / days.length;
  const ticks =
    days.length === 1
      ? [{ index: 0, x: width / 2, anchor: "middle" as const }]
      : days.length === 2
        ? [
            { index: 0, x: 0, anchor: "start" as const },
            { index: 1, x: width, anchor: "end" as const },
          ]
        : [
            { index: 0, x: 0, anchor: "start" as const },
            {
              index: Math.floor(days.length / 2),
              x: width / 2,
              anchor: "middle" as const,
            },
            { index: days.length - 1, x: width, anchor: "end" as const },
          ];

  return (
    <svg
      className="analytics-chart"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label="Daily page views and unique visitors"
    >
      {days.map((day, index) => {
        const pvHeight = (day.pv / max) * plotHeight;
        const uvHeight = (day.uv / max) * plotHeight;
        const x = padX + index * slot;
        const title = `${day.date}: ${fmt.format(day.pv)} views, ${fmt.format(day.uv)} visitors`;
        return (
          <g key={day.date}>
            <rect
              className="analytics-chart-bar-pv"
              x={x + slot * 0.18}
              y={height - padBottom - pvHeight}
              width={slot * 0.64}
              height={Math.max(pvHeight, day.pv > 0 ? 1.25 : 0)}
            >
              <title>{title}</title>
            </rect>
            {day.uv > 0 && (
              <rect
                className="analytics-chart-bar-uv"
                x={x + slot * 0.34}
                y={height - padBottom - uvHeight}
                width={slot * 0.32}
                height={Math.max(uvHeight, 1.25)}
              >
                <title>{title}</title>
              </rect>
            )}
          </g>
        );
      })}
      <line
        className="analytics-chart-axis"
        x1="0"
        x2={width}
        y1={height - padBottom}
        y2={height - padBottom}
      />
      <text className="analytics-chart-tick" x="0" y={padTop - 2}>
        {fmt.format(max)}
      </text>
      {ticks.map((tick) => (
        <text
          key={`${tick.index}-${tick.anchor}`}
          className="analytics-chart-tick"
          x={tick.x}
          y={height - 4}
          textAnchor={tick.anchor}
        >
          {days[tick.index]!.date.slice(5)}
        </text>
      ))}
    </svg>
  );
}

function WorldHeatmap({ points }: { points: Summary["points"] }) {
  const maxCount = Math.max(1, ...points.map((point) => point.count));
  return (
    <div className="analytics-map-frame">
      <svg
        className="analytics-map-svg"
        viewBox={`0 0 ${MAP_W} ${MAP_H}`}
        role="img"
        aria-label="World heatmap of request origins"
      >
        <defs>
          <radialGradient id="analytics-heat-gradient">
            <stop offset="0%" stopOpacity="0.85" />
            <stop offset="55%" stopOpacity="0.35" />
            <stop offset="100%" stopOpacity="0" />
          </radialGradient>
        </defs>
        <rect
          className="analytics-map-ocean"
          x="0"
          y="0"
          width={MAP_W}
          height={MAP_H}
        />
        <g className="analytics-map-land" aria-hidden="true">
          {LAND_PATHS.map((path, index) => (
            <path d={path} key={index} />
          ))}
        </g>
        <g className="analytics-map-heat">
          {points.map((point) => {
            const cx = (point.lng + 180) * 10;
            const cy = (84 - point.lat) * 10;
            const radius = 18 + 64 * Math.sqrt(point.count / maxCount);
            const title = `${fmt.format(point.count)} views near ${point.lat}, ${point.lng}`;
            return (
              <g key={`${point.lat},${point.lng}`}>
                <circle
                  className="analytics-heat-blob"
                  cx={cx}
                  cy={cy}
                  r={radius}
                >
                  <title>{title}</title>
                </circle>
                <circle
                  className="analytics-heat-core"
                  cx={cx}
                  cy={cy}
                  r={Math.max(2.8, radius * 0.09)}
                >
                  <title>{title}</title>
                </circle>
              </g>
            );
          })}
        </g>
      </svg>
    </div>
  );
}

function BreakdownTable<T extends BreakdownRow>({
  heading,
  rows,
  total,
  label,
  mono = false,
  loading,
  unavailable,
}: {
  heading: string;
  rows: T[];
  total: BreakdownTotal;
  label: (row: T) => string;
  mono?: boolean;
  loading: boolean;
  unavailable: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const tableId = useId();
  const canExpand = rows.length > DEFAULT_BREAKDOWN_ROWS;
  const visibleRows = expanded ? rows : rows.slice(0, DEFAULT_BREAKDOWN_ROWS);

  return (
    <div className="analytics-breakdown">
      <div className="analytics-table-scroll">
        <table className="analytics-table" id={tableId}>
          <thead>
            <tr>
              <th scope="col">{heading}</th>
              <th scope="col" className="num">
                Unique Visitors
              </th>
              <th scope="col" className="num">
                Page Views
              </th>
            </tr>
          </thead>
          <tbody>
            {loading || unavailable || rows.length === 0 ? (
              <tr>
                <td colSpan={3} className="analytics-empty">
                  {loading
                    ? "Loading…"
                    : unavailable
                      ? "Unavailable."
                      : "No data yet."}
                </td>
              </tr>
            ) : (
              visibleRows.map((row, index) => {
                const metric = rowMetrics(row);
                return (
                  <tr key={`${label(row)}-${index}`}>
                    <td className={mono ? "mono" : undefined}>{label(row)}</td>
                    <MetricCell value={metric.uv} total={total.uv} />
                    <MetricCell value={metric.pv} total={total.pv} />
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
      {canExpand && (
        <button
          type="button"
          className="analytics-table-toggle"
          aria-controls={tableId}
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          <span>
            {expanded
              ? `Show top ${DEFAULT_BREAKDOWN_ROWS}`
              : `Show all ${rows.length}`}
          </span>
          <ChevronIcon expanded={expanded} />
        </button>
      )}
    </div>
  );
}

function MetricCell({ value, total }: { value: number; total: number }) {
  const percentage = total > 0 ? (value / total) * 100 : 0;
  return (
    <td className="num analytics-metric">
      <span>{fmt.format(value)}</span>
      <span className="analytics-metric-distribution">
        {percentage.toFixed(1)}%
      </span>
    </td>
  );
}

function countryName(code: string): string {
  if (code === "__other__") return "Other regions";
  if (code === "T1") return "Tor network";
  if (code === "XX") return "Unknown";
  try {
    return new Intl.DisplayNames(["en"], { type: "region" }).of(code) ?? code;
  } catch {
    return code;
  }
}

function ChevronIcon({ expanded }: { expanded: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={expanded ? "is-expanded" : undefined}
    >
      <path d="m7 10 5 5 5-5" />
    </svg>
  );
}

function sourceName(source: string): string {
  return (
    SOURCE_LABELS[source] ??
    (source.startsWith("ref:") ? source.slice(4) : source)
  );
}

function MoonIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M21 14.3A8.5 8.5 0 0 1 9.7 3a7 7 0 1 0 11.3 11.3Z" />
    </svg>
  );
}

function SunIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
    </svg>
  );
}
