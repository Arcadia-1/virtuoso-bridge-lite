import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { describe, expect, it } from "vitest";

for (const page of ["index.html", "guide/index.html", "privacy/index.html"]) {
  const html = readFileSync(new URL(`../docs/${page}`, import.meta.url), "utf8");
  const init = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];

  const initialize = (search = "", stored: string | null = null, blocked = false) => {
    const root = { lang: "en", dataset: { lang: "en" }, classList: { add() {} } };
    runInNewContext(init!, {
      document: { documentElement: root },
      URLSearchParams,
      location: { search },
      navigator: { language: "zh-CN" },
      localStorage: {
        getItem: () => {
          if (blocked) throw new Error("Storage unavailable");
          return stored;
        },
      },
    });
    return root;
  };

  describe(`${page} language initialization`, () => {
    it("starts in English even in a Chinese browser", () => {
      expect(init).toBeTruthy();
      expect(initialize().lang).toBe("en");
      expect(initialize().dataset.lang).toBe("en");
    });

    it("honors explicit links before saved manual choices", () => {
      expect(initialize("?lang=zh").lang).toBe("zh-CN");
      expect(initialize("?lang=en", "zh").lang).toBe("en");
      expect(initialize("?lang=zh", "en").lang).toBe("zh-CN");
      expect(initialize("", "zh").lang).toBe("zh-CN");
      expect(initialize("", "en").lang).toBe("en");
    });

    it("keeps English as the fallback when storage is blocked", () => {
      expect(initialize("", null, true).lang).toBe("en");
      expect(initialize("?lang=zh", null, true).lang).toBe("zh-CN");
    });
  });
}
