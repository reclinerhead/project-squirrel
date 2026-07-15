// The data-access seam (issue #116). Components import ONLY this module for
// library data -- never fixtures.ts directly -- so wiring the real catalog
// (epic #115 Phase 0) means reimplementing these functions against HTTP
// routes and touching nothing above them. Synchronous today because fixtures
// are in-memory; the real versions go async, which is why callers already
// treat results as opaque snapshots rather than live references.

import { ARTISTS, TOP_TRACKS } from "./fixtures";
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
