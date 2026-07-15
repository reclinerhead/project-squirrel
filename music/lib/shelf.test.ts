import { describe, expect, it } from "vitest";
import { recentlyAdded, recentlyPlayed, rediscovery, SHELF_CAP } from "./shelf";
import type { Album } from "./types";

function album(id: string, year: number): Album {
  return { id, title: id, artistId: "a", artist: "A", year, genre: "Rock", tracks: [] };
}

const LIB: Album[] = Array.from({ length: 40 }, (_, i) => album(`al-${i}`, 1980 + i));

describe("recentlyAdded", () => {
  it("returns newest first, capped", () => {
    const r = recentlyAdded(LIB, 5);
    expect(r.map((a) => a.year)).toEqual([2019, 2018, 2017, 2016, 2015]);
  });

  it("breaks year ties by title so the order is stable", () => {
    const r = recentlyAdded([album("b", 2000), album("a", 2000), album("c", 1999)], 2);
    expect(r.map((a) => a.id)).toEqual(["a", "b"]);
  });
});

describe("recentlyPlayed", () => {
  it("preserves the caller's most-recent-first order", () => {
    const r = recentlyPlayed(LIB, ["al-7", "al-2", "al-30"]);
    expect(r.map((a) => a.id)).toEqual(["al-7", "al-2", "al-30"]);
  });

  it("skips ids the catalog no longer has -- history outlives rebuilds", () => {
    const r = recentlyPlayed(LIB, ["gone", "al-3", "also-gone", "al-4"]);
    expect(r.map((a) => a.id)).toEqual(["al-3", "al-4"]);
  });

  it("caps", () => {
    const r = recentlyPlayed(LIB, LIB.map((a) => a.id), 3);
    expect(r.length).toBe(3);
  });
});

describe("rediscovery", () => {
  const recent = new Set(["al-0", "al-1", "al-2"]);

  it("is deterministic for a given date seed", () => {
    const a = rediscovery(LIB, recent, "2026-07-14");
    const b = rediscovery(LIB, recent, "2026-07-14");
    expect(a.map((x) => x.id)).toEqual(b.map((x) => x.id));
  });

  it("changes across days", () => {
    const a = rediscovery(LIB, recent, "2026-07-14");
    const b = rediscovery(LIB, recent, "2026-07-15");
    expect(a.map((x) => x.id)).not.toEqual(b.map((x) => x.id));
  });

  it("never surfaces a recently played album -- hard filter, not a weight", () => {
    for (const seed of ["2026-07-14", "2026-07-15", "2026-07-16", "2026-12-25"]) {
      const ids = rediscovery(LIB, recent, seed).map((x) => x.id);
      for (const r of recent) expect(ids).not.toContain(r);
    }
  });

  it("caps at SHELF_CAP by default and degrades on small catalogs", () => {
    expect(rediscovery(LIB, recent, "2026-07-14").length).toBe(SHELF_CAP);
    const tiny = LIB.slice(0, 4);
    expect(rediscovery(tiny, new Set(["al-0"]), "2026-07-14").length).toBe(3);
    expect(rediscovery([], new Set(), "2026-07-14")).toEqual([]);
  });

  it("does not mutate the input", () => {
    const before = LIB.map((a) => a.id);
    rediscovery(LIB, recent, "2026-07-14");
    expect(LIB.map((a) => a.id)).toEqual(before);
  });
});
