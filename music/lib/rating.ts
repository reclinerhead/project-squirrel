// --- Pure four-level rating transitions (unit-tested in rating.test.ts) ---
// The epic's feedback model (-2/-1/+1/+2) rendered as a split thumb control:
// click sets +/-1, a second click on the same thumb escalates to +/-2, a third
// clears, and the opposite thumb always replaces. These transitions are
// contract, not decoration -- a strong-down becomes a hard WHERE clause in
// Phase 3, so the state machine that mints one gets an exhaustive table test.

import type { Rating } from "./types";

export type ThumbClick = "up" | "down";

export function nextRating(current: Rating, click: ThumbClick): Rating {
  if (click === "up") {
    if (current === 1) return 2; // escalate
    if (current === 2) return 0; // third click clears
    return 1; // 0 or any down -> replace with +1
  }
  if (current === -1) return -2; // escalate
  if (current === -2) return 0; // third click clears
  return -1; // 0 or any up -> replace with -1
}
