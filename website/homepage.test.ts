import { readFileSync, existsSync } from "node:fs";
import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";
import inventory from "./homepage-content.json";

const html = readFileSync(new URL("../docs/index.html", import.meta.url), "utf8");

describe("archive production homepage", () => {
  it("preserves the pre-redesign content and every original destination", () => {
    const links = [...html.matchAll(/<a\b[^>]*href="([^"]+)"/g)].map(m => m[1]);
    const removedNavigation = ["https://chip-arena.com/", "https://circuits-and-systems.tokenzhang.com/"];
    for (const href of inventory.links.filter(link => !removedNavigation.includes(link))) expect(links).toContain(href === "/guide/#cite" ? "#cite" : href);
    for (const [id, digest] of Object.entries(inventory.sections)) {
      if (["ecosystem", "skillbridge"].includes(id)) continue; // Reorganized records are checked individually below.
      const target = id;
      const section = html.match(new RegExp(`<section[^>]*id="${target}"[\\s\\S]*?</section>`))?.[0];
      expect(section).toBeTruthy();
      const text = section!.replace(/>Example<\/a>/g, ">examples/</a>").replace(/href="#cite"/g, 'href="/guide/#cite" data-internal').replace(/<span class="section-no">\d+<\/span>/g, "").replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim();
      expect(createHash("sha256").update(text).digest("hex")).toBe(digest);
    }
  });

  it("preserves every comparison entry in the paired archive leaves", () => {
    const section = html.match(/<section class="block" id="skillbridge">[\s\S]*?<\/section>/)![0];
    const entries = [...section.matchAll(/<article class="file-value (primary|secondary)-value"[^>]*><h4[^>]*>([\s\S]*?)<\/h4><p>([\s\S]*?)<\/p><\/article>/g)];
    expect(entries).toHaveLength(14);
    inventory.comparison.rows.forEach((row, i) => {
      expect(entries[i * 2].slice(1)).toEqual(["primary", row.heading, row.vb]);
      expect(entries[i * 2 + 1].slice(1)).toEqual(["secondary", row.heading, row.sb]);
    });
    expect(section).toContain(`<p>${inventory.comparison.intro}</p>`);
    expect(section).toContain(`<p class="note">${inventory.comparison.note}</p>`);
    expect(section).not.toContain("<small");
    expect(section).toContain('src="/assets/mark.svg?v=20261012-vb"');
  });

  it("keeps section order, unique anchors and the complete project wall", () => {
    const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]);
    expect(new Set(ids).size).toBe(ids.length);
    const order = ["skillbridge", "start", "docs", "ecosystem", "architecture", "cite"].map(id => ids.indexOf(id));
    expect(order.every(i => i >= 0)).toBe(true);
    expect(order).toEqual([...order].sort((a,b) => a-b));
    expect(html.match(/class="wf-record"/g)).toHaveLength(53);
    for (const group of inventory.projects) {
      expect(html).toContain(`id="${group.id}"`);
      for (const item of group.items) {
        const record = html.match(new RegExp(`<a class="wf-record" href="${item.url.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}"[\\s\\S]*?</a>`))?.[0];
        expect(record).toBeTruthy();
        expect(record).toContain(`aria-label="${item.name} — GitHub"`);
        expect(record).toContain(`<span class="en">${item.en}</span><span class="zh">${item.zh}</span>`);
      }
    }
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
    expect(html).not.toMatch(/id="lab"|dossier\.css/);
  });

  it("moves the two guide sections verbatim and numbers them 05 and 06", () => {
    for (const [id, original] of Object.entries(inventory.transferredSections)) {
      const section = html.match(new RegExp(`<section class="block" id="${id}">[\\s\\S]*?</section>`))![0];
      expect(section.replace(/\r\n/g, "\n").replace(/<span class="section-no">0[56]<\/span>/, "")).toBe(original);
    }
    expect(html.match(/<footer[\s\S]*?<\/footer>/)![0]).not.toContain('>Cite<');
  });

  it("makes the Lite core explicit and keeps all project cards available without JavaScript", () => {
    const hero = html.match(/<section class="intro wrap"[\s\S]*?<\/section>/)![0];
    expect(hero).toContain("Spectre simulation and PDK/CDF callbacks");
    expect(hero).toContain("Spectre 仿真与 PDK/CDF 回调");
    expect(hero).toContain("Raw SKILL for the rest.");
    expect(hero).toContain("默认极简，高级配置按需开启");
    expect(html).toContain('<section class="archive" data-waterfall');
    expect(html).not.toMatch(/id="(?:full-index|ecosystem-index|search|search-status|index-clear)"|class="wf-detail"/);
    expect(html.match(/data-original/g)).toHaveLength(5);
    for (const id of ["forks", "tools", "agents", "skills", "mentions"]) expect(html).toContain(`href="#eco-${id}"`);
  });
});
