import {
  ANALYTICS_PERSISTENCE_IDENTITY,
  hasOptedOut,
  optOutCookie,
  queryAnalyticsSummary,
  MAX_LOAD_TIME_MS,
  queryVisitStats,
  readVisitorId,
  recordPageLoad,
  recordPageView,
  retiredCookieExpiries,
  visitorCookie,
  visitorCookieExpiry,
  type DurableObjectNamespaceLike,
} from "./worker";

const [TRACK_ROUTE, STATS_ROUTE, ANALYTICS_ROUTE] =
  ANALYTICS_PERSISTENCE_IDENTITY.routes;
/** GET reads, POST `{optOut}` sets, whether this browser is counted. */
export const OPT_OUT_ROUTE = `${TRACK_ROUTE}/opt-out`;
/** POST `{t, l}`: how long this page load took (#1581). */
export const PAGE_LOAD_ROUTE = `${TRACK_ROUTE}/load`;

export type AnalyticsRouteEnv = {
  ANALYTICS: DurableObjectNamespaceLike;
  ANALYTICS_KEY: string | undefined;
};

type RequestCf = {
  country?: string;
  latitude?: string;
  longitude?: string;
};

const BOT_UA =
  /bot|crawler|spider|slurp|headless|phantom|pingdom|uptime|lighthouse|pagespeed|curl|wget|python|axios|fetch\//i;

const CAMPAIGN_SOURCES: Record<string, string> = {
  google: "search:google",
  bing: "search:bing",
  baidu: "search:baidu",
  duckduckgo: "search:duckduckgo",
  wechat: "social:wechat",
  weixin: "social:wechat",
  linkedin: "social:linkedin",
  twitter: "social:x",
  x: "social:x",
  github: "social:github",
  zhihu: "social:zhihu",
  bilibili: "social:bilibili",
  xiaohongshu: "social:xiaohongshu",
  rednote: "social:xiaohongshu",
  email: "campaign:email",
  newsletter: "campaign:email",
  qr: "campaign:qr",
  qrcode: "campaign:qr",
  rss: "campaign:rss",
};

const REFERRER_CATEGORIES: readonly (readonly [string, readonly string[]])[] = [
  ["search:bing", ["bing.com"]],
  ["search:baidu", ["baidu.com"]],
  ["search:duckduckgo", ["duckduckgo.com"]],
  ["search:yahoo", ["yahoo.com"]],
  ["search:yandex", ["yandex.com", "yandex.ru"]],
  ["search:ecosia", ["ecosia.org"]],
  ["search:naver", ["naver.com"]],
  ["search:sogou", ["sogou.com"]],
  ["search:360", ["so.com"]],
  ["social:wechat", ["weixin.qq.com", "servicewechat.com", "wechat.com"]],
  ["social:linkedin", ["linkedin.com", "lnkd.in"]],
  ["social:x", ["x.com", "twitter.com", "t.co"]],
  ["social:facebook", ["facebook.com", "fb.com"]],
  ["social:instagram", ["instagram.com"]],
  ["social:reddit", ["reddit.com", "redd.it"]],
  ["social:github", ["github.com"]],
  ["social:zhihu", ["zhihu.com"]],
  ["social:bilibili", ["bilibili.com"]],
  ["social:xiaohongshu", ["xiaohongshu.com", "xhslink.com"]],
];

/** Route the complete analytics HTTP API, or return null for the host app. */
export async function routeAnalyticsRequest(
  request: Request,
  env: AnalyticsRouteEnv,
): Promise<Response | null> {
  const url = new URL(request.url);
  if (url.pathname === TRACK_ROUTE && request.method === "POST") {
    return trackPageView(request, env);
  }
  if (url.pathname === PAGE_LOAD_ROUTE && request.method === "POST") {
    return trackPageLoad(request, env);
  }
  if (url.pathname === OPT_OUT_ROUTE) {
    return optOut(request);
  }
  if (url.pathname === STATS_ROUTE && request.method === "GET") {
    return stats(env);
  }
  if (url.pathname === ANALYTICS_ROUTE && request.method === "GET") {
    return analytics(request, env);
  }
  return null;
}

/**
 * Whether a request may be counted: same-origin, from a browser that has not
 * asked not to be (Do Not Track, Global Privacy Control or this site's own
 * opt-out), and not from a bot.
 */
function countable(request: Request): boolean {
  if (!isSameOriginTrackRequest(request)) return false;
  if (
    request.headers.get("DNT") === "1" ||
    request.headers.get("Sec-GPC") === "1" ||
    hasOptedOut(request.headers.get("Cookie"))
  )
    return false;
  const userAgent = request.headers.get("User-Agent") ?? "";
  return Boolean(userAgent) && !BOT_UA.test(userAgent);
}

/** The visitor's country as Cloudflare reports it; "XX" when unknown. */
function requestCountry(request: Request): string {
  const cf = (request as Request & { cf?: RequestCf }).cf ?? {};
  const rawCountry =
    typeof cf.country === "string" ? cf.country.toUpperCase() : "";
  return /^[A-Z0-9]{2}$/.test(rawCountry) ? rawCountry : "XX";
}

/**
 * How long a page load took (#1581), kept as counts per day and country:
 * no visitor id, no cookie, no address. Always answers 204.
 */
async function trackPageLoad(
  request: Request,
  env: AnalyticsRouteEnv,
): Promise<Response> {
  const noContent = new Response(null, {
    status: 204,
    headers: { "cache-control": "no-store" },
  });
  if (!countable(request)) return noContent;
  const payload = (await request.json().catch(() => null)) as {
    t?: unknown;
    l?: unknown;
  } | null;
  const ttfb = loadTimeMs(payload?.t);
  const shown = loadTimeMs(payload?.l);
  if (ttfb === null || shown === null) return noContent;
  try {
    await recordPageLoad(env.ANALYTICS, {
      country: requestCountry(request),
      ttfb,
      shown,
    });
  } catch {
    // A lost report only thins the sample.
  }
  return noContent;
}

function loadTimeMs(raw: unknown): number | null {
  if (typeof raw !== "number" || !Number.isFinite(raw)) return null;
  const ms = Math.round(raw);
  return ms >= 0 && ms <= MAX_LOAD_TIME_MS ? ms : null;
}

async function trackPageView(
  request: Request,
  env: AnalyticsRouteEnv,
): Promise<Response> {
  const noContent = () => new Response(null, { status: 204 });
  // A browser asking not to be tracked, by Do Not Track, Global Privacy
  // Control or this site's own opt-out, is not counted and gets no cookie.
  if (!countable(request)) return noContent();

  const payload = (await request.json().catch(() => null)) as {
    p?: unknown;
    r?: unknown;
    s?: unknown;
  } | null;
  const path = normalizeTrackedPath(payload?.p);
  if (!path) return noContent();

  const cookieHeader = request.headers.get("Cookie");
  const existing = readVisitorId(cookieHeader);
  const visitorId = existing ?? crypto.randomUUID();
  // The analytics object keeps a visitor's first source of the day.
  const source = normalizeAcquisitionSource(
    payload?.r,
    payload?.s,
    new URL(request.url),
  );
  const cf = (request as Request & { cf?: RequestCf }).cf ?? {};
  const country = requestCountry(request);
  const secure = request.url.startsWith("https://");
  const headers = new Headers({ "cache-control": "no-store" });
  // Set once, never renewed: the id lasts a year from the first visit.
  if (!existing) headers.append("Set-Cookie", visitorCookie(visitorId, secure));
  for (const expiry of retiredCookieExpiries(cookieHeader, secure))
    headers.append("Set-Cookie", expiry);

  try {
    const result = await recordPageView(env.ANALYTICS, visitorId, {
      path,
      country,
      lat: cfNumber(cf.latitude),
      lng: cfNumber(cf.longitude),
      source,
    });
    return Response.json(result, { headers });
  } catch {
    return new Response(null, { status: 204, headers });
  }
}

/**
 * The privacy notice's "Stop counting me": opting out removes the visitor
 * id and remembers the choice for 13 months; opting back in forgets it.
 */
async function optOut(request: Request): Promise<Response> {
  const cookieHeader = request.headers.get("Cookie");
  const headers = new Headers({ "cache-control": "no-store" });
  if (request.method === "GET")
    return Response.json({ optedOut: hasOptedOut(cookieHeader) }, { headers });
  if (request.method !== "POST")
    return Response.json({ error: "method-not-allowed" }, { status: 405 });
  if (!isSameOriginTrackRequest(request))
    return Response.json({ error: "forbidden" }, { status: 403 });
  const body = (await request.json().catch(() => null)) as {
    optOut?: unknown;
  } | null;
  if (typeof body?.optOut !== "boolean")
    return Response.json({ error: "invalid-request" }, { status: 400 });
  const secure = request.url.startsWith("https://");
  headers.append("Set-Cookie", optOutCookie(body.optOut, secure));
  if (body.optOut) headers.append("Set-Cookie", visitorCookieExpiry(secure));
  return Response.json({ optedOut: body.optOut }, { headers });
}

async function stats(env: AnalyticsRouteEnv): Promise<Response> {
  try {
    const result = await queryVisitStats(env.ANALYTICS);
    return Response.json(
      { ...result, scope: "all" },
      { headers: { "cache-control": "no-store" } },
    );
  } catch {
    return Response.json({ error: "Analytics unavailable" }, { status: 502 });
  }
}

async function analytics(
  request: Request,
  env: AnalyticsRouteEnv,
): Promise<Response> {
  if (
    env.ANALYTICS_KEY &&
    new URL(request.url).searchParams.get("key") !== env.ANALYTICS_KEY
  ) {
    return Response.json({ error: "Forbidden" }, { status: 403 });
  }
  try {
    return Response.json(await queryAnalyticsSummary(env.ANALYTICS), {
      headers: { "cache-control": "no-store" },
    });
  } catch {
    return Response.json({ error: "Analytics unavailable" }, { status: 502 });
  }
}

export function normalizeTrackedPath(raw: unknown): string | null {
  if (typeof raw !== "string") return null;
  let path = raw.split("?")[0]?.split("#")[0] ?? "";
  if (!path.startsWith("/")) return null;
  if (path.length > 120) path = path.slice(0, 120);
  if (path.startsWith("/api/") || /^\/analytics\/?$/.test(path)) return null;
  return path;
}

export function normalizeAcquisitionSource(
  rawReferrer: unknown,
  rawCampaignSource: unknown,
  siteUrl: URL,
): string {
  if (typeof rawCampaignSource === "string" && rawCampaignSource.trim()) {
    const key = rawCampaignSource.trim().toLowerCase();
    if (!/^[a-z0-9._-]{1,40}$/.test(key)) return "campaign:other";
    return CAMPAIGN_SOURCES[key] ?? "campaign:other";
  }
  if (typeof rawReferrer !== "string" || !rawReferrer.trim())
    return "direct-or-unknown";
  try {
    const referrer = new URL(rawReferrer);
    if (!/^https?:$/.test(referrer.protocol)) return "direct-or-unknown";
    const hostname = canonicalHostname(referrer.hostname);
    if (!hostname || hostname === canonicalHostname(siteUrl.hostname))
      return "direct-or-unknown";
    if (/^google\.[a-z.]+$/.test(hostname)) return "search:google";
    for (const [source, domains] of REFERRER_CATEGORIES) {
      if (
        domains.some(
          (domain) => hostname === domain || hostname.endsWith(`.${domain}`),
        )
      ) {
        return source;
      }
    }
    return /^[a-z0-9.-]{1,100}$/.test(hostname)
      ? `ref:${hostname}`
      : "ref:other";
  } catch {
    return "direct-or-unknown";
  }
}

function isSameOriginTrackRequest(request: Request): boolean {
  const expectedOrigin = new URL(request.url).origin;
  const origin = request.headers.get("Origin");
  const fetchSite = request.headers.get("Sec-Fetch-Site");
  if (origin && origin !== expectedOrigin) return false;
  if (fetchSite && fetchSite !== "same-origin") return false;
  if (origin || fetchSite === "same-origin") return true;
  const referer = request.headers.get("Referer");
  if (!referer) return false;
  try {
    return new URL(referer).origin === expectedOrigin;
  } catch {
    return false;
  }
}

function canonicalHostname(hostname: string): string {
  return hostname
    .toLowerCase()
    .replace(/^www\./, "")
    .replace(/\.$/, "");
}

function cfNumber(value: string | undefined): number | null {
  if (!value) return null;
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : null;
}
