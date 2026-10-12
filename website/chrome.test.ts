import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const read = (path: string) =>
  readFileSync(new URL(`../docs/${path}`, import.meta.url), "utf8");
const pages = ["index.html", "guide/index.html", "privacy/index.html"].map(
  read,
);

describe("shared light website chrome", () => {
  it("keeps the same navigation and footer on every public page", () => {
    for (const expression of [
      /<header class="site-header">[\s\S]*?<\/header>/,
      /<footer class="site-footer">[\s\S]*?<\/footer>/,
    ]) {
      const sections = pages.map((html) => html.match(expression)?.[0]);
      expect(sections[0]).toBeTruthy();
      expect(new Set(sections).size).toBe(1);
    }
  });

  it("retains full product names, personal links, and independent analytics", () => {
    for (const page of pages) {
      for (const target of [
        "https://analog-canvas.tokenzhang.com/",
        "https://chip-arena.com/",
        "https://circuits-and-systems.tokenzhang.com/",
        "https://www.tokenzhang.com/",
        'href="/guide/"',
        'href="/privacy/"',
        'href="/analytics/"',
        "Analog Canvas",
        "CAS Classroom",
        "data-visitors",
        "data-views",
      ])
        expect(page).toContain(target);
      expect(page).not.toContain("related-nav");
      expect(page).not.toContain("footer-brand");
      expect(page).not.toContain('v=20261012"');
    }
  });

  it("uses a light default, including the independent dashboard", () => {
    const css = read("site.css");
    expect(css).toMatch(/color-scheme:\s*light/);
    expect(css).toMatch(/--bg:\s*#f7f8fa/);
    expect(css).not.toContain("color-scheme: dark");
    expect(css).not.toContain("#080a10");
    expect(read("analytics/index.html")).toContain(
      '<html lang="en" class="light">',
    );
  });

  it("shares the new circuit-bridge mark with the social preview", () => {
    const logo = read("favicon.svg");
    const social = read("assets/social-card.svg");
    expect(logo).toContain('fill="#fff"');
    expect(logo).not.toContain("rotate(");
    for (const match of logo.matchAll(/<path\s+d="([^"]+)"/g))
      expect(social).toContain(match[1]);
  });
});
