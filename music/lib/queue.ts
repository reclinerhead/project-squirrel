// --- Pure queue shaping (unit-tested in queue.test.ts) ---
// The player holds one flat sequence plus a cursor; everything the queue
// panel shows (history / current / next up) is a view over that pair. The
// one mutation v1 allows is removing an UPCOMING track -- user-driven edits
// are fine (epic #115's non-goal is agent-driven rewriting); history and the
// playing track are never touched by any operation here.

import type { Track } from "./types";

export type QueueView = {
  history: Track[];
  current: Track | null;
  upNext: Track[];
};

export function queueView(sequence: Track[], currentIndex: number): QueueView {
  if (currentIndex < 0 || currentIndex >= sequence.length) {
    return { history: [], current: null, upNext: [] };
  }
  return {
    history: sequence.slice(0, currentIndex),
    current: sequence[currentIndex],
    upNext: sequence.slice(currentIndex + 1),
  };
}

/** Remove the up-next track at `upNextIndex` (an index INTO the upNext view,
 * not the sequence). Out-of-range indexes return the sequence unchanged --
 * a stale click on a row that just advanced must be a no-op, not a splice
 * of the wrong track. */
export function removeUpcoming(
  sequence: Track[],
  currentIndex: number,
  upNextIndex: number,
): Track[] {
  const target = currentIndex + 1 + upNextIndex;
  if (upNextIndex < 0 || target <= currentIndex || target >= sequence.length) {
    return sequence;
  }
  return [...sequence.slice(0, target), ...sequence.slice(target + 1)];
}

/** Fisher-Yates over ONLY the upcoming tracks, rng injected for testability.
 * History and the current track hold their positions -- shuffle changes what
 * comes next, never what already happened. */
export function shuffleUpcoming(
  sequence: Track[],
  currentIndex: number,
  rng: () => number,
): Track[] {
  const head = sequence.slice(0, currentIndex + 1);
  const tail = sequence.slice(currentIndex + 1);
  for (let i = tail.length - 1; i > 0; i--) {
    const j = Math.floor(rng() * (i + 1));
    [tail[i], tail[j]] = [tail[j], tail[i]];
  }
  return [...head, ...tail];
}
