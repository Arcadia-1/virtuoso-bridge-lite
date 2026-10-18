import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const read = (path: string) =>
  readFileSync(new URL(`../docs/${path}`, import.meta.url), "utf8");
const pages = ["index.html", "privacy/index.html", "guide/index.html"].map(
  read,
);

describe("shared light website chrome", () => {
  it("keeps shared document navigation and the same footer on every public page", () => {
    for (const expression of [
      /<header class="site-header">[\s\S]*?<\/header>/,
      /<footer class="site-footer">[\s\S]*?<\/footer>/,
    ]) {
      const sharedPages = expression.source.includes("header") ? pages.slice(1) : pages;
      const sections = sharedPages.map((html) => html.match(expression)?.[0]);
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
        'href="/guide/" data-internal',
        'href="/guide/#cite" data-internal',
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

  it("shows four direct cross-site links and keeps Docs in the utility area", () => {
    for (const page of pages) {
      const nav = page.match(/<nav class="header-nav(?: project-links)?"[^>]*>[\s\S]*?<\/nav>/)?.[0] ?? "";
      const actions = page.match(/<div class="header-actions">[\s\S]*?<\/div>/)?.[0] ?? "";
      expect(nav.match(/class="site-link"/g)).toHaveLength(4);
      expect(nav).not.toContain("nav-guide");
      expect(actions).toContain('class="nav-guide"');
      expect(page).not.toContain("site-switcher");
      for (const link of nav.match(/<a [\s\S]*?<\/a>/g) ?? []) {
        expect(link).toContain('aria-label="');
        expect(link).toContain('title="');
        expect(link).toContain('target="_blank" rel="noopener"');
        expect(link).toContain('width="24" height="24"');
      }
    }
    expect(read("site.js")).not.toContain("siteSwitcher");
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

  it("uses one frameless VB mark for the favicon, header and social preview", () => {
    const paths = (svg: string) =>
      [...svg.matchAll(/\sd="([^"]+)"/g)].map((match) => match[1]);
    const [mark] = paths(read("assets/mark.svg"));
    const favicon = read("favicon.svg");
    expect(mark).toBeTruthy();
    expect(paths(favicon)).toEqual([mark]);
    expect(favicon).not.toContain("<rect");
    expect(favicon).toContain("prefers-color-scheme: dark");
    expect(read("assets/social-card.svg")).toContain(`d="${mark}"`);
    expect(read("assets/architecture.svg")).toContain(`d="${mark}"`);
    for (const page of pages)
      expect(page).toContain('src="/assets/mark.svg?v=20261012-vb"');
  });

  it("uses the personal homepage's A / 文 language icon on every public page", () => {
    for (const page of pages) {
      const switches = [
        ...page.matchAll(
          /<button\s[^>]*data-language-toggle[^>]*>[\s\S]*?<\/button>/g,
        ),
      ];
      expect(switches).toHaveLength(1);
      const toggle = switches[0][0];
      for (const path of ["m5 8 6 6", "m4 14 6-6 2-3", "M2 5h12", "M7 2h1", "m22 22-5-10-5 10", "M14 18h6"])
        expect(toggle).toContain(`d="${path}"`);
      expect(toggle).toContain('width="22"');
      expect(toggle).toContain('height="22"');
      expect(toggle).toContain('stroke-width="1.75"');
      expect(toggle).not.toContain("<span");
      expect(page).not.toContain('data-language="');
    }
  });

  it("keeps the two-line hero headline", () => {
    const h1 = pages[0].match(/<h1>[\s\S]*?<\/h1>/)?.[0] ?? "";
    expect(h1).toContain('Your agent.<span class="gradient-text">Inside Virtuoso.</span>');
    expect(h1).toContain('让智能体，<span class="gradient-text">走进 Virtuoso。</span>');
  });

  it("keeps the landing page a short front door with a correct quick start", () => {
    const landing = pages[0];
    expect(landing).not.toContain("Design preview");
    // `start` opens the tunnel; the CIW load(...) line is printed by `status`.
    expect(landing).toMatch(/virtuoso-bridge status\s+<span class="comment"># prints the load/);
    expect(landing).not.toMatch(/load\([^)]*\)[^<]*(printed by|输出的)[^<]*start/);
    expect(landing).not.toContain("virtuoso-bridge-lite#readme");
  });

  it("lists external projects at the bottom of the home page", () => {
    const landing = pages[0];
    const section = landing.match(/<section class="block ecosystem" id="ecosystem">[\s\S]*?<\/section>/)?.[0];
    expect(section).toBeTruthy();
    const repos = [...section!.matchAll(/<li><a href="https:\/\/github\.com\/([^"]+)" title="\1">/g)].map((m) => m[1]);
    expect(repos.length).toBeGreaterThan(40);
    expect(new Set(repos).size).toBe(repos.length);
    // Same-author projects stay out of the list.
    for (const repo of repos) expect(repo).not.toMatch(/^(Arcadia-1|cascode-ai)\//);
    // It is the last section on the page.
    expect(landing.indexOf('id="ecosystem"')).toBeGreaterThan(landing.indexOf('id="docs"'));
  });

  it("compares with skillbridge in one labelled table", () => {
    const table = pages[0].match(/<table class="compare">[\s\S]*?<\/table>/)?.[0];
    expect(table).toBeTruthy();
    expect(table).toContain('href="https://github.com/unihd-cag/skillbridge"');
    // Phones stack each row and show these labels instead of the header row.
    const rows = table!.match(/<tr>[\s\S]*?<\/tr>/g)!.slice(1);
    expect(rows.length).toBeGreaterThan(3);
    for (const row of rows)
      expect(row.match(/data-label="(Virtuoso Bridge|skillbridge)"/g)).toHaveLength(2);
  });

  it("serves the guide as an indexed page that replaces the README body", () => {
    const guide = pages[2];
    expect(guide).not.toContain('http-equiv="refresh"');
    expect(guide).not.toContain('content="noindex"');
    expect(guide).toContain(
      '<link rel="canonical" href="https://virtuoso-bridge.tokenzhang.com/guide/" />',
    );
    for (const id of ["install", "config", "cli", "agents", "cite"])
      expect(guide).toContain(`<section class="block" id="${id}">`);
    expect(read("sitemap.xml")).toContain("/guide/");
  });
});
