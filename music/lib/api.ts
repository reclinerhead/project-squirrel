// The data-access seam (issue #116), now against the real catalog (issue
// #129). Server-only and async: every function opens music.db per request
// via lib/db.ts, shapes rows through lib/catalog-rows.ts, and runs the SAME
// pure sort/window/rail/shelf helpers the fixture era used -- so the ordering
// the UI shipped with, and the tests that pin it, carry over unchanged.
//
// Client components no longer import this module (node:sqlite would fail
// their build, loudly and correctly): search goes through /api/search, the
// player through /api/player/*, the [id] pages get their data from server
// wrappers, and the seed queue arrives as a layout prop. fixtures.ts stays
// on disk for the pure-helper tests, feeding nothing.

import {
  byName,
  byNewest,
  clampWindow,
  indexForLetter,
  lettersPresent,
  PAGE_LIMIT,
} from "./browse";
import { searchLibrary, type SearchResults } from "./search";
import { recentlyAdded, recentlyPlayed, rediscovery } from "./shelf";
import {
  albumFromRow,
  albumIdOf,
  artistIdOf,
  decodeAlbumId,
  decodeArtistId,
  trackFromRow,
} from "./catalog-rows";
import {
  albumIndex,
  artistAlbums,
  artistArtMap,
  artistFor,
  hydrateAlbum,
  libraryTotals,
  recentlyPlayedPairs,
  searchTrackRows,
  albumNoteFor,
  artistBioFor,
  seedPair,
  topTrackRows,
  tracksForAlbum,
  withDb,
  type AlbumIndexEntry,
} from "./db";
import type { Album, Artist, ArtistBio, Track } from "./types";

export async function getArtist(id: string): Promise<Artist | null> {
  const name = decodeArtistId(id);
  if (name === null) return null;
  return withDb<Artist | null>(null, (db) => artistFor(db, name));
}

/** The album page's About panel (issue #170). Server-rendered alongside the
 * album so the panel is present or absent per page load with no client
 * pop-in -- the no-layout-shift rule. Takes the raw artist NAME, which the
 * album already carries, rather than re-decoding an id. */
export async function getArtistBio(name: string): Promise<ArtistBio | null> {
  return withDb<ArtistBio | null>(null, (db) => artistBioFor(db, name));
}

export async function getAlbum(id: string): Promise<Album | null> {
  const pair = decodeAlbumId(id);
  if (pair === null) return null;
  return withDb<Album | null>(null, (db) => {
    const tracks = tracksForAlbum(db, pair.artist, pair.album);
    if (tracks.length === 0) return null;
    const meta = albumIndex(db).find(
      (e) => e.artist === pair.artist && e.album === pair.album,
    );
    const note = albumNoteFor(db, pair.artist, pair.album);
    return {
      id,
      title: pair.album,
      artistId: artistIdOf(pair.artist),
      artist: pair.artist,
      year: meta?.year ?? 0,
      genre: meta?.genre || "Uncategorized",
      tracks,
      // The tracks already carry the album's art (one subquery, same rows);
      // falling to the index entry covers a trackless meta-only hit.
      artHash: tracks[0]?.artHash ?? meta?.art_hash ?? null,
      // The backdrop's crop anchor (issue #159) rides the index entry only
      // -- tracks don't carry it, and null honestly means "center".
      artFocalY: meta?.focal_y ?? null,
      // The album's own copy (issue #171). Resolved on the server half with
      // everything else, so the panel is part of the first paint.
      description: note?.description ?? null,
      descriptionSrc: note?.source ?? null,
    };
  });
}

/** play_history-ranked now, exactly as the fixture era's note promised.
 * Empty until listening accumulates -- the page already hides the section. */
export async function getTopTracks(artistId: string): Promise<Track[]> {
  const name = decodeArtistId(artistId);
  if (name === null) return [];
  return withDb<Track[]>([], (db) => topTrackRows(db, name).map(trackFromRow));
}

export async function search(query: string): Promise<SearchResults> {
  const none: SearchResults = { artists: [], albums: [], tracks: [] };
  if (query.trim().length < 2) return none;
  return withDb(none, (db) => {
    // A bounded candidate sweep, regrouped into the nested Artist shape the
    // tested scorer ranks. The overlay renders names and covers off these,
    // never track counts, so the partial albums are honest.
    const tracks = searchTrackRows(db, query.trim()).map(trackFromRow);
    const artistArt = artistArtMap(db);
    const artists = new Map<string, Artist>();
    for (const t of tracks) {
      let a = artists.get(t.artistId);
      if (!a) {
        // The identity name, not the per-track credit (#152): two casings
        // share one artistId now, and the row must wear the canonical one.
        const name = t.albumArtist ?? t.artist;
        a = { id: t.artistId, name, bio: "", albums: [],
              artHash: artistArt.get(name) ?? null };
        artists.set(t.artistId, a);
      }
      let al = a.albums.find((x) => x.id === t.albumId);
      if (!al) {
        al = { id: t.albumId, title: t.album, artistId: t.artistId,
               // The track's tagged year (issue #167) -- hardcoded 0 here is
               // why every album in the overlay read "Artist · 0". 0 still
               // means "unknown", which the surfaces now render as nothing.
               artist: t.albumArtist ?? t.artist, year: t.year ?? 0, genre: "", tracks: [],
               artHash: t.artHash ?? null };
        a.albums.push(al);
      }
      al.tracks.push(t);
    }
    return searchLibrary([...artists.values()], query);
  });
}

// --- browse + shelves (issue #118 contracts, real rows) ---

export type BrowseSort = "az" | "new";
export type BrowseQuery = { genre?: string; sort: BrowseSort; offset?: number; limit?: number };
export type BrowseResult<T> = { items: T[]; total: number; nextOffset: number | null };
/** One rail entry: the letter and the offset its first entry sits at. */
export type RailEntry = { letter: string; offset: number };

export async function listGenres(): Promise<string[]> {
  return withDb<string[]>([], (db) =>
    [...new Set(albumIndex(db).map((e) => e.genre || "Uncategorized"))].sort(),
  );
}

export async function libraryCounts(): Promise<{ artists: number; albums: number; tracks: number }> {
  return withDb({ artists: 0, albums: 0, tracks: 0 }, libraryTotals);
}

function filteredIndex(entries: AlbumIndexEntry[], genre: string | undefined): AlbumIndexEntry[] {
  return genre ? entries.filter((e) => (e.genre || "Uncategorized") === genre) : entries;
}

function sortedAlbumEntries(entries: AlbumIndexEntry[], sort: BrowseSort): AlbumIndexEntry[] {
  return [...entries].sort(
    sort === "az"
      ? byName<AlbumIndexEntry>((e) => e.album)
      : byNewest<AlbumIndexEntry>((e) => e.year ?? 0, (e) => e.album),
  );
}

export async function browseAlbums(q: BrowseQuery): Promise<BrowseResult<Album>> {
  return withDb({ items: [], total: 0, nextOffset: null } as BrowseResult<Album>, (db) => {
    const pool = sortedAlbumEntries(filteredIndex(albumIndex(db), q.genre), q.sort);
    const win = clampWindow(pool.length, q.offset ?? 0, q.limit ?? PAGE_LIMIT);
    return {
      // Only the visible window hydrates tracks -- the point of the window.
      items: pool.slice(win.start, win.end).map((e) => hydrateAlbum(db, e)),
      total: pool.length,
      nextOffset: win.nextOffset,
    };
  });
}

type ArtistCard = { name: string; entries: AlbumIndexEntry[] };

function sortedArtistCards(entries: AlbumIndexEntry[], sort: BrowseSort): ArtistCard[] {
  const cards = [...artistAlbums(entries).entries()].map(([name, list]) => ({
    name,
    entries: list,
  }));
  return cards.sort(
    sort === "az"
      ? byName<ArtistCard>((c) => c.name)
      : byNewest<ArtistCard>(
          (c) => Math.max(...c.entries.map((e) => e.year ?? 0)),
          (c) => c.name,
        ),
  );
}

export async function browseArtists(q: BrowseQuery): Promise<BrowseResult<Artist>> {
  return withDb({ items: [], total: 0, nextOffset: null } as BrowseResult<Artist>, (db) => {
    const pool = sortedArtistCards(filteredIndex(albumIndex(db), q.genre), q.sort);
    const win = clampWindow(pool.length, q.offset ?? 0, q.limit ?? PAGE_LIMIT);
    const art = artistArtMap(db);
    return {
      items: pool.slice(win.start, win.end).map((c) => ({
        id: artistIdOf(c.name),
        name: c.name,
        bio: "",
        albums: c.entries.map((e) => hydrateAlbum(db, e)),
        artHash: art.get(c.name) ?? null,
      })),
      total: pool.length,
      nextOffset: win.nextOffset,
    };
  });
}

export async function albumRail(genre: string | undefined): Promise<RailEntry[]> {
  return withDb<RailEntry[]>([], (db) => {
    const names = sortedAlbumEntries(filteredIndex(albumIndex(db), genre), "az").map(
      (e) => e.album,
    );
    return lettersPresent(names).map((letter) => ({
      letter,
      offset: indexForLetter(names, letter),
    }));
  });
}

export async function artistRail(genre: string | undefined): Promise<RailEntry[]> {
  return withDb<RailEntry[]>([], (db) => {
    const names = sortedArtistCards(filteredIndex(albumIndex(db), genre), "az").map(
      (c) => c.name,
    );
    return lettersPresent(names).map((letter) => ({
      letter,
      offset: indexForLetter(names, letter),
    }));
  });
}

export async function getShelves(dateSeed: string): Promise<{
  recentlyAdded: Album[];
  recentlyPlayed: Album[];
  rediscovery: Album[];
}> {
  const none = { recentlyAdded: [], recentlyPlayed: [], rediscovery: [] };
  return withDb(none, (db) => {
    const entries = albumIndex(db);
    // SELECT LIGHT, HYDRATE THE WINNERS. The shelf helpers only read ids and
    // meta, so they choose among trackless albums; only the ~40 that actually
    // render get a tracks query. Hydrating the pool instead would be 3,000
    // queries per home render for 14 picks.
    const light = new Map(entries.map((e) => [
      albumIdOf(e.artist, e.album),
      { entry: e, album: albumFromRow(e, []) },
    ]));
    const rehydrate = (albums: Album[]): Album[] =>
      albums.map((a) => {
        const found = light.get(a.id);
        return found ? hydrateAlbum(db, found.entry) : a;
      });

    // "Recently added" now orders by real file mtimes -- the shelf note's
    // "by year until the catalog has ingest dates" era ends here.
    const byAdded = [...entries].sort((a, b) => (b.added ?? 0) - (a.added ?? 0));
    const added = recentlyAdded(
      byAdded.slice(0, 40).map((e) => light.get(albumIdOf(e.artist, e.album))!.album),
    );

    // Real play_history: pairs arrive newest-first; recentlyPlayed()
    // preserves caller order (its tested contract).
    const played = recentlyPlayedPairs(db, 20).map((p) => hydrateAlbum(db, p));
    const playedShelf = recentlyPlayed(played, played.map((a) => a.id));

    const recentIds = new Set(playedShelf.map((a) => a.id));
    const dig = rediscovery([...light.values()].map((v) => v.album), recentIds, dateSeed);
    return {
      recentlyAdded: rehydrate(added),
      recentlyPlayed: playedShelf,
      rediscovery: rehydrate(dig),
    };
  });
}

/** The queue the app opens with: mid-album at the last-played track, paused
 * -- real history once it exists, the newest-added album until then. */
export async function getSeedQueue(): Promise<{
  sequence: Track[];
  currentIndex: number;
  playingFrom: string;
}> {
  const none = { sequence: [] as Track[], currentIndex: -1, playingFrom: "" };
  return withDb(none, (db) => {
    const seed = seedPair(db);
    if (!seed) return none;
    const tracks = tracksForAlbum(db, seed.artist, seed.album);
    if (tracks.length === 0) return none;
    const i = seed.trackId ? tracks.findIndex((t) => t.id === seed.trackId) : 0;
    return { sequence: tracks, currentIndex: Math.max(0, i), playingFrom: seed.album };
  });
}
