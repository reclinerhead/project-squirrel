import { describe, expect, it } from "vitest";
import { eventClock, eventLine, sortedCounts, visitLength } from "./daemon";

describe("eventClock", () => {
  it("extracts HH:MM:SS from an ISO timestamp", () => {
    expect(eventClock("2026-07-05T14:31:02")).toBe("14:31:02");
  });
  it("passes through a value with no time part", () => {
    expect(eventClock("whenever")).toBe("whenever");
  });
});

describe("eventLine", () => {
  it("summarizes a crowd with its species mix, sorted", () => {
    expect(
      eventLine({
        ts: "t",
        kind: "crowd_snapshot",
        details: { total: 6, counts: { squirrel: 5, chipmunk: 1 } },
      }),
    ).toBe("crowd of 6 — 1 chipmunk, 5 squirrel");
  });
  it("handles a crowd event with missing details", () => {
    expect(eventLine({ ts: "t", kind: "crowd_snapshot", details: null })).toBe(
      "crowd of ?",
    );
  });
  it("describes a hard-frame save with its box count", () => {
    expect(
      eventLine({ ts: "t", kind: "hard_frame_saved", details: { boxes: 4 } }),
    ).toBe("hard frame banked (4 boxes pre-labeled)");
  });
  it("announces an arrival by species", () => {
    expect(
      eventLine({
        ts: "t",
        kind: "arrival",
        details: { species: "turkey", count: 1 },
      }),
    ).toBe("turkey arrived");
  });
  it("notes the head-count when more of a species arrive", () => {
    expect(
      eventLine({
        ts: "t",
        kind: "arrival",
        details: { species: "squirrel", count: 3 },
      }),
    ).toBe("squirrel arrived (3 now)");
  });
  it("announces a full departure with the visit length", () => {
    expect(
      eventLine({
        ts: "t",
        kind: "departure",
        details: { species: "chipmunk", count: 0, duration_s: 61.6 },
      }),
    ).toBe("chipmunk left after 62s");
  });
  it("announces a partial departure with who's left", () => {
    expect(
      eventLine({
        ts: "t",
        kind: "departure",
        details: { species: "squirrel", count: 1 },
      }),
    ).toBe("squirrel left (1 still here)");
  });
  it("handles a departure with no duration", () => {
    expect(eventLine({ ts: "t", kind: "departure", details: null })).toBe(
      "critter left",
    );
  });
  it("falls back to a humanized kind for unknown events", () => {
    expect(eventLine({ ts: "t", kind: "clip_recorded", details: null })).toBe(
      "clip recorded",
    );
  });
});

describe("visitLength", () => {
  it("stays in seconds under two minutes", () => {
    expect(visitLength(61.6)).toBe("62s");
  });
  it("switches to minutes for longer visits", () => {
    expect(visitLength(150)).toBe("3m");
    expect(visitLength(1800)).toBe("30m");
  });
  it("switches to hours for marathon visits", () => {
    expect(visitLength(7200)).toBe("2.0h");
  });
});

describe("sortedCounts", () => {
  it("orders by count desc, then name", () => {
    expect(sortedCounts({ turkey: 2, squirrel: 8, chipmunk: 2 })).toEqual([
      ["squirrel", 8],
      ["chipmunk", 2],
      ["turkey", 2],
    ]);
  });
  it("returns [] for no counts", () => {
    expect(sortedCounts({})).toEqual([]);
  });
});
