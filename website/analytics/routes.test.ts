import { describe, expect, it } from "vitest";

import { ANALYTICS_OPT_OUT_PATH, ANALYTICS_PAGE_LOAD_PATH } from "./client";
import {
  OPT_OUT_ROUTE,
  PAGE_LOAD_ROUTE,
  normalizeAcquisitionSource,
  normalizeTrackedPath,
  routeAnalyticsRequest,
  type AnalyticsRouteEnv,
} from "./routes";

function analyticsEnv(
  response: unknown,
  onFetch?: (input: string, init?: RequestInit) => void,
): AnalyticsRouteEnv {
  return {
    ANALYTICS_KEY: undefined,
    ANALYTICS: {
      getByName(name) {
        expect(name).toBe("global");
        return {
          async fetch(input, init) {
            onFetch?.(input, init);
            return Response.json(response);
          },
        };
      },
    },
  };
}

describe("analytics request normalization", () => {
  it("keeps bounded page paths and excludes analytics/API routes", () => {
    expect(normalizeTrackedPath("/editor?utm_source=github#canvas")).toBe(
      "/editor",
    );
    expect(normalizeTrackedPath("/analytics")).toBeNull();
    expect(normalizeTrackedPath("/api/stats")).toBeNull();
    expect(normalizeTrackedPath("https://example.com/")).toBeNull();
  });

  it("retains only normalized acquisition categories or hostnames", () => {
    const site = new URL("https://virtuoso-bridge.tokenzhang.com/");
    expect(
      normalizeAcquisitionSource(
        "https://www.google.com/search?q=private",
        "",
        site,
      ),
    ).toBe("search:google");
    expect(
      normalizeAcquisitionSource(
        "https://github.com/some/private/path",
        "",
        site,
      ),
    ).toBe("social:github");
    expect(
      normalizeAcquisitionSource(
        "https://example.com/private/path?q=secret",
        "",
        site,
      ),
    ).toBe("ref:example.com");
    expect(normalizeAcquisitionSource("", "qrcode", site)).toBe("campaign:qr");
  });
});

describe("analytics HTTP routes", () => {
  it("delegates the public counters to the established global object", async () => {
    const response = await routeAnalyticsRequest(
      new Request("https://virtuoso-bridge.tokenzhang.com/api/stats"),
      analyticsEnv({ pv: 47_111, uv: 11_781 }, (input) => {
        expect(input).toBe("https://analytics.internal/stats");
      }),
    );

    expect(response?.status).toBe(200);
    await expect(response?.json()).resolves.toEqual({
      pv: 47_111,
      uv: 11_781,
      scope: "all",
    });
  });

  it("leaves unrelated requests for the host Worker", async () => {
    await expect(
      routeAnalyticsRequest(
        new Request("https://virtuoso-bridge.tokenzhang.com/api/projects"),
        analyticsEnv({}),
      ),
    ).resolves.toBeNull();
  });
});

const SITE = "https://virtuoso-bridge.tokenzhang.com";

function track(headers: Record<string, string> = {}) {
  return new Request(`${SITE}/api/track`, {
    method: "POST",
    headers: {
      origin: SITE,
      "user-agent": "Mozilla/5.0 Firefox/150.0",
      ...headers,
    },
    body: JSON.stringify({ p: "/editor", r: "", s: "" }),
  });
}

describe("the visitor cookie", () => {
  it("is set once, never renewed, and the retired source cookie goes", async () => {
    let counted = 0;
    const env = analyticsEnv({ pv: 1, uv: 1, scope: "all" }, () => {
      counted += 1;
    });
    const first = await routeAnalyticsRequest(track(), env);
    const [visitor] = first!.headers.getSetCookie();
    expect(visitor).toMatch(
      /^vb_vid=[0-9a-f-]{36}; Path=\/; Max-Age=31536000; SameSite=Lax; HttpOnly; Secure$/u,
    );
    const id = visitor!.split(";")[0]!;
    const returning = await routeAnalyticsRequest(
      track({ cookie: `${id}; vb_sid=social:github` }),
      env,
    );
    expect(returning!.headers.getSetCookie()).toEqual([
      "vb_sid=; Path=/; Max-Age=0; SameSite=Lax; HttpOnly; Secure",
    ]);
    expect(counted).toBe(2);
  });

  it("is never set for a browser that asks not to be counted", async () => {
    let counted = 0;
    const env = analyticsEnv({}, () => {
      counted += 1;
    });
    const privacyHeaders: Record<string, string>[] = [
      { DNT: "1" },
      { "Sec-GPC": "1" },
      { cookie: "vb_optout=1" },
    ];
    for (const headers of privacyHeaders) {
      const response = await routeAnalyticsRequest(track(headers), env);
      expect(response!.status).toBe(204);
      expect(response!.headers.getSetCookie()).toEqual([]);
    }
    expect(counted).toBe(0);
  });

  it("answers at the path the privacy notice calls", () => {
    expect(OPT_OUT_ROUTE).toBe(ANALYTICS_OPT_OUT_PATH);
  });

  it("opts out, and back in, from the privacy notice", async () => {
    const env = analyticsEnv({});
    const optOut = (optOut: unknown, origin = SITE) =>
      routeAnalyticsRequest(
        new Request(`${SITE}/api/track/opt-out`, {
          method: "POST",
          headers: { origin, cookie: "vb_vid=x" },
          body: JSON.stringify({ optOut }),
        }),
        env,
      );
    const out = await optOut(true);
    expect(await out!.json()).toEqual({ optedOut: true });
    expect(out!.headers.getSetCookie()).toEqual([
      "vb_optout=1; Path=/; Max-Age=34214400; SameSite=Lax; HttpOnly; Secure",
      "vb_vid=; Path=/; Max-Age=0; SameSite=Lax; HttpOnly; Secure",
    ]);
    const back = await optOut(false);
    expect(back!.headers.getSetCookie()).toEqual([
      "vb_optout=; Path=/; Max-Age=0; SameSite=Lax; HttpOnly; Secure",
    ]);
    expect((await optOut(true, "https://elsewhere.example"))!.status).toBe(403);
    expect((await optOut("yes"))!.status).toBe(400);
    const state = await routeAnalyticsRequest(
      new Request(`${SITE}/api/track/opt-out`, {
        headers: { cookie: "vb_optout=1" },
      }),
      env,
    );
    expect(await state!.json()).toEqual({ optedOut: true });
  });
});

describe("page-load reports (#1581)", () => {
  function report(body: unknown, headers: Record<string, string> = {}) {
    const request = new Request(`${SITE}${ANALYTICS_PAGE_LOAD_PATH}`, {
      method: "POST",
      headers: {
        origin: SITE,
        "user-agent": "Mozilla/5.0 Firefox/150.0",
        ...headers,
      },
      body: JSON.stringify(body),
    });
    Object.defineProperty(request, "cf", { value: { country: "cn" } });
    return request;
  }

  it("records the times with the country only: no cookie, no visitor", async () => {
    expect(PAGE_LOAD_ROUTE).toBe(ANALYTICS_PAGE_LOAD_PATH);
    const recorded: unknown[] = [];
    const env = analyticsEnv({}, (input, init) => {
      expect(input).toBe("https://analytics.internal/load");
      recorded.push(JSON.parse(String(init?.body)));
    });
    const response = await routeAnalyticsRequest(
      report({ t: 812.4, l: 2410.6 }, { cookie: "vb_vid=abc" }),
      env,
    );
    expect(response!.status).toBe(204);
    expect(response!.headers.getSetCookie()).toEqual([]);
    expect(recorded).toEqual([{ country: "CN", ttfb: 812, shown: 2411 }]);
  });

  it("drops reports from browsers that ask not to be counted, bots and bad values", async () => {
    let recorded = 0;
    const env = analyticsEnv({}, () => {
      recorded += 1;
    });
    for (const request of [
      report({ t: 800, l: 2400 }, { DNT: "1" }),
      report({ t: 800, l: 2400 }, { "Sec-GPC": "1" }),
      report({ t: 800, l: 2400 }, { cookie: "vb_optout=1" }),
      report({ t: 800, l: 2400 }, { "user-agent": "curl/8.0" }),
      report({ t: 800, l: 2400 }, { origin: "https://example.com" }),
      report({ t: "800", l: 2400 }),
      report({ t: 800, l: 700_000 }),
      report(null),
    ]) {
      const response = await routeAnalyticsRequest(request, env);
      expect(response!.status).toBe(204);
    }
    expect(recorded).toBe(0);
  });
});
