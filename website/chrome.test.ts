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


        "https://www.tokenzhang.com/",
        'href="/guide/" data-internal',
        'href="/privacy/"',
        'href="/analytics/"',
        "Analog Canvas",

        "data-visitors",
        "data-views",
      ])
        expect(page).toContain(target);
      expect(page).not.toContain("related-nav");
      expect(page).not.toContain("footer-brand");
      expect(page).not.toContain('v=20261012"');
    }
  });

  it("places Docs beside the brand and only Analog Canvas and the author on the right", () => {
    for (const page of pages) {
      const left = page.match(/<div class="header-left">[\s\S]*?<\/div>/)![0];
      const nav = page.match(/<nav class="header-nav"[^>]*>[\s\S]*?<\/nav>/)![0];
      expect(left).toContain('class="brand"');
      expect(left).toContain('class="nav-guide"');
      expect(nav.match(/<a /g)).toHaveLength(2);
      expect(nav).toContain('Analog Canvas');
      expect(nav).toContain('Presented by');
      expect(nav).toContain('TokenZhang');
      expect(page).not.toMatch(/chip-arena.com|circuits-and-systems.tokenzhang.com|related-label/);
      for (const link of nav.match(/<a [\s\S]*?<\/a>/g) ?? []) {
        expect(link).toContain('aria-label="');
        expect(link).toContain('title="');
        expect(link).toContain('target="_blank" rel="noopener"');
      }
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
    const h1 = pages[0].match(/<h1\b[^>]*>[\s\S]*?<\/h1>/)?.[0] ?? "";
    const text = h1.replace(/<[^>]+>/g, "").replace(/\s+/g, " ");
    for (const line of ["Your agent.", "Inside Virtuoso.", "让智能体，", "走进 Virtuoso。"])
      expect(text).toContain(line);
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
    const section = landing.match(/<section class="archive" data-waterfall[\s\S]*?<\/section>/)?.[0];
    expect(section).toBeTruthy();
    const repos = [...section!.matchAll(/<a class="wf-record" href="https:\/\/github\.com\/([^"]+)"/g)].map((m) => m[1]);
    expect(repos.length).toBeGreaterThan(40);
    expect(new Set(repos).size).toBe(repos.length);
    // Same-author projects stay out of the list.
    for (const repo of repos) expect(repo).not.toMatch(/^(Arcadia-1|cascode-ai)\//);
    // It is the last section on the page.
    expect(landing.indexOf('id="ecosystem"')).toBeGreaterThan(landing.indexOf('id="docs"'));
  });

  it("labels all comparison entries with their criterion and product", () => {
    const section = pages[0].match(/<section class="block" id="skillbridge">[\s\S]*?<\/section>/)![0];
    expect(section).toContain('href="https://github.com/unihd-cag/skillbridge"');
    expect(section.match(/class="dossier-pair"/g)).toHaveLength(7);
    for (let i = 0; i < 7; i++) {
      expect(section).toContain(`aria-labelledby="criterion-${i}-primary vb-heading"`);
      expect(section).toContain(`aria-labelledby="criterion-${i}-secondary sb-heading"`);
    }
  });

  it("serves the guide as an indexed page that replaces the README body", () => {
    const guide = pages[2];
    expect(guide).not.toContain('http-equiv="refresh"');
    expect(guide).not.toContain('content="noindex"');
    expect(guide).toContain(
      '<link rel="canonical" href="https://virtuoso-bridge.tokenzhang.com/guide/" />',
    );
    for (const id of ["install", "config", "cli", "agents"])
      expect(guide).toContain(`<section class="block" id="${id}">`);
    expect(read("sitemap.xml")).toContain("/guide/");
  });
});
