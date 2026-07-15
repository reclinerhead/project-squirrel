// --- Pure browse mechanics (unit-tested in browse.test.ts) ---
// Pagination, alphabetical bucketing, and sort comparators for the full-
// catalog pages (issue #118). The design constraint everything here serves:
// no page ever renders more than one page-worth of cards, no matter how big
// the catalog gets.

export const PER_PAGE = 60;

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

export type Page = {
  page: number; // clamped, 1-based
  pages: number; // >= 1
  start: number; // slice start
  end: number; // slice end (exclusive)
};

export function paginate(total: number, requestedPage: number, perPage: number = PER_PAGE): Page {
  const pages = Math.max(1, Math.ceil(total / perPage));
  const page = Math.min(Math.max(1, Math.floor(requestedPage) || 1), pages);
  const start = (page - 1) * perPage;
  return { page, pages, start, end: Math.min(start + perPage, total) };
}

/** The letters actually present, in rail order ('#' last), from a list of
 * names -- the rail renders only these, so dead letters can't be clicked. */
export function lettersPresent(names: string[]): string[] {
  const set = new Set(names.map(alphaBucket));
  const letters = [...set].filter((l) => l !== "#").sort();
  return set.has("#") ? [...letters, "#"] : letters;
}

/** Which page (1-based) holds the first item of `letter`, given names ALREADY
 * sorted with byName. -1 if the letter has no items -- the rail shouldn't
 * have offered it, but a stale click must not navigate anywhere wrong. */
export function pageForLetter(sortedNames: string[], letter: string, perPage: number = PER_PAGE): number {
  const i = sortedNames.findIndex((n) => alphaBucket(n) === letter);
  return i === -1 ? -1 : Math.floor(i / perPage) + 1;
}
