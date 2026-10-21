import { readFileSync, existsSync } from "node:fs";
import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";
import inventory from "./homepage-content.json";

const html = readFileSync(new URL("../docs/index.html", import.meta.url), "utf8");

describe("archive production homepage", () => {
  it("preserves the pre-redesign content and every original destination", () => {
    const links = [...html.matchAll(/<a\b[^>]*href="([^"]+)"/g)].map(m => m[1]);
    for (const href of inventory.links) expect(links).toContain(href);
    for (const [id, digest] of Object.entries(inventory.sections)) {
      const target = id === "ecosystem" ? "ecosystem-index" : id;
      const section = html.match(new RegExp(`<section[^>]*id="${target}"[\\s\\S]*?</section>`))?.[0];
      expect(section).toBeTruthy();
      const text = section!.replace(/<span class="section-no">\d+<\/span>/g, "").replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim();
      expect(createHash("sha256").update(text).digest("hex")).toBe(digest);
    }
  });

  it("keeps section order, unique anchors and the complete project index", () => {
    const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]);
    expect(new Set(ids).size).toBe(ids.length);
    const order = ["skillbridge", "start", "docs", "ecosystem"].map(id => ids.indexOf(id));
    expect(order.every(i => i >= 0)).toBe(true);
    expect(order).toEqual([...order].sort((a,b) => a-b));
    const index = html.match(/<section class="block ecosystem" id="ecosystem-index">[\s\S]*?<\/section>/)![0];
    expect(index.match(/<li><a href="https:\/\/github.com\//g)).toHaveLength(53);
    for (const [, id] of html.matchAll(/href="#([^"]+)"/g)) expect(ids).toContain(id);
  });

  it("ships official metadata, existing analytics and self-contained assets", () => {
    expect(html).toContain("<title>Virtuoso Bridge — AI agents for Cadence Virtuoso and Spectre</title>");
    expect(html).toContain('src="/analytics/track.js?');
    expect(html).toContain('rel="canonical" href="https://virtuoso-bridge.tokenzhang.com/"');
    expect(html).not.toMatch(/Design preview|(?:src|href)="https?:\/\/(?:localhost|127\.0\.0\.1)|prototypes\//);
    for (const [, asset] of html.matchAll(/(?:src|href)="(\/assets\/home\/[^"?]+)(?:\?[^" ]*)?"/g)) {
      expect(existsSync(new URL(`../docs${asset}`, import.meta.url))).toBe(true);
    }
    expect(html).toContain('class="hero-flow"');
    for (const label of ["AI agent", "VIRTUOSO", "BRIDGE", "Virtuoso", "Maestro", "Spectre"])
      expect(html).toContain(label);
    // Keep the original reusable illustration intact for docs and social identity.
    expect(createHash("sha256").update(readFileSync(new URL("../docs/assets/architecture.svg", import.meta.url), "utf8").replace(/\r\n/g, "\n")).digest("hex"))
      .toBe("3f33a9abb147ab9c3f3c75cac967b9d95f809819a7492fa4a9b2941a72e59233");
    expect(html).not.toMatch(/id="(?:lab|architecture)"|dossier\.css/);
  });

  it("makes the Lite core explicit and keeps the index open without JavaScript", () => {
    const hero = html.match(/<section class="intro wrap"[\s\S]*?<\/section>/)![0];
    expect(hero).toContain("Spectre simulation and PDK/CDF callbacks");
    expect(hero).toContain("Spectre 仿真与 PDK/CDF 回调");
    expect(hero).toContain("Raw SKILL for the rest.");
    expect(hero).toContain("默认极简，高级配置按需开启");
    expect(html).toContain('<section class="block ecosystem" id="ecosystem-index">');
    expect(html).toContain('<details id="full-index" open>');
    expect(html).toContain('<section hidden class="archive"');
    expect(html).toContain('<div id="eco-search-controls" hidden>');
    expect(html).toContain('aria-controls="ecosystem-index"');
    expect(html).toContain('id="search-status" role="status"');
    for (const id of ["forks", "tools", "agents", "skills", "mentions"]) {
      expect(html).toContain(`href="#eco-${id}"`);
      expect(html).toContain(`<h3 id="eco-${id}">`);
    }
  });
});
