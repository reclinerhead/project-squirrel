import { describe, expect, it } from "vitest";
import { coverParams, hashString } from "./cover";

describe("hashString", () => {
  it("is stable across calls and inputs differ", () => {
    expect(hashString("gravel-static")).toBe(hashString("gravel-static"));
    expect(hashString("gravel-static")).not.toBe(hashString("one-bar"));
  });

  it("is always a non-negative 32-bit integer", () => {
    for (const s of ["", "a", "gravel-static", "北", "🎵"]) {
      const h = hashString(s);
      expect(h).toBeGreaterThanOrEqual(0);
      expect(h).toBeLessThanOrEqual(0xffffffff);
      expect(Number.isInteger(h)).toBe(true);
    }
  });
});

describe("coverParams", () => {
  it("is deterministic -- the same album looks the same everywhere", () => {
    expect(coverParams("gravel-static")).toEqual(coverParams("gravel-static"));
  });

  it("stays inside its ranges", () => {
    for (const id of ["gravel-static", "one-bar", "under-glass", "plumage", "x"]) {
      const p = coverParams(id);
      expect(p.hue1).toBeGreaterThanOrEqual(0);
      expect(p.hue1).toBeLessThan(360);
      expect(p.hue2).toBeGreaterThanOrEqual(0);
      expect(p.hue2).toBeLessThan(360);
      expect(p.pattern).toBeGreaterThanOrEqual(0);
      expect(p.pattern).toBeLessThan(4);
    }
  });

  it("keeps the two hues at least 90 degrees apart on the circle", () => {
    for (const id of ["gravel-static", "one-bar", "under-glass", "plumage", "last-set"]) {
      const { hue1, hue2 } = coverParams(id);
      const d = Math.abs(hue1 - hue2);
      const circular = Math.min(d, 360 - d);
      expect(circular).toBeGreaterThanOrEqual(90);
    }
  });
});
