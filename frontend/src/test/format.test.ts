import { describe, expect, it } from "vitest";

import { compact, day, money, pct } from "@/lib/format";

describe("format", () => {
  it("formats signed percentages", () => {
    expect(pct(0.1234)).toBe("+12.3%");
    expect(pct(-0.05)).toBe("-5.0%");
    expect(pct(null)).toBe("—");
    expect(pct(Number.NaN)).toBe("—");
  });
  it("formats money and compact values", () => {
    expect(money(1234.5)).toBe("$1,234.50");
    expect(compact(4.6e10)).toBe("$46.00B");
    expect(compact(-2.5e6)).toBe("-$2.5M");
    expect(money(undefined)).toBe("—");
  });
  it("renders calendar dates without time-zone shifting", () => {
    expect(day("2026-01-01")).toContain("2026");
    expect(day("2026-01-01")).toMatch(/Jan|1/);
  });
});
