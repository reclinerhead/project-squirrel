// --- Pure shelf selection (unit-tested in shelf.test.ts) ---
// The home page's shelves (issue #118): each is a bounded, deterministic
// selection over the catalog. No clocks in here -- the date seed and the
// "what counts as recent" judgment are injected by the caller (the server
// boundary today, the play_history-backed API later), so these functions are
// the same under test, under fixtures, and under the real catalog.

import { hashString } from "./cover";
import type { Album } from "./types";

export const SHELF_CAP = 14;

/** Newest first. Fixture stand-in orders by release year; the real catalog
 * orders by ingest timestamp -- same shape, better clock. */
export function recentlyAdded(albums: Album[], cap: number = SHELF_CAP): Album[] {
  return albums
    .slice()
    .sort((a, b) => b.year - a.year || a.title.localeCompare(b.title))
    .slice(0, cap);
}

/** Most recent first, in the order the caller's history says. Unknown ids
 * are skipped -- history outliving a catalog rebuild must not crash a shelf. */
export function recentlyPlayed(albums: Album[], playedOrder: string[], cap: number = SHELF_CAP): Album[] {
  const byId = new Map(albums.map((a) => [a.id, a]));
  const out: Album[] = [];
  for (const id of playedOrder) {
    const a = byId.get(id);
    if (a) out.push(a);
    if (out.length >= cap) break;
  }
  return out;
}

/** The signature shelf: a deterministic daily sample of albums NOT recently
 * played. Seeded by the date string (stable all day, fresh tomorrow) via the
 * same mulberry32 the fixtures use -- a seeded Fisher-Yates, then the cap.
 * Anti-recency is a hard filter, not a weight: the whole point is surfacing
 * what you've forgotten, so nothing recent may appear at all. */
export function rediscovery(
  albums: Album[],
  recentIds: ReadonlySet<string>,
  dateSeed: string,
  cap: number = SHELF_CAP,
): Album[] {
  const pool = albums.filter((a) => !recentIds.has(a.id));
  let s = hashString(dateSeed) >>> 0;
  const rng = () => {
    s |= 0;
    s = (s + 0x6d2b79f5) | 0;
    let t = Math.imul(s ^ (s >>> 15), 1 | s);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const deck = pool.slice();
  for (let i = deck.length - 1; i > 0; i--) {
    const j = Math.floor(rng() * (i + 1));
    [deck[i], deck[j]] = [deck[j], deck[i]];
  }
  return deck.slice(0, cap);
}
