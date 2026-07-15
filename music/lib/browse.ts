// --- Pure browse mechanics (unit-tested in browse.test.ts) ---
// Windowing, alphabetical bucketing, and sort comparators for the full-
// catalog pages (issue #118). The design constraint everything here serves:
// the browser never holds more than what's been scrolled to, no matter how
// big the catalog gets.
//
// The window is offset+limit rather than page numbers because that's what the
// real catalog will speak: `LIMIT ? OFFSET ?` against SQLite. Infinite scroll
// asks for "the next N after what I have", which is an offset question, not a
// page question -- keeping the seam in offsets means Phase 0 swaps the fixture
// call for a query and the client never notices.

export const PAGE_LIMIT = 60;

/** Sort key: case-folded, leading "The " dropped (so The Cold Frame files
 * under C -- the record-store convention, decided here and tested). "A"/"An"
 * are deliberately NOT dropped: band names like "An Horse" are rare enough
 * that guessing costs more than filing them under A. */
export function sortKey(name: string): string {
  const n = name.trim().toLowerCase();
  return n.startsWith("the ") ? n.slice(4) : n;
}

/** 'A'-'Z' from the sort key's first letter; everything else (digits,
 * symbols, empty) buckets under '#'. */
export function alphaBucket(name: string): string {
  const c = sortKey(name).charAt(0).toUpperCase();
  return c >= "A" && c <= "Z" ? c : "#";
}

export function byName<T>(name: (x: T) => string): (a: T, b: T) => number {
  return (a, b) => sortKey(name(a)).localeCompare(sortKey(name(b))) || name(a).localeCompare(name(b));
}

export function byNewest<T>(year: (x: T) => number, name: (x: T) => string): (a: T, b: T) => number {
  return (a, b) => year(b) - year(a) || sortKey(name(a)).localeCompare(sortKey(name(b)));
}

export type Window = {
  start: number; // slice start, clamped into [0, total]
  end: number; // slice end (exclusive)
  /** Offset to ask for next, or null when this window reaches the end --
   * the client's "am I done scrolling" answer, so it never guesses from
   * a short page. */
  nextOffset: number | null;
};

export function clampWindow(total: number, offset: number, limit: number = PAGE_LIMIT): Window {
  const lim = Math.max(1, Math.floor(limit) || PAGE_LIMIT);
  const start = Math.min(Math.max(0, Math.floor(offset) || 0), Math.max(0, total));
  const end = Math.min(start + lim, total);
  return { start, end, nextOffset: end < total ? end : null };
}

/** The letters actually present, in rail order ('#' last), from a list of
 * names -- the rail renders only these, so dead letters can't be clicked. */
export function lettersPresent(names: string[]): string[] {
  const set = new Set(names.map(alphaBucket));
  const letters = [...set].filter((l) => l !== "#").sort();
  return set.has("#") ? [...letters, "#"] : letters;
}

/** The index of `letter`'s first entry, given names ALREADY sorted with
 * byName -- i.e. the offset a letter jump starts the window at. 0 if the
 * letter has no items: the rail shouldn't have offered it, and a stale click
 * lands at the top rather than somewhere arbitrary. */
export function indexForLetter(sortedNames: string[], letter: string): number {
  const i = sortedNames.findIndex((n) => alphaBucket(n) === letter);
  return i === -1 ? 0 : i;
}
