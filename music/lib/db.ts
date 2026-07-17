// The catalog reader (issue #129): music.db -> the shapes lib/api.ts hands
// the pages. Server-only by construction (node:sqlite), which is enforcement,
// not convention -- a client component importing this fails the build loudly.
//
// The mcc precedent applies whole (mcc/app/weather/history/route.ts): opened
// PER REQUEST, read-only, from MERLE_MUSIC_DB with NO DEFAULT -- the unit's
// WorkingDirectory is the music/ subdirectory, so a relative default would
// name a file the indexer never writes and this app would confidently serve
// an empty library. Unset env or a missing file degrade to an empty catalog
// (day one on a fresh box is not an error), never to a 500.
//
// SHAPE STRATEGY: the album/artist indexes (one row per album, ~3k) load
// whole and go through the SAME pure sort/window/rail helpers the fixture era
// used (lib/browse.ts) -- their tests, and the exact ordering the UI shipped
// with, keep holding. Only the visible window's tracks are hydrated. SQLite
// reads a 3k-row GROUP BY off a 12 MB local file in single-digit
// milliseconds; the win of porting sortKey()'s leading-article rules into SQL
// is nothing, and the cost is two implementations of one ordering.
//
// The recycle bin needs no WHERE here: the indexer stopped walking it
// (EXCLUDED_DIRS) and the deploy re-index prunes what the first pass caught.
// Catalog hygiene is the indexer's job; queries trust the catalog.

import { DatabaseSync } from "node:sqlite";
import {
  albumFromRow,
  artistIdOf,
  trackFromRow,
  type AlbumRow,
  type TrackRow,
} from "./catalog-rows";
import type { Album, Artist, Track } from "./types";

export function openDb(): DatabaseSync | null {
  const path = process.env.MERLE_MUSIC_DB;
  if (!path) return null;
  try {
    return new DatabaseSync(path, { readOnly: true });
  } catch {
    return null; // no catalog yet, or not ours to read
  }
}

/** Run `fn` against the catalog, or return `empty` when there isn't one. */
export function withDb<T>(empty: T, fn: (db: DatabaseSync) => T): T {
  const db = openDb();
  if (!db) return empty;
  try {
    return fn(db);
  } catch {
    return empty; // a catalog missing its tables is still just no data
  } finally {
    db.close();
  }
}

// COALESCE(album_artist, artist) everywhere an ALBUM is grouped or named:
// compilations carry album_artist "Various Artists" with per-track artists,
// and grouping by track artist would explode one album into twenty.
const ALBUM_ARTIST = "COALESCE(NULLIF(t.album_artist, ''), t.artist, 'Unknown Artist')";

// THE ALBUM KEY (issue #153): the U+241F pair the art tables are keyed on.
// This derivation is albumIdOf's input, verbatim -- and it has a Python twin
// (music_catalog.ALBUM_KEY_SQL) that the extractor writes with. The paired
// fixture tests (catalog-rows.test.ts / test_music_catalog.py) are what keep
// the two implementations from drifting.
const ALBUM_KEY =
  ALBUM_ARTIST + " || '␟' || COALESCE(NULLIF(t.album, ''), 'Unknown Album')";

// The art scalar subquery, same shape as the rating's and for the same
// reason: TRACK_COLS splices into GROUP BY queries where a join's column
// would need grouping; album_art is PK-probed per row over a window.
const ART_HASH_SUB =
  `(SELECT art_hash FROM album_art WHERE album_key = ${ALBUM_KEY})`;

/** Whether this catalog has the art tables yet (issue #153). A music.db
 * snapshot from before the art pass -- or a pearl mid-deploy where the app
 * restarted before the daemon minted the tables -- must degrade to "no art,
 * full library", never to withDb's catch turning every page empty. */
function hasArt(db: DatabaseSync): boolean {
  return !!db
    .prepare("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'album_art'")
    .get();
}

// The rating rides along on every track the app hands out (issue #135). A
// persisted thumb that doesn't come back on load is indistinguishable from a
// lost one, so hydration is not a separate feature -- it's the other half of
// the write, and it belongs where every surface already gets its data.
//
// A scalar subquery rather than a LEFT JOIN: TRACK_COLS is spliced into three
// queries including topTracks' COUNT(ph.id) GROUP BY, where a joined column
// would have to be grouped too. ratings is keyed by track_id (PK), so this is
// an index probe per row over a window of tracks, never the catalog.
const trackCols = (art: boolean) =>
  "t.id, t.title, t.artist, t.album, t.album_artist, t.track_no, " +
  "t.duration_s, t.format, t.codec, t.bitrate, t.samplerate, " +
  "(SELECT value FROM ratings WHERE track_id = t.id) AS rating, " +
  (art ? ART_HASH_SUB : "NULL") + " AS art_hash";

/** One row per album: name pair + year + dominant genre + newest file mtime
 * (the "added" proxy until the catalog has ingest dates). Dominant genre is
 * decided while scanning per-(album, genre) counts -- the types.ts contract
 * says an album's genre comes from its tracks' dominant tag. */
export type AlbumIndexEntry = AlbumRow & { added: number };

export function albumIndex(db: DatabaseSync): AlbumIndexEntry[] {
  // The art subquery re-derives the key from t.* inside a grouped query --
  // safe because every row in a (artist, album, genre) group derives the
  // same key, so SQLite's any-row semantics can't pick differently.
  const art = hasArt(db) ? ART_HASH_SUB : "NULL";
  const rows = db
    .prepare(
      `SELECT ${ALBUM_ARTIST} AS artist,
              COALESCE(NULLIF(t.album, ''), 'Unknown Album') AS album,
              t.genre AS genre, COUNT(*) AS n,
              MAX(t.year) AS year, MAX(f.mtime) AS added,
              ${art} AS art_hash
       FROM tracks t JOIN track_files f ON f.track_id = t.id
       GROUP BY 1, 2, 3`,
    )
    .all() as Array<AlbumRow & { n: number; added: number }>;

  const byAlbum = new Map<string, { entry: AlbumIndexEntry; best: number }>();
  for (const r of rows) {
    const key = r.artist + "␟" + r.album;
    const seen = byAlbum.get(key);
    if (!seen) {
      byAlbum.set(key, { entry: { ...r }, best: r.genre ? r.n : -1 });
    } else {
      seen.entry.year = Math.max(seen.entry.year ?? 0, r.year ?? 0);
      seen.entry.added = Math.max(seen.entry.added ?? 0, r.added ?? 0);
      if (r.genre && r.n > seen.best) {
        seen.entry.genre = r.genre;
        seen.best = r.n;
      }
    }
  }
  return [...byAlbum.values()].map((v) => v.entry);
}

export function tracksForAlbum(db: DatabaseSync, artist: string, album: string): Track[] {
  const rows = db
    .prepare(
      `SELECT ${trackCols(hasArt(db))}
       FROM tracks t
       WHERE ${ALBUM_ARTIST} = ? AND COALESCE(NULLIF(t.album, ''), 'Unknown Album') = ?
       ORDER BY COALESCE(t.disc_no, 1), COALESCE(t.track_no, 0), t.title`,
    )
    .all(artist, album) as TrackRow[];
  return rows.map(trackFromRow);
}

export function hydrateAlbum(db: DatabaseSync, entry: AlbumRow): Album {
  return albumFromRow(entry, tracksForAlbum(db, entry.artist, entry.album));
}

/** The artists index derives from the album index: an artist here is an
 * ALBUM artist (the browse cards and the artist page both hang off albums).
 * Track-artist browsing -- every performer on every compilation -- is a
 * later phase's card catalog, not this seam. */
export function artistAlbums(entries: AlbumIndexEntry[]): Map<string, AlbumIndexEntry[]> {
  const byArtist = new Map<string, AlbumIndexEntry[]>();
  for (const e of entries) {
    const list = byArtist.get(e.artist);
    if (list) list.push(e);
    else byArtist.set(e.artist, [e]);
  }
  return byArtist;
}

export function artistFor(db: DatabaseSync, name: string): Artist | null {
  const entries = albumIndex(db).filter((e) => e.artist === name);
  if (entries.length === 0) return null;
  const bio = db
    .prepare("SELECT bio FROM artists WHERE name = ?")
    .get(name) as { bio: string | null } | undefined;
  const art = hasArt(db)
    ? (db.prepare("SELECT art_hash FROM artist_art WHERE artist = ?").get(name) as
        | { art_hash: string }
        | undefined)
    : undefined;
  return {
    id: artistIdOf(name),
    name,
    bio: bio?.bio ?? "",
    albums: entries.map((e) => hydrateAlbum(db, e)),
    artHash: art?.art_hash ?? null,
  };
}

/** artist -> art hash, whole-table (issue #153): the browse-artists window
 * attaches these by lookup; 747 tiny rows read in microseconds, and a probe
 * per card would be a query per artist per page. */
export function artistArtMap(db: DatabaseSync): Map<string, string> {
  if (!hasArt(db)) return new Map();
  const rows = db.prepare("SELECT artist, art_hash FROM artist_art").all() as Array<{
    artist: string;
    art_hash: string;
  }>;
  return new Map(rows.map((r) => [r.artist, r.art_hash]));
}

/** Search candidates: bounded LIKE sweep; ranking stays in lib/search.ts's
 * tested scorer. The candidate cap exists so a one-letter query can't pull
 * 25k rows -- the UI won't search under 2 chars anyway. */
export function searchTrackRows(db: DatabaseSync, q: string, cap = 400): TrackRow[] {
  const like = "%" + q.replace(/[%_]/g, "") + "%";
  return db
    .prepare(
      `SELECT ${trackCols(hasArt(db))}
       FROM tracks t
       WHERE t.title LIKE ? OR t.artist LIKE ? OR t.album LIKE ?
       LIMIT ?`,
    )
    .all(like, like, like, cap) as TrackRow[];
}

/** Album ids most recently played, newest first -- real play_history, the
 * thing Phase 2a exists to start collecting. */
export function recentlyPlayedPairs(db: DatabaseSync, cap: number): AlbumRow[] {
  const art = hasArt(db) ? ART_HASH_SUB : "NULL";
  return db
    .prepare(
      `SELECT ${ALBUM_ARTIST} AS artist,
              COALESCE(NULLIF(t.album, ''), 'Unknown Album') AS album,
              MAX(t.year) AS year, MAX(t.genre) AS genre,
              MAX(ph.played_at) AS latest,
              ${art} AS art_hash
       FROM play_history ph JOIN tracks t ON t.id = ph.track_id
       GROUP BY 1, 2 ORDER BY latest DESC LIMIT ?`,
    )
    .all(cap) as unknown as AlbumRow[];
}

/** An artist's most-played tracks -- play_history-ranked, exactly what the
 * artist page's "fixture-ranked · play history later" note promised. */
export function topTrackRows(db: DatabaseSync, artist: string, cap = 5): TrackRow[] {
  return db
    .prepare(
      `SELECT ${trackCols(hasArt(db))}, COUNT(ph.id) AS plays
       FROM play_history ph JOIN tracks t ON t.id = ph.track_id
       WHERE ${ALBUM_ARTIST} = ?
       GROUP BY t.id ORDER BY plays DESC, t.title LIMIT ?`,
    )
    .all(artist, cap) as TrackRow[];
}

/** The seed queue: the album containing the most recently played track,
 * cursor on that track -- the app opens where the listening left off. No
 * history yet (day one) falls back to the newest-added album at track 0. */
export function seedPair(db: DatabaseSync): { artist: string; album: string; trackId: string | null } | null {
  const last = db
    .prepare(
      `SELECT ${ALBUM_ARTIST} AS artist,
              COALESCE(NULLIF(t.album, ''), 'Unknown Album') AS album,
              t.id AS trackId
       FROM play_history ph JOIN tracks t ON t.id = ph.track_id
       ORDER BY ph.played_at DESC LIMIT 1`,
    )
    .get() as { artist: string; album: string; trackId: string } | undefined;
  if (last) return last;
  const newest = db
    .prepare(
      `SELECT ${ALBUM_ARTIST} AS artist,
              COALESCE(NULLIF(t.album, ''), 'Unknown Album') AS album,
              MAX(f.mtime) AS added
       FROM tracks t JOIN track_files f ON f.track_id = t.id
       GROUP BY 1, 2 ORDER BY added DESC LIMIT 1`,
    )
    .get() as { artist: string; album: string } | undefined;
  return newest ? { ...newest, trackId: null } : null;
}

export function libraryTotals(db: DatabaseSync): { artists: number; albums: number; tracks: number } {
  const entries = albumIndex(db);
  return {
    artists: artistAlbums(entries).size,
    albums: entries.length,
    tracks: (db.prepare("SELECT COUNT(*) AS n FROM tracks").get() as { n: number }).n,
  };
}
