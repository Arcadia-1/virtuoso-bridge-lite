import { afterEach, describe, expect, it, vi } from "vitest";
import { startAnalytics } from "./client";

afterEach(() => vi.unstubAllGlobals());

describe("shared footer on non-tracking pages", () => {
  it.each(["/privacy/", "/analytics/"])(
    "reads public counts without tracking a visit on %s",
    async (pathname) => {
      vi.stubGlobal("location", {
        hostname: "virtuoso-bridge.tokenzhang.com",
        pathname,
      });
      const counts = { pv: 55, uv: 32, scope: "all" };
      const fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => counts,
      });
      vi.stubGlobal("fetch", fetch);
      const update = vi.fn();
      startAnalytics(update);
      await vi.waitFor(() => expect(update).toHaveBeenCalledWith(counts));
      expect(fetch).toHaveBeenCalledExactlyOnceWith("/api/stats", {
        cache: "no-store",
      });
    },
  );
});
