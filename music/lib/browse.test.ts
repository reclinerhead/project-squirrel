import { describe, expect, it } from "vitest";
import { alphaBucket, byName, byNewest, clampWindow, indexForLetter, lettersPresent, sortKey } from "./browse";

describe("sortKey / alphaBucket", () => {
  it("drops a leading 'The ' -- record-store filing", () => {
    expect(sortKey("The Cold Frame")).toBe("cold frame");
    expect(alphaBucket("The Cold Frame")).toBe("C");
  });

  it("keeps 'A'/'An' and mid-name 'the' intact", () => {
    expect(sortKey("An Horse")).toBe("an horse");
    expect(sortKey("North of the Thaw")).toBe("north of the thaw");
    expect(sortKey("Theory of Light")).toBe("theory of light"); // 'The' + space only
  });

  it("buckets digits, symbols, and empties under '#'", () => {
    expect(alphaBucket("2 Fast")).toBe("#");
    expect(alphaBucket("...And Stars")).toBe("#");
    expect(alphaBucket("")).toBe("#");
  });
});

describe("comparators", () => {
  it("byName sorts by the key, ignoring 'The '", () => {
    const names = ["The Zebra Room", "Anchor & Fern", "The Beacons"];
    const sorted = names.slice().sort(byName((n) => n));
    expect(sorted).toEqual(["Anchor & Fern", "The Beacons", "The Zebra Room"]);
  });

  it("byNewest sorts year desc, name asc within a year", () => {
    const items = [
      { t: "B", y: 2020 },
      { t: "A", y: 2020 },
      { t: "C", y: 2024 },
    ];
    const sorted = items.slice().sort(byNewest((x) => x.y, (x) => x.t));
    expect(sorted.map((x) => x.t)).toEqual(["C", "A", "B"]);
  });
});

describe("clampWindow", () => {
  it("slices the first and an interior window", () => {
    expect(clampWindow(150, 0, 60)).toEqual({ start: 0, end: 60, nextOffset: 60 });
    expect(clampWindow(150, 60, 60)).toEqual({ start: 60, end: 120, nextOffset: 120 });
  });

  it("reports nextOffset null only when the window reaches the end", () => {
    expect(clampWindow(150, 120, 60)).toEqual({ start: 120, end: 150, nextOffset: null });
    // the trap this guards: a final window that happens to be exactly full
    expect(clampWindow(120, 60, 60)).toEqual({ start: 60, end: 120, nextOffset: null });
  });

  it("clamps offsets past the end and nonsense input", () => {
    expect(clampWindow(150, 999, 60)).toEqual({ start: 150, end: 150, nextOffset: null });
    expect(clampWindow(150, -5, 60).start).toBe(0);
    expect(clampWindow(150, NaN, 60).start).toBe(0);
    expect(clampWindow(150, 0, 0).end).toBe(60); // limit 0 falls back, never an empty window forever
  });

  it("an empty catalog yields an empty terminal window", () => {
    expect(clampWindow(0, 0, 60)).toEqual({ start: 0, end: 0, nextOffset: null });
  });
});

describe("lettersPresent / indexForLetter", () => {
  const names = ["Anchor", "Beacon", "The Beacons", "Comet", "2 Fast"];

  it("lists only letters that exist, '#' last", () => {
    expect(lettersPresent(names)).toEqual(["A", "B", "C", "#"]);
  });

  it("finds the offset of a letter's first entry", () => {
    const sorted = ["Anchor", "Apple", "Beacon", "Comet", "Delta", "Echo"];
    expect(indexForLetter(sorted, "A")).toBe(0);
    expect(indexForLetter(sorted, "B")).toBe(2);
    expect(indexForLetter(sorted, "E")).toBe(5);
  });

  it("falls back to the top for an absent letter -- a stale click lands somewhere sane", () => {
    expect(indexForLetter(["Anchor"], "Q")).toBe(0);
  });
});
