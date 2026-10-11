import {
  routeAnalyticsRequest,
  type AnalyticsRouteEnv,
} from "./analytics/routes";
export { AnalyticsDO } from "./analytics/worker";

type Env = AnalyticsRouteEnv & {
  ASSETS: { fetch(request: Request): Promise<Response> };
};

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const response = await routeAnalyticsRequest(request, env);
    if (response) return response;
    if (new URL(request.url).pathname.startsWith("/api/")) {
      return Response.json({ error: "Not found" }, { status: 404 });
    }
    return env.ASSETS.fetch(request);
  },
};
