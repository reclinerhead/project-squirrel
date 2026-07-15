import { describe, expect, it } from "vitest";
import { alphaBucket, byName, byNewest, lettersPresent, pageForLetter, paginate, sortKey } from "./browse";

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

describe("paginate", () => {
  it("slices interior and final pages correctly", () => {
    expect(paginate(150, 1, 60)).toEqual({ page: 1, pages: 3, start: 0, end: 60 });
    expect(paginate(150, 3, 60)).toEqual({ page: 3, pages: 3, start: 120, end: 150 });
  });

  it("clamps out-of-range and nonsense pages", () => {
    expect(paginate(150, 99, 60).page).toBe(3);
    expect(paginate(150, 0, 60).page).toBe(1);
    expect(paginate(150, NaN, 60).page).toBe(1);
  });

  it("an empty catalog is one empty page, not zero pages", () => {
    expect(paginate(0, 1, 60)).toEqual({ page: 1, pages: 1, start: 0, end: 0 });
  });

  it("an exact multiple doesn't mint a phantom page", () => {
    expect(paginate(120, 2, 60).pages).toBe(2);
  });
});

describe("lettersPresent / pageForLetter", () => {
  const names = ["Anchor", "Beacon", "The Beacons", "Comet", "2 Fast"];

  it("lists only letters that exist, '#' last", () => {
    expect(lettersPresent(names)).toEqual(["A", "B", "C", "#"]);
  });

  it("finds the page holding a letter's first entry", () => {
    const sorted = ["Anchor", "Apple", "Beacon", "Comet", "Delta", "Echo"];
    expect(pageForLetter(sorted, "A", 2)).toBe(1);
    expect(pageForLetter(sorted, "B", 2)).toBe(2);
    expect(pageForLetter(sorted, "E", 2)).toBe(3);
  });

  it("returns -1 for an absent letter -- a stale click goes nowhere", () => {
    expect(pageForLetter(["Anchor"], "Q", 2)).toBe(-1);
  });
});
