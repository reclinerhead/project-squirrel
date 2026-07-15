// --- Pure library search (unit-tested in search.test.ts) ---
// Everything-search over an injected corpus: artists, albums, tracks, grouped
// and ranked. Scoring is deliberately simple -- exact > prefix > word-prefix >
// substring -- because at ~27k tracks a legible ranking beats a clever one,
// and the corpus is injected so the same function serves fixtures today and
// catalog rows later.

import type { Album, Artist, Track } from "./types";

export type SearchResults = {
  artists: Artist[];
  albums: Album[];
  tracks: Track[];
};

export const GROUP_CAPS = { artists: 4, albums: 4, tracks: 8 } as const;

/** 0 = no match; higher is better. Case- and whitespace-insensitive. */
export function matchScore(text: string, query: string): number {
  const t = text.toLowerCase().trim();
  const q = query.toLowerCase().trim();
  if (!q || !t) return 0;
  if (t === q) return 4;
  if (t.startsWith(q)) return 3;
  // any word boundary: "thaw" hits "North of the Thaw"
  if (t.split(/\s+/).some((w) => w.startsWith(q))) return 2;
  if (t.includes(q)) return 1;
  return 0;
}

function ranked<T>(items: T[], score: (item: T) => number, name: (item: T) => string, cap: number): T[] {
  return items
    .map((item) => ({ item, s: score(item) }))
    .filter((x) => x.s > 0)
    .sort((a, b) => b.s - a.s || name(a.item).localeCompare(name(b.item)))
    .slice(0, cap)
    .map((x) => x.item);
}

export function searchLibrary(artists: Artist[], query: string): SearchResults {
  const q = query.trim();
  if (q.length < 2) return { artists: [], albums: [], tracks: [] };

  const albums = artists.flatMap((a) => a.albums);
  const tracks = albums.flatMap((al) => al.tracks);

  return {
    artists: ranked(artists, (a) => matchScore(a.name, q), (a) => a.name, GROUP_CAPS.artists),
    albums: ranked(albums, (al) => matchScore(al.title, q), (al) => al.title, GROUP_CAPS.albums),
    // A track matches on its own title first, but an artist-name hit still
    // surfaces it (half-weight) -- searching "signal creek" should offer
    // tracks to play, not just a link to a page.
    tracks: ranked(
      tracks,
      (t) => Math.max(matchScore(t.title, q) * 2, matchScore(t.artist, q)),
      (t) => t.title,
      GROUP_CAPS.tracks,
    ),
  };
}
