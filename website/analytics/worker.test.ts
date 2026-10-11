import { DatabaseSync } from "node:sqlite";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AnalyticsDO, type AnalyticsSummary } from "./worker";

const DAY_MS = 24 * 60 * 60 * 1000;
const DAY_ONE = Date.UTC(2026, 9, 1, 12);
const HASH_A = "a".repeat(64);
const HASH_B = "b".repeat(64);

function sqliteState(db = new DatabaseSync(":memory:")) {
  return {
    storage: {
      sql: {
        exec<T>(query: string, ...bindings: unknown[]) {
          const statement = db.prepare(query);
          if (/^\s*(select|with|pragma)/iu.test(query)) {
            const rows = statement.all(
              ...(bindings as (string | number | null)[]),
            ) as T[];
            return {
              toArray: () => rows,
              one: () => {
                if (rows.length !== 1) throw new Error("expected one row");
                return rows[0]!;
              },
            };
          }
          statement.run(...(bindings as (string | number | null)[]));
          return {
            toArray: () => [] as T[],
            one: () => {
              throw new Error("no rows");
            },
          };
        },
      },
      transactionSync<T>(callback: () => T): T {
        return callback();
      },
    },
  };
}

async function hit(
  analytics: AnalyticsDO,
  visitorHash: string,
  extra: { path?: string; source?: string } = {},
) {
  const response = await analytics.fetch(
    new Request("https://analytics.internal/hit", {
      method: "POST",
      body: JSON.stringify({
        visitorHash,
        path: extra.path ?? "/",
        country: "CH",
        lat: 47.4,
        lng: 8.5,
        source: extra.source ?? "direct-or-unknown",
      }),
    }),
  );
  return (await response.json()) as { pv: number; uv: number };
}

async function summary(analytics: AnalyticsDO): Promise<AnalyticsSummary> {
  return (await analytics
    .fetch(new Request("https://analytics.internal/analytics"))
    .then((response) => response.json())) as AnalyticsSummary;
}

function rows(db: DatabaseSync, query: string) {
  return db.prepare(query).all();
}

afterEach(() => {
  vi.useRealTimers();
});

describe("visitor counting", () => {
  it("counts a returning visitor once, whatever the day", async () => {
    vi.useFakeTimers({ now: DAY_ONE });
    const analytics = new AnalyticsDO(sqliteState());
    await hit(analytics, HASH_A, { source: "social:github" });
    await hit(analytics, HASH_A, { path: "/editor" });
    vi.setSystemTime(DAY_ONE + 3 * DAY_MS);
    expect(await hit(analytics, HASH_A)).toEqual({
      pv: 3,
      uv: 1,
      scope: "all",
    });
    expect(await hit(analytics, HASH_B)).toMatchObject({ pv: 4, uv: 2 });
    const report = await summary(analytics);
    // Each day still counts who came that day.
    expect(report.days.at(-4)).toMatchObject({ pv: 2, uv: 1 });
    expect(report.days.at(-1)).toMatchObject({ pv: 2, uv: 2 });
    // The second view had no source of its own; it keeps GitHub's.
    expect(report.sources).toContainEqual({
      source: "social:github",
      pv: 2,
      uv: 1,
    });
  });

  it("keeps only today's hashes per day, and drops ids whose cookie expired", async () => {
    vi.useFakeTimers({ now: DAY_ONE });
    const db = new DatabaseSync(":memory:");
    const analytics = new AnalyticsDO(sqliteState(db));
    await hit(analytics, HASH_A);
    vi.setSystemTime(DAY_ONE + DAY_MS);
    await hit(analytics, HASH_B);
    const day = Math.floor(DAY_ONE / DAY_MS);
    expect(rows(db, "SELECT day, visitors FROM daily_visitor_counts")).toEqual([
      { day, visitors: 1 },
    ]);
    expect(rows(db, "SELECT day FROM daily_visitors")).toEqual([
      { day: day + 1 },
    ]);

    // A year and a day later both cookies have expired: the ids go, and
    // the total keeps counting them.
    vi.setSystemTime(DAY_ONE + 368 * DAY_MS);
    const stats = await analytics
      .fetch(new Request("https://analytics.internal/stats"))
      .then((response) => response.json());
    expect(stats).toMatchObject({ uv: 2 });
    expect(rows(db, "SELECT visitor_hash FROM visitors")).toEqual([]);
  });

  it("starts the total from the visitors already counted, once", async () => {
    vi.useFakeTimers({ now: DAY_ONE });
    const today = Math.floor(DAY_ONE / DAY_MS);
    const db = new DatabaseSync(":memory:");
    // A deployment from before the total became a counter.
    db.exec(`
      CREATE TABLE visitors (
        visitor_hash TEXT PRIMARY KEY,
        last_seen_day INTEGER NOT NULL
      ) WITHOUT ROWID;
      CREATE TABLE daily_views (day INTEGER PRIMARY KEY, views INTEGER NOT NULL);
      CREATE TABLE daily_visitors (
        day INTEGER NOT NULL,
        visitor_hash TEXT NOT NULL,
        PRIMARY KEY (day, visitor_hash)
      ) WITHOUT ROWID;
      INSERT INTO visitors VALUES ('${HASH_A}', ${today - 1}), ('x', ${today - 1}), ('y', ${today});
      INSERT INTO daily_views VALUES (${today - 1}, 5), (${today}, 2);
      INSERT INTO daily_visitors VALUES
        (${today - 1}, '${HASH_A}'), (${today - 1}, 'x'), (${today}, 'y');
    `);
    const analytics = new AnalyticsDO(sqliteState(db));
    const stats = await analytics
      .fetch(new Request("https://analytics.internal/stats"))
      .then((response) => response.json());
    expect(stats).toEqual({ pv: 7, uv: 3, scope: "all" });
    const days = (await summary(analytics)).days.slice(-2);
    expect(days.map((entry) => [entry.pv, entry.uv])).toEqual([
      [5, 2],
      [2, 1],
    ]);

    // A known visitor adds a view, not a visitor; a later start leaves the
    // total alone.
    const again = new AnalyticsDO(sqliteState(db));
    expect(await hit(again, HASH_A)).toMatchObject({ pv: 8, uv: 3 });
    expect(await hit(again, HASH_B)).toMatchObject({ pv: 9, uv: 4 });
  });
});

describe("page-load times (#1581)", () => {
  async function load(
    analytics: AnalyticsDO,
    country: string,
    ttfb: number,
    shown: number,
  ) {
    return analytics.fetch(
      new Request("https://analytics.internal/load", {
        method: "POST",
        body: JSON.stringify({ country, ttfb, shown }),
      }),
    );
  }

  it("keeps counts per country and summarizes medians and 75th percentiles", async () => {
    vi.useFakeTimers({ now: DAY_ONE });
    const db = new DatabaseSync(":memory:");
    const analytics = new AnalyticsDO(sqliteState(db));
    for (const [ttfb, shown] of [
      [150, 1100],
      [180, 1200],
      [420, 2600],
    ])
      expect((await load(analytics, "CH", ttfb!, shown!)).status).toBe(204);
    await load(analytics, "CN", 900, 4200);
    // Nothing a visitor could be told apart by is accepted or kept.
    expect((await load(analytics, "cn", 900, 4200)).status).toBe(400);
    expect((await load(analytics, "CN", -1, 4200)).status).toBe(400);
    expect((await load(analytics, "CN", 900, 1.5)).status).toBe(400);
    expect(
      Object.keys(
        rows(db, "SELECT * FROM load_times LIMIT 1")[0] as object,
      ).sort(),
    ).toEqual(["bucket", "count", "country", "day", "metric"]);

    const { loadTimes } = await summary(analytics);
    expect(loadTimes.days).toBe(30);
    expect(loadTimes.all).toMatchObject({ code: "ALL", n: 4 });
    expect(loadTimes.countries).toEqual([
      {
        code: "CH",
        n: 3,
        ttfbP50: 200,
        ttfbP75: 500,
        shownP50: 1250,
        shownP75: 3000,
      },
      {
        code: "CN",
        n: 1,
        ttfbP50: 900,
        ttfbP75: 900,
        shownP50: 5000,
        shownP75: 5000,
      },
    ]);
  });

  it("summarizes only the last 30 days, and drops days past retention", async () => {
    vi.useFakeTimers({ now: DAY_ONE });
    const db = new DatabaseSync(":memory:");
    const analytics = new AnalyticsDO(sqliteState(db));
    await load(analytics, "CH", 150, 1100);
    vi.setSystemTime(DAY_ONE + 30 * DAY_MS);
    expect((await summary(analytics)).loadTimes).toEqual({
      days: 30,
      all: null,
      countries: [],
    });
    vi.setSystemTime(DAY_ONE + 401 * DAY_MS);
    await summary(analytics);
    expect(rows(db, "SELECT day FROM load_times")).toEqual([]);
  });
});
