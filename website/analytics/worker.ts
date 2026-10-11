/**
 * These values locate the already-deployed analytics namespace and identify
 * returning browsers. Changing one starts a separate data or visitor history.
 */
export const ANALYTICS_PERSISTENCE_IDENTITY = {
  binding: "ANALYTICS",
  durableObjectClass: "AnalyticsDO",
  objectName: "global",
  visitorCookie: "vb_vid",
  routes: ["/api/track", "/api/stats", "/api/analytics"],
} as const;

/** Remembers that a browser asked not to be counted. */
export const ANALYTICS_OPT_OUT_COOKIE = "vb_optout";

/**
 * The 30-minute source cookie the analytics once set. A visit's source now
 * stays with the day's visit on the server, and a request that still
 * carries the cookie gets it back expired.
 */
export const RETIRED_ANALYTICS_COOKIES = ["vb_sid"] as const;

const VISITOR_COOKIE = ANALYTICS_PERSISTENCE_IDENTITY.visitorCookie;
/**
 * One year from the first visit and never renewed: within the 13 months
 * audience-measurement rules allow a counting cookie.
 */
export const VISITOR_COOKIE_MAX_AGE = 60 * 60 * 24 * 365;
/** Thirteen months, the longest those rules allow. */
export const OPT_OUT_COOKIE_MAX_AGE = 60 * 60 * 24 * 396;
/**
 * A visitor id unseen this long can never come back: its cookie, never
 * renewed, has expired. It is deleted, and the total keeps counting it.
 */
const VISITOR_ID_RETENTION_DAYS = 366;
const DAY_MS = 24 * 60 * 60 * 1000;
const RETAINED_DAYS = 400;
const DURABLE_OBJECT_NAME = ANALYTICS_PERSISTENCE_IDENTITY.objectName;
const OTHER_KEY = "__other__";
const MAX_BREAKDOWN_ROWS = 256;
const MAX_POINT_ROWS = 2000;

/**
 * Upper bounds, in milliseconds, of the page-load histogram (#1581): fine
 * where pages usually load, coarse in the tail; the last bucket is open.
 * Only counts per day, country and bucket are kept, never a visitor.
 */
export const LOAD_TIME_BUCKETS_MS = [
  100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1250, 1500, 1750, 2000,
  2500, 3000, 3500, 4000, 5000, 6000, 8000, 10000, 15000, 20000, 30000, 60000,
] as const;
/** The window the page-load table summarizes. */
export const LOAD_TIME_DAYS = 30;
/** The longest time a page-load report may carry. */
export const MAX_LOAD_TIME_MS = 600_000;

const BREAKDOWN_TABLES = {
  countries: "analytics_countries",
  sources: "analytics_sources",
  pages: "analytics_pages",
} as const;

type SqlResult<T> = {
  one(): T;
  toArray(): T[];
};

type SqlStorage = {
  exec<T>(query: string, ...bindings: unknown[]): SqlResult<T>;
};

type DurableObjectStateLike = {
  storage: {
    sql: SqlStorage;
    transactionSync<T>(callback: () => T): T;
  };
};

export type DurableObjectNamespaceLike = {
  getByName(name: string): {
    fetch(input: string, init?: RequestInit): Promise<Response>;
  };
};

export type VisitStats = { pv: number; uv: number };

export type PageViewEvent = {
  visitorHash: string;
  path: string;
  country: string;
  lat: number | null;
  lng: number | null;
  source: string;
};

/** One page load: when its first byte arrived, and when it was shown. */
export type PageLoadEvent = {
  country: string;
  /** Navigation start to the document's first byte, in ms. */
  ttfb: number;
  /** Navigation start to the largest paint (or the load event), in ms. */
  shown: number;
};

/** Medians and 75th percentiles, as histogram bucket upper bounds in ms. */
export type LoadTimeRow = {
  code: string;
  n: number;
  ttfbP50: number;
  ttfbP75: number;
  shownP50: number;
  shownP75: number;
};

export type AnalyticsSummary = {
  generatedAt: string;
  totals: VisitStats;
  today: { date: string; pv: number; uv: number };
  days: { date: string; pv: number; uv: number }[];
  countries: { code: string; pv: number; uv: number }[];
  points: { lat: number; lng: number; count: number }[];
  paths: { path: string; pv: number; uv: number }[];
  sources: { source: string; pv: number; uv: number }[];
  breakdownStartedAt: string;
  breakdownTotals: {
    countries: VisitStats;
    sources: VisitStats;
    pages: VisitStats;
  };
  /** Page loads over the last LOAD_TIME_DAYS days (#1581). */
  loadTimes: {
    days: number;
    all: LoadTimeRow | null;
    countries: LoadTimeRow[];
  };
};

type BreakdownRow = {
  dimension_key: string;
  pv: number;
  uv: number;
};

type DailyViewsRow = { day: number; views: number };
type DailyVisitorsRow = { day: number; visitors: number };
type PointRow = { lat: number; lng: number; count: number };

const META = {
  /** Every visitor ever counted; deleting a dead id leaves it unchanged. */
  visitorTotal: "visitor_total",
  /** The day finished days were last turned into counts. */
  rolledUpDay: "rolled_up_day",
} as const;

export class AnalyticsDO {
  private readonly sql: SqlStorage;

  constructor(private readonly state: DurableObjectStateLike) {
    this.sql = state.storage.sql;
    this.initializeSchema();
  }

  private initializeSchema(): void {
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS visitors (
        visitor_hash TEXT PRIMARY KEY,
        last_seen_day INTEGER NOT NULL
      ) WITHOUT ROWID
    `);
    this.sql.exec(`
      CREATE INDEX IF NOT EXISTS idx_visitors_last_seen_day
      ON visitors(last_seen_day)
    `);
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS daily_views (
        day INTEGER PRIMARY KEY,
        views INTEGER NOT NULL
      )
    `);
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS daily_visitors (
        day INTEGER NOT NULL,
        visitor_hash TEXT NOT NULL,
        PRIMARY KEY (day, visitor_hash)
      ) WITHOUT ROWID
    `);
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS analytics_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
      ) WITHOUT ROWID
    `);
    for (const table of Object.values(BREAKDOWN_TABLES)) {
      this.sql.exec(`
        CREATE TABLE IF NOT EXISTS ${table} (
          dimension_key TEXT PRIMARY KEY,
          pv INTEGER NOT NULL,
          uv INTEGER NOT NULL
        ) WITHOUT ROWID
      `);
    }
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS analytics_points (
        lat INTEGER NOT NULL,
        lng INTEGER NOT NULL,
        count INTEGER NOT NULL,
        PRIMARY KEY (lat, lng)
      ) WITHOUT ROWID
    `);
    this.sql.exec(
      "INSERT OR IGNORE INTO analytics_meta(key, value) VALUES ('breakdown_started_at', ?)",
      new Date().toISOString(),
    );
    // Finished days keep only their visitor count; their hashes go.
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS daily_visitor_counts (
        day INTEGER PRIMARY KEY,
        visitors INTEGER NOT NULL
      )
    `);
    try {
      // Additive: each visitor's first source of the day.
      this.sql.exec("ALTER TABLE daily_visitors ADD COLUMN source TEXT");
    } catch {
      // Column already present.
    }
    // Page-load histogram (#1581): counts per day, country, metric, bucket.
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS load_times (
        day INTEGER NOT NULL,
        country TEXT NOT NULL,
        metric TEXT NOT NULL,
        bucket INTEGER NOT NULL,
        count INTEGER NOT NULL,
        PRIMARY KEY (day, country, metric, bucket)
      ) WITHOUT ROWID
    `);
    // The total was the size of `visitors`. Dead ids are now deleted from
    // it, so the total becomes a counter, starting from that size once.
    this.sql.exec(
      `INSERT OR IGNORE INTO analytics_meta(key, value)
       SELECT ?, CAST(COUNT(*) AS TEXT) FROM visitors`,
      META.visitorTotal,
    );
  }

  /**
   * Once a day, on its first request: turn the finished days' hashes into
   * counts and delete the ids whose cookies have expired.
   */
  private rollUpFinishedDays(today: number): void {
    if (this.meta(META.rolledUpDay) === String(today)) return;
    this.state.storage.transactionSync(() => {
      this.sql.exec(
        `INSERT INTO daily_visitor_counts(day, visitors)
         SELECT day, COUNT(*) FROM daily_visitors WHERE day < ? GROUP BY day
         ON CONFLICT(day) DO UPDATE SET visitors = visitors + excluded.visitors`,
        today,
      );
      this.sql.exec("DELETE FROM daily_visitors WHERE day < ?", today);
      this.sql.exec(
        "DELETE FROM visitors WHERE last_seen_day < ?",
        today - VISITOR_ID_RETENTION_DAYS,
      );
      this.sql.exec(
        "DELETE FROM load_times WHERE day < ?",
        today - RETAINED_DAYS,
      );
      this.setMeta(META.rolledUpDay, String(today));
    });
  }

  private meta(key: string): string | null {
    return (
      this.sql
        .exec<{
          value: string;
        }>("SELECT value FROM analytics_meta WHERE key = ?", key)
        .toArray()[0]?.value ?? null
    );
  }

  private setMeta(key: string, value: string): void {
    this.sql.exec(
      `INSERT INTO analytics_meta(key, value) VALUES (?, ?)
       ON CONFLICT(key) DO UPDATE SET value = excluded.value`,
      key,
      value,
    );
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    // The day's first request, counted or not, retires the finished days.
    this.rollUpFinishedDays(utcDay(Date.now()));
    if (request.method === "POST" && url.pathname === "/hit") {
      const event = (await request
        .json()
        .catch(() => null)) as PageViewEvent | null;
      if (!isValidEvent(event)) {
        return Response.json(
          { error: "Invalid analytics event" },
          { status: 400 },
        );
      }
      this.recordHit(event, Date.now());
      return Response.json({ ...this.readStats(), scope: "all" });
    }
    if (request.method === "POST" && url.pathname === "/load") {
      const event = (await request
        .json()
        .catch(() => null)) as PageLoadEvent | null;
      if (!isValidLoadEvent(event)) {
        return Response.json(
          { error: "Invalid page-load event" },
          { status: 400 },
        );
      }
      this.recordLoad(event, Date.now());
      return new Response(null, { status: 204 });
    }
    if (request.method === "GET" && url.pathname === "/stats") {
      return Response.json({ ...this.readStats(), scope: "all" });
    }
    if (request.method === "GET" && url.pathname === "/analytics") {
      return Response.json(this.readAnalyticsSummary());
    }
    return Response.json({ error: "Not found" }, { status: 404 });
  }

  private recordHit(event: PageViewEvent, now: number): void {
    const today = utcDay(now);
    this.state.storage.transactionSync(() => {
      const knownVisitor = this.sql
        .exec<{ visitor_hash: string }>(
          "SELECT visitor_hash FROM visitors WHERE visitor_hash = ?",
          event.visitorHash,
        )
        .toArray()[0];
      const uvDelta = knownVisitor ? 0 : 1;
      const seenToday = this.sql
        .exec<{ source: string | null }>(
          "SELECT source FROM daily_visitors WHERE day = ? AND visitor_hash = ?",
          today,
          event.visitorHash,
        )
        .toArray()[0];
      // A visitor keeps the day's first source, as the retired session
      // cookie kept it for half an hour.
      const source = seenToday?.source ?? event.source;

      this.sql.exec(
        `INSERT INTO daily_views(day, views) VALUES (?, 1)
         ON CONFLICT(day) DO UPDATE SET views = views + 1`,
        today,
      );
      if (!seenToday)
        this.sql.exec(
          "INSERT INTO daily_visitors(day, visitor_hash, source) VALUES (?, ?, ?)",
          today,
          event.visitorHash,
          event.source,
        );
      this.sql.exec(
        `INSERT INTO visitors(visitor_hash, last_seen_day) VALUES (?, ?)
         ON CONFLICT(visitor_hash) DO UPDATE SET last_seen_day = excluded.last_seen_day
         WHERE visitors.last_seen_day < excluded.last_seen_day`,
        event.visitorHash,
        today,
      );
      if (!knownVisitor)
        this.sql.exec(
          `UPDATE analytics_meta SET value = CAST(value AS INTEGER) + 1
           WHERE key = ?`,
          META.visitorTotal,
        );

      this.upsertBreakdown(BREAKDOWN_TABLES.countries, event.country, uvDelta);
      this.upsertBreakdown(BREAKDOWN_TABLES.sources, source, uvDelta);
      this.upsertBreakdown(BREAKDOWN_TABLES.pages, event.path, uvDelta);

      if (event.lat != null && event.lng != null) {
        this.sql.exec(
          `INSERT INTO analytics_points(lat, lng, count) VALUES (?, ?, 1)
           ON CONFLICT(lat, lng) DO UPDATE SET count = count + 1`,
          Math.round(event.lat),
          Math.round(event.lng),
        );
        this.trimPoints();
      }
    });
  }

  private upsertBreakdown(table: string, key: string, uvDelta: number): void {
    this.sql.exec(
      `INSERT INTO ${table}(dimension_key, pv, uv) VALUES (?, 1, ?)
       ON CONFLICT(dimension_key) DO UPDATE SET
         pv = pv + 1,
         uv = uv + excluded.uv`,
      key,
      uvDelta,
    );
    this.trimBreakdown(table);
  }

  private trimBreakdown(table: string): void {
    const count = Number(
      this.sql
        .exec<{ count: number }>(`SELECT COUNT(*) AS count FROM ${table}`)
        .one().count,
    );
    const excess = count - MAX_BREAKDOWN_ROWS;
    if (excess <= 0) return;
    const rows = this.sql
      .exec<BreakdownRow>(
        `SELECT dimension_key, pv, uv FROM ${table}
         WHERE dimension_key != ? ORDER BY pv ASC, uv ASC LIMIT ?`,
        OTHER_KEY,
        excess,
      )
      .toArray();
    for (const row of rows) {
      this.sql.exec(
        `DELETE FROM ${table} WHERE dimension_key = ?`,
        row.dimension_key,
      );
      this.sql.exec(
        `INSERT INTO ${table}(dimension_key, pv, uv) VALUES (?, ?, ?)
         ON CONFLICT(dimension_key) DO UPDATE SET
           pv = pv + excluded.pv,
           uv = uv + excluded.uv`,
        OTHER_KEY,
        Number(row.pv),
        Number(row.uv),
      );
    }
  }

  private trimPoints(): void {
    const count = Number(
      this.sql
        .exec<{ count: number }>(
          "SELECT COUNT(*) AS count FROM analytics_points",
        )
        .one().count,
    );
    const excess = count - MAX_POINT_ROWS;
    if (excess <= 0) return;
    const rows = this.sql
      .exec<PointRow>(
        "SELECT lat, lng, count FROM analytics_points ORDER BY count ASC LIMIT ?",
        excess,
      )
      .toArray();
    for (const row of rows) {
      this.sql.exec(
        "DELETE FROM analytics_points WHERE lat = ? AND lng = ?",
        row.lat,
        row.lng,
      );
    }
  }

  private recordLoad(event: PageLoadEvent, now: number): void {
    const day = utcDay(now);
    this.state.storage.transactionSync(() => {
      for (const [metric, ms] of [
        ["ttfb", event.ttfb],
        ["shown", event.shown],
      ] as const) {
        this.sql.exec(
          `INSERT INTO load_times(day, country, metric, bucket, count)
           VALUES (?, ?, ?, ?, 1)
           ON CONFLICT(day, country, metric, bucket)
           DO UPDATE SET count = count + 1`,
          day,
          event.country,
          metric,
          loadTimeBucket(ms),
        );
      }
    });
  }

  private readLoadTimes(today: number): AnalyticsSummary["loadTimes"] {
    const rows = this.sql
      .exec<{ country: string; metric: string; bucket: number; count: number }>(
        `SELECT country, metric, bucket, SUM(count) AS count FROM load_times
         WHERE day > ? GROUP BY country, metric, bucket`,
        today - LOAD_TIME_DAYS,
      )
      .toArray();
    const histograms = new Map<string, { ttfb: number[]; shown: number[] }>();
    const histogram = (code: string) => {
      let entry = histograms.get(code);
      if (!entry) {
        const empty = () => new Array(LOAD_TIME_BUCKETS_MS.length + 1).fill(0);
        entry = { ttfb: empty(), shown: empty() };
        histograms.set(code, entry);
      }
      return entry;
    };
    for (const row of rows) {
      if (row.metric !== "ttfb" && row.metric !== "shown") continue;
      const bucket = Number(row.bucket);
      if (!(bucket >= 0 && bucket <= LOAD_TIME_BUCKETS_MS.length)) continue;
      const count = Number(row.count);
      for (const code of [row.country, "ALL"]) {
        const counts = histogram(code)[row.metric];
        counts[bucket] = (counts[bucket] ?? 0) + count;
      }
    }
    const summarize = (code: string): LoadTimeRow => {
      const { ttfb, shown } = histogram(code);
      return {
        code,
        n: ttfb.reduce((sum, count) => sum + count, 0),
        ttfbP50: loadTimePercentile(ttfb, 0.5),
        ttfbP75: loadTimePercentile(ttfb, 0.75),
        shownP50: loadTimePercentile(shown, 0.5),
        shownP75: loadTimePercentile(shown, 0.75),
      };
    };
    const countries = [...histograms.keys()]
      .filter((code) => code !== "ALL")
      .map(summarize)
      .sort(
        (left, right) =>
          right.n - left.n || left.code.localeCompare(right.code),
      );
    return {
      days: LOAD_TIME_DAYS,
      all: histograms.has("ALL") ? summarize("ALL") : null,
      countries,
    };
  }

  private readStats(): VisitStats {
    const pv = this.sql
      .exec<{ pv: number }>(
        "SELECT COALESCE(SUM(views), 0) AS pv FROM daily_views",
      )
      .one();
    return { pv: Number(pv.pv), uv: Number(this.meta(META.visitorTotal)) };
  }

  private readAnalyticsSummary(): AnalyticsSummary {
    const today = utcDay(Date.now());
    const firstDay = today - (RETAINED_DAYS - 1);
    const views = this.sql
      .exec<DailyViewsRow>(
        "SELECT day, views FROM daily_views WHERE day >= ? ORDER BY day",
        firstDay,
      )
      .toArray();
    // Finished days are counts; today still has its hashes.
    const visitors = this.sql
      .exec<DailyVisitorsRow>(
        `SELECT day, SUM(visitors) AS visitors FROM (
           SELECT day, visitors FROM daily_visitor_counts WHERE day >= ?
           UNION ALL
           SELECT day, COUNT(*) AS visitors FROM daily_visitors
           WHERE day >= ? GROUP BY day
         ) GROUP BY day ORDER BY day`,
        firstDay,
        firstDay,
      )
      .toArray();
    const viewsByDay = new Map(
      views.map((row) => [Number(row.day), Number(row.views)]),
    );
    const visitorsByDay = new Map(
      visitors.map((row) => [Number(row.day), Number(row.visitors)]),
    );
    const days = Array.from({ length: RETAINED_DAYS }, (_, index) => {
      const day = firstDay + index;
      return {
        date: utcDate(day),
        pv: viewsByDay.get(day) ?? 0,
        uv: visitorsByDay.get(day) ?? 0,
      };
    });

    const countries = this.readBreakdown(BREAKDOWN_TABLES.countries).map(
      (row) => ({
        code: row.dimension_key,
        pv: Number(row.pv),
        uv: Number(row.uv),
      }),
    );
    const sources = this.readBreakdown(BREAKDOWN_TABLES.sources).map((row) => ({
      source: row.dimension_key,
      pv: Number(row.pv),
      uv: Number(row.uv),
    }));
    const paths = this.readBreakdown(BREAKDOWN_TABLES.pages).map((row) => ({
      path: row.dimension_key,
      pv: Number(row.pv),
      uv: Number(row.uv),
    }));
    const points = this.sql
      .exec<PointRow>(
        "SELECT lat, lng, count FROM analytics_points ORDER BY count DESC LIMIT ?",
        MAX_POINT_ROWS,
      )
      .toArray()
      .map((row) => ({
        lat: Number(row.lat),
        lng: Number(row.lng),
        count: Number(row.count),
      }));

    return {
      generatedAt: new Date().toISOString(),
      totals: this.readStats(),
      today: days.at(-1) ?? { date: utcDate(today), pv: 0, uv: 0 },
      days,
      countries,
      points,
      paths,
      sources,
      breakdownStartedAt: this.sql
        .exec<{ value: string }>(
          "SELECT value FROM analytics_meta WHERE key = 'breakdown_started_at'",
        )
        .one().value,
      breakdownTotals: {
        countries: this.breakdownTotal(BREAKDOWN_TABLES.countries),
        sources: this.breakdownTotal(BREAKDOWN_TABLES.sources),
        pages: this.breakdownTotal(BREAKDOWN_TABLES.pages),
      },
      loadTimes: this.readLoadTimes(today),
    };
  }

  private readBreakdown(table: string): BreakdownRow[] {
    const rows = this.sql
      .exec<BreakdownRow>(
        `SELECT dimension_key, pv, uv FROM ${table} ORDER BY pv DESC, uv DESC`,
      )
      .toArray();
    const other = rows.find((row) => row.dimension_key === OTHER_KEY);
    const regular = rows.filter((row) => row.dimension_key !== OTHER_KEY);
    return other ? [...regular, other] : regular;
  }

  private breakdownTotal(table: string): VisitStats {
    const row = this.sql
      .exec<VisitStats>(
        `SELECT COALESCE(SUM(pv), 0) AS pv, COALESCE(SUM(uv), 0) AS uv FROM ${table}`,
      )
      .one();
    return { pv: Number(row.pv), uv: Number(row.uv) };
  }
}

/** The histogram bucket a time falls in; past the last bound, the open one. */
export function loadTimeBucket(ms: number): number {
  const index = LOAD_TIME_BUCKETS_MS.findIndex((bound) => ms <= bound);
  return index === -1 ? LOAD_TIME_BUCKETS_MS.length : index;
}

/**
 * A percentile of a histogram, as the upper bound of the bucket it falls in
 * (the open bucket answers its lower bound). 0 when there is nothing.
 */
export function loadTimePercentile(
  counts: readonly number[],
  quantile: number,
): number {
  const total = counts.reduce((sum, count) => sum + count, 0);
  if (total === 0) return 0;
  const target = Math.ceil(total * quantile);
  let seen = 0;
  for (const [bucket, count] of counts.entries()) {
    seen += count;
    if (seen >= target)
      return LOAD_TIME_BUCKETS_MS[bucket] ?? LOAD_TIME_BUCKETS_MS.at(-1)!;
  }
  return LOAD_TIME_BUCKETS_MS.at(-1)!;
}

function isValidLoadTime(ms: unknown): ms is number {
  return (
    typeof ms === "number" &&
    Number.isInteger(ms) &&
    ms >= 0 &&
    ms <= MAX_LOAD_TIME_MS
  );
}

function isValidLoadEvent(event: PageLoadEvent | null): event is PageLoadEvent {
  return Boolean(
    event &&
    /^[A-Z0-9]{2}$/.test(event.country) &&
    isValidLoadTime(event.ttfb) &&
    isValidLoadTime(event.shown),
  );
}

function isValidEvent(event: PageViewEvent | null): event is PageViewEvent {
  return Boolean(
    event &&
    /^[0-9a-f]{64}$/.test(event.visitorHash) &&
    event.path.startsWith("/") &&
    event.path.length <= 120 &&
    /^[A-Z0-9]{2}$/.test(event.country) &&
    event.source.length > 0 &&
    event.source.length <= 120 &&
    (event.lat == null ||
      (Number.isFinite(event.lat) && event.lat >= -90 && event.lat <= 90)) &&
    (event.lng == null ||
      (Number.isFinite(event.lng) && event.lng >= -180 && event.lng <= 180)),
  );
}

export function readCookieValue(
  header: string | null,
  name: string,
): string | null {
  if (!header) return null;
  for (const part of header.split(";")) {
    const [rawKey, ...rest] = part.trim().split("=");
    if (rawKey !== name) continue;
    const value = rest.join("=").trim();
    return /^[A-Za-z0-9._:-]{1,120}$/.test(value) ? value : null;
  }
  return null;
}

export function readVisitorId(header: string | null): string | null {
  const value = readCookieValue(header, VISITOR_COOKIE);
  return value && /^[0-9a-f-]{36}$/i.test(value) ? value : null;
}

export function hasOptedOut(header: string | null): boolean {
  return readCookieValue(header, ANALYTICS_OPT_OUT_COOKIE) === "1";
}

function cookie(
  name: string,
  value: string,
  maxAge: number,
  secure: boolean,
): string {
  const parts = [
    `${name}=${value}`,
    "Path=/",
    `Max-Age=${maxAge}`,
    "SameSite=Lax",
    "HttpOnly",
  ];
  if (secure) parts.push("Secure");
  return parts.join("; ");
}

export function visitorCookie(visitorId: string, secure: boolean): string {
  return cookie(VISITOR_COOKIE, visitorId, VISITOR_COOKIE_MAX_AGE, secure);
}

/** Remove the visitor id; the browser is no longer counted. */
export function visitorCookieExpiry(secure: boolean): string {
  return cookie(VISITOR_COOKIE, "", 0, secure);
}

/** Remember, or forget, that this browser asked not to be counted. */
export function optOutCookie(optedOut: boolean, secure: boolean): string {
  return optedOut
    ? cookie(ANALYTICS_OPT_OUT_COOKIE, "1", OPT_OUT_COOKIE_MAX_AGE, secure)
    : cookie(ANALYTICS_OPT_OUT_COOKIE, "", 0, secure);
}

/** Expire each retired analytics cookie the request still carries. */
export function retiredCookieExpiries(
  header: string | null,
  secure: boolean,
): string[] {
  const present = new Set(
    (header ?? "").split(";").map((part) => part.trim().split("=")[0]),
  );
  return RETIRED_ANALYTICS_COOKIES.filter((name) => present.has(name)).map(
    (name) => cookie(name, "", 0, secure),
  );
}

export async function recordPageView(
  namespace: DurableObjectNamespaceLike,
  visitorId: string,
  event: Omit<PageViewEvent, "visitorHash">,
): Promise<VisitStats & { scope: "all" }> {
  const visitorHash = await hashVisitorId(visitorId);
  const response = await namespace
    .getByName(DURABLE_OBJECT_NAME)
    .fetch("https://analytics.internal/hit", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ ...event, visitorHash }),
    });
  if (!response.ok)
    throw new Error(`Visit tracking failed (${response.status})`);
  return response.json();
}

export async function recordPageLoad(
  namespace: DurableObjectNamespaceLike,
  event: PageLoadEvent,
): Promise<void> {
  const response = await namespace
    .getByName(DURABLE_OBJECT_NAME)
    .fetch("https://analytics.internal/load", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(event),
    });
  if (!response.ok)
    throw new Error(`Page-load tracking failed (${response.status})`);
}

export async function queryVisitStats(
  namespace: DurableObjectNamespaceLike,
): Promise<VisitStats> {
  const response = await namespace
    .getByName(DURABLE_OBJECT_NAME)
    .fetch("https://analytics.internal/stats");
  if (!response.ok) throw new Error(`Visit stats failed (${response.status})`);
  return response.json();
}

export async function queryAnalyticsSummary(
  namespace: DurableObjectNamespaceLike,
): Promise<AnalyticsSummary> {
  const response = await namespace
    .getByName(DURABLE_OBJECT_NAME)
    .fetch("https://analytics.internal/analytics");
  if (!response.ok)
    throw new Error(`Analytics summary failed (${response.status})`);
  return response.json();
}

function utcDay(timestamp: number): number {
  return Math.floor(timestamp / DAY_MS);
}

function utcDate(day: number): string {
  return new Date(day * DAY_MS).toISOString().slice(0, 10);
}

async function hashVisitorId(visitorId: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(visitorId),
  );
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}
