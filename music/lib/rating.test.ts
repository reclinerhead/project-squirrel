import { describe, expect, it } from "vitest";
import { nextRating } from "./rating";
import type { Rating } from "./types";

// The full table, exhaustively: 5 states x 2 clicks. A strong-down becomes a
// hard filter in Phase 3, so every one of these transitions is load-bearing.
const TABLE: [Rating, "up" | "down", Rating][] = [
  // click up: set, escalate, clear, replace
  [0, "up", 1],
  [1, "up", 2],
  [2, "up", 0],
  [-1, "up", 1],
  [-2, "up", 1],
  // click down, mirrored
  [0, "down", -1],
  [-1, "down", -2],
  [-2, "down", 0],
  [1, "down", -1],
  [2, "down", -1],
];

describe("nextRating", () => {
  it.each(TABLE)("from %d, thumb-%s -> %d", (from, click, to) => {
    expect(nextRating(from, click)).toBe(to);
  });

  it("reaches all four non-zero levels from unrated in <= 2 clicks", () => {
    expect(nextRating(nextRating(0, "up"), "up")).toBe(2);
    expect(nextRating(nextRating(0, "down"), "down")).toBe(-2);
  });
});
