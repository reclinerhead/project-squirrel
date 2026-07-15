// The data-access seam (issue #116). Components import ONLY this module for
// library data -- never fixtures.ts directly -- so wiring the real catalog
// (epic #115 Phase 0) means reimplementing these functions against HTTP
// routes and touching nothing above them. Synchronous today because fixtures
// are in-memory; the real versions go async, which is why callers already
// treat results as opaque snapshots rather than live references.

import { ARTISTS, PLAYED_ORDER, TOP_TRACKS } from "./fixtures";
import { byName, byNewest, paginate, PER_PAGE, type Page } from "./browse";
import { recentlyAdded, recentlyPlayed, rediscovery } from "./shelf";
import { searchLibrary, type SearchResults } from "./search";
import type { Album, Artist, Output, Track } from "./types";

export function listArtists(): Artist[] {
  return ARTISTS;
}

export function getArtist(id: string): Artist | null {
  return ARTISTS.find((a) => a.id === id) ?? null;
}

export function getAlbum(id: string): Album | null {
  for (const a of ARTISTS) {
    const al = a.albums.find((x) => x.id === id);
    if (al) return al;
  }
  return null;
}

/** Fixture-curated stand-in for the play_history ranking Phase 2 starts
 * collecting. Missing ids are skipped defensively rather than crashing the
 * artist page over a fixture typo. */
export function getTopTracks(artistId: string): Track[] {
  const artist = getArtist(artistId);
  if (!artist) return [];
  const byId = new Map(artist.albums.flatMap((al) => al.tracks).map((t) => [t.id, t]));
  return (TOP_TRACKS[artistId] ?? [])
    .map((id) => byId.get(id))
    .filter((t): t is Track => t !== undefined);
}

export function search(query: string): SearchResults {
  return searchLibrary(ARTISTS, query);
}

// --- browse + shelves (issue #118) ---

function allAlbums(): Album[] {
  return ARTISTS.flatMap((a) => a.albums);
}

/** Genres actually present in the library, alphabetical -- the pill row.
 * Normalization is the catalog's job (epic #115 Phase 0/1); the UI renders
 * whatever this returns. */
export function listGenres(): string[] {
  return [...new Set(allAlbums().map((al) => al.genre))].sort();
}

export function libraryCounts(): { artists: number; albums: number; tracks: number } {
  const albums = allAlbums();
  return {
    artists: ARTISTS.length,
    albums: albums.length,
    tracks: albums.reduce((n, al) => n + al.tracks.length, 0),
  };
}

export type BrowseSort = "az" | "new";

export function browseAlbums(opts: {
  genre?: string;
  sort: BrowseSort;
  page: number;
  perPage?: number;
}): { items: Album[]; pageInfo: Page; total: number; names: string[] } {
  const per = opts.perPage ?? PER_PAGE;
  const pool = opts.genre ? allAlbums().filter((al) => al.genre === opts.genre) : allAlbums();
  const sorted = pool.sort(
    opts.sort === "az" ? byName<Album>((al) => al.title) : byNewest<Album>((al) => al.year, (al) => al.title),
  );
  const pageInfo = paginate(sorted.length, opts.page, per);
  return {
    items: sorted.slice(pageInfo.start, pageInfo.end),
    pageInfo,
    total: sorted.length,
    names: sorted.map((al) => al.title),
  };
}

export function browseArtists(opts: {
  sort: BrowseSort;
  page: number;
  perPage?: number;
}): { items: Artist[]; pageInfo: Page; total: number; names: string[] } {
  const per = opts.perPage ?? PER_PAGE;
  const sorted = ARTISTS.slice().sort(
    opts.sort === "az"
      ? byName<Artist>((a) => a.name)
      : byNewest<Artist>((a) => Math.max(...a.albums.map((al) => al.year)), (a) => a.name),
  );
  const pageInfo = paginate(sorted.length, opts.page, per);
  return {
    items: sorted.slice(pageInfo.start, pageInfo.end),
    pageInfo,
    total: sorted.length,
    names: sorted.map((a) => a.name),
  };
}

/** The home shelves. `dateSeed` comes from the server boundary (never from
 * client render -- the Date-in-render hydration trap). Recently-played reads
 * fixture recency today, play_history later; when it's empty the shelf is
 * simply absent. */
export function getShelves(dateSeed: string): {
  recentlyAdded: Album[];
  recentlyPlayed: Album[];
  rediscovery: Album[];
} {
  const albums = allAlbums();
  return {
    recentlyAdded: recentlyAdded(albums),
    recentlyPlayed: recentlyPlayed(albums, PLAYED_ORDER),
    rediscovery: rediscovery(albums, new Set(PLAYED_ORDER), dateSeed),
  };
}

/** Phase 2's three confirmed playback targets, verbatim from epic #115. */
export function listOutputs(): Output[] {
  return [
    { id: "browser", name: "This browser", kind: "browser" },
    { id: "denon-x4000", name: "Denon AVR-X4000 · living room", kind: "dlna" },
    { id: "lg-c2", name: "LG C2 · basement", kind: "dlna" },
  ];
}

/** The demo seed: the app opens with an album mid-listen, paused -- so the
 * bar, the queue's history section, and the now-playing highlight all have
 * something to show without a click. */
export function getSeedQueue(): { sequence: Track[]; currentIndex: number; playingFrom: string } {
  const album = getAlbum("gravel-static");
  if (!album) return { sequence: [], currentIndex: -1, playingFrom: "" };
  return { sequence: album.tracks, currentIndex: 2, playingFrom: album.title };
}
