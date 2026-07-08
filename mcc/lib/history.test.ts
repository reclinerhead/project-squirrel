import { describe, expect, it } from "vitest";
import {
  censusPeak,
  dayLabel,
  dayTotal,
  hoursPeak,
  runsNewestFirst,
  speciesInWindow,
  speciesOrder,
  stackDay,
  type TrainingRun,
} from "./history";

const run = (run_name: string): TrainingRun => ({
  run_name,
  map50: null,
  recall: null,
  map50_95: null,
  val_split: null,
  notes: null,
  metrics: null,
});

describe("speciesOrder", () => {
  it("puts known species in stack order, squirrel first", () => {
    expect(speciesOrder(["turkey", "squirrel"])).toEqual(["squirrel", "turkey"]);
  });
  it("appends unknown species alphabetically after the known ones", () => {
    expect(speciesOrder(["raccoon", "squirrel", "opossum"])).toEqual([
      "squirrel",
      "opossum",
      "raccoon",
    ]);
  });
});

describe("stackDay", () => {
  it("orders segments baseline-first and drops zero counts", () => {
    expect(stackDay({ chipmunk: 1, squirrel: 4, turkey: 0 })).toEqual([
      { species: "squirrel", n: 4 },
      { species: "chipmunk", n: 1 },
    ]);
  });
  it("returns [] for a quiet day", () => {
    expect(stackDay({})).toEqual([]);
  });
});

describe("speciesInWindow", () => {
  it("collects every species across the window, in stack order", () => {
    const census: import("./history").DayCensus[] = [
      { date: "2026-07-05", counts: { turkey: 1 } },
      { date: "2026-07-06", counts: { squirrel: 3 } },
      { date: "2026-07-07", counts: {} },
    ];
    expect(speciesInWindow(census)).toEqual(["squirrel", "turkey"]);
  });
});

describe("censusPeak", () => {
  it("finds the tallest day total", () => {
    const census: import("./history").DayCensus[] = [
      { date: "a", counts: { squirrel: 2 } },
      { date: "b", counts: { squirrel: 3, turkey: 2 } },
    ];
    expect(censusPeak(census)).toBe(5);
  });
  it("never returns less than 1 (no divide-by-zero scale)", () => {
    expect(censusPeak([{ date: "a", counts: {} }])).toBe(1);
    expect(censusPeak([])).toBe(1);
  });
});

describe("dayTotal", () => {
  it("sums a day's counts", () => {
    expect(dayTotal({ squirrel: 4, turkey: 2 })).toBe(6);
    expect(dayTotal({})).toBe(0);
  });
});

describe("dayLabel", () => {
  it("formats an ISO date as a short label", () => {
    expect(dayLabel("2026-07-06")).toBe("Jul 6");
    expect(dayLabel("2026-12-25")).toBe("Dec 25");
  });
});

describe("runsNewestFirst", () => {
  it("orders by numeric suffix descending (lineage, not leaderboard)", () => {
    const runs = [run("train-15"), run("train-18"), run("train-16")];
    expect(runsNewestFirst(runs).map((r) => r.run_name)).toEqual([
      "train-18",
      "train-16",
      "train-15",
    ]);
  });
  it("sorts double-digit above single-digit (numeric, not lexical)", () => {
    expect(runsNewestFirst([run("train-9"), run("train-18")])[0].run_name).toBe(
      "train-18",
    );
  });
  it("puts unparseable names last", () => {
    const names = runsNewestFirst([run("baseline"), run("train-15")]).map(
      (r) => r.run_name,
    );
    expect(names).toEqual(["train-15", "baseline"]);
  });
});

describe("hoursPeak", () => {
  it("finds the busiest hour's total", () => {
    expect(hoursPeak({ "9": { squirrel: 2 }, "17": { squirrel: 1, turkey: 3 } })).toBe(4);
  });
  it("floors at 1 for an empty day", () => {
    expect(hoursPeak({})).toBe(1);
  });
});
