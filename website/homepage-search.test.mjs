import { describe, expect, it } from "vitest";
import { matchesQuery } from "../docs/assets/home/home.mjs";

describe("ecosystem keyword search", () => {
  const content = "PKU-IDEA/PANDA LLM analog design to layout LLM 模拟设计到版图 Agents and design projects 智能体与设计项目";

  it("matches names and descriptions in either language, ignoring case", () => {
    for (const query of ["panda", "PKU-idea", "analog", "版图", "智能体"])
      expect(matchesQuery(content, query)).toBe(true);
    expect(matchesQuery(content, "nonexistent")).toBe(false);
  });

  it("requires all keywords and treats punctuation as literal text", () => {
    expect(matchesQuery(content, "  PANDA   layout ")).toBe(true);
    expect(matchesQuery(content, "PANDA spectre")).toBe(false);
    expect(matchesQuery(content, ".*")).toBe(false);
    expect(matchesQuery(content, "[")).toBe(false);
  });

  it("shows all entries for an empty or whitespace query", () => {
    expect(matchesQuery(content, "")).toBe(true);
    expect(matchesQuery(content, "  ")).toBe(true);
  });
});
