// The pure half of the catalog seam (issue #129): music.db rows -> the UI's
// Track/Album shapes, and the id codec that makes catalog entities addressable
// in URLs. No node imports, no SQL -- lib/db.ts owns the queries and feeds
// rows through here, so everything that could silently mangle a name or
// misfile a track is testable in vitest against synthetic rows.
//
// IDS ARE DERIVED, NOT STORED. The catalog has no artist/album tables with
// PKs -- an artist IS a name, an album IS an (artist, title) pair (that's
// Phase 0's design: identity lives on tracks). But names go in URLs, and the
// pages build links as raw `/album/${id}` with no encoding -- so ids must be
// URL-safe for ANY name this library holds ("AC/DC" would 404 as a path, "?"
// would eat the querystring). base64url gives ids from the alphabet
// [A-Za-z0-9_-], reversible, collision-free -- at the cost of opacity, which
// a URL for a personal music library can afford.
//
// TRACK ids need none of this: they're the indexer's content hashes
// (b:<hex>, f:<hex>), already URL-safe, and shared verbatim with the
// daemon's /stream/{id}.

import type { Album, AudioFormat, Rating, Track } from "./types";

// U+241F (symbol for unit separator) between artist and album inside one id:
// visually obvious in a debugger, and no album title contains it.
const SEP = "␟";

const b64 = (bytes: Uint8Array) => {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
};

const unb64 = (id: string) => {
  const pad = id.length % 4 === 0 ? "" : "=".repeat(4 - (id.length % 4));
  const bin = atob(id.replace(/-/g, "+").replace(/_/g, "/") + pad);
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
};

export function artistIdOf(name: string): string {
  return b64(new TextEncoder().encode(name));
}

export function decodeArtistId(id: string): string | null {
  try {
    return new TextDecoder().decode(unb64(id));
  } catch {
    return null; // a mangled URL is a 404, not a crash
  }
}

export function albumIdOf(artist: string, album: string): string {
  return b64(new TextEncoder().encode(artist + SEP + album));
}

export function decodeAlbumId(id: string): { artist: string; album: string } | null {
  const text = decodeArtistId(id);
  if (text === null) return null;
  const i = text.indexOf(SEP);
  if (i < 0) return null;
  return { artist: text.slice(0, i), album: text.slice(i + 1) };
}

/** The catalog's format column -> the UI's AudioFormat. The catalog says
 * "m4a"/"mp4" without saying what's inside; the CODEC column answers that
 * for real (issue #149's stsd atom walk, wired through here in #157) -- the
 * same provenance the daemon's needs_flac policy trusts. The bitrate split
 * (ALAC in this library runs ~700-1100 kbps, iTunes-store AAC tops out at
 * 320) survives only as the fallback for rows indexed before the column
 * existed and never backfilled -- its worst case is a mislabeled pill,
 * never a playback decision. */
export function formatFromCatalog(
  format: string | null,
  bitrate: number | null,
  codec?: string | null,
): AudioFormat {
  switch (format) {
    case "mp3":
      return "mp3";
    case "flac":
      return "flac";
    case "wav":
      return "wav";
    case "m4a":
    case "mp4":
      if (codec === "alac") return "alac";
      if (codec === "aac") return "aac";
      return (bitrate ?? 0) > 500_000 ? "alac" : "aac";
    default:
      return "aac"; // unreachable for a catalog the indexer wrote
  }
}

const LOSSY = new Set<AudioFormat>(["mp3", "aac"]);

const RATINGS = new Set([-2, -1, 1, 2]);

/** The catalog's nullable rating -> the UI's Rating. Null (no row) is 0 =
 * unrated. Anything else the column could somehow hold is also 0: this is the
 * boundary between a store that promises four values and a control whose
 * transition table assumes exactly five states, and a stray 3 arriving here
 * would strand the thumb in a state no click could leave. */
export function ratingFromRow(value: number | null): Rating {
  return (RATINGS.has(value as number) ? value : 0) as Rating;
}

/** One tracks-table row (join carries the album's display artist) -> Track.
 * The album's artist may differ from the track's (compilations: album_artist
 * "Various Artists", track artist the performer) -- both ride along. */
export type TrackRow = {
  id: string;
  title: string | null;
  artist: string | null;
  album: string | null;
  album_artist: string | null;
  // The normalization pass's case-collapsed artist identity (issue #152) --
  // when present it IS the album-level artist, so `Gwar` and `GWAR` rows
  // mint one artistId/albumId. Optional: pre-#152 shapes (the daemon's
  // /queue payload, older snapshots) fall back to the raw derivation.
  artist_norm?: string | null;
  track_no: number | null;
  duration_s: number | null;
  format: string | null;
  // What's inside an m4a/mp4 container (issue #149; null elsewhere, and null
  // for pre-backfill rows). Optional because the daemon's /queue wire shape
  // predates the column -- absent falls back to the bitrate heuristic.
  codec?: string | null;
  bitrate: number | null;
  samplerate: number | null;
  // The track's tagged release year (issue #167): search assembles album
  // stubs from track rows, and without this every album in the overlay wore
  // a hardcoded year 0. Optional -- pre-#167 wire shapes omit it.
  year?: number | null;
  // The catalog stores only real opinions (-2/-1/+1/+2); an unrated track has
  // no ratings row at all, so this arrives null and maps to 0 = unrated.
  rating: number | null;
  // The album's cover hash (issue #153) -- null before the art pass ran, or
  // for the ~10% with no art anywhere. Optional so pre-art row shapes (the
  // daemon's /queue payload) keep satisfying this type unchanged.
  art_hash?: string | null;
};

export function trackFromRow(row: TrackRow): Track {
  const albumArtist =
    row.artist_norm || row.album_artist || row.artist || "Unknown Artist";
  const artist = row.artist || albumArtist;
  const album = row.album || "Unknown Album";
  const format = formatFromCatalog(row.format, row.bitrate, row.codec);
  const lossy = LOSSY.has(format);
  return {
    id: row.id,
    title: row.title || "Untitled",
    artistId: artistIdOf(albumArtist),
    artist,
    albumArtist,
    albumId: albumIdOf(albumArtist, album),
    album,
    trackNo: row.track_no ?? 0,
    durationS: row.duration_s ?? 0,
    format,
    container: row.format,
    // The catalog doesn't carry bit depth (Phase 1); null is honest, and
    // the quality badge treats lossless-with-unknown-depth as plain lossless.
    bitDepth: null,
    sampleRateHz: row.samplerate ?? null,
    year: row.year ?? null,
    bitrateKbps: lossy && row.bitrate ? Math.round(row.bitrate / 1000) : null,
    rating: ratingFromRow(row.rating),
    artHash: row.art_hash ?? null,
  };
}

/** Album meta from the grouped index query (tracks hydrated separately). */
export type AlbumRow = {
  artist: string; // already COALESCE(album_artist, artist)'d by the query
  album: string;
  year: number | null;
  genre: string | null;
  art_hash?: string | null; // issue #153; absent on pre-art shapes
  focal_y?: number | null; // issue #159; absent pre-focal, null = center
};

export function albumFromRow(row: AlbumRow, tracks: Track[]): Album {
  return {
    id: albumIdOf(row.artist, row.album),
    title: row.album,
    artistId: artistIdOf(row.artist),
    artist: row.artist,
    year: row.year ?? 0,
    genre: row.genre || "Uncategorized",
    tracks,
    artHash: row.art_hash ?? null,
    artFocalY: row.focal_y ?? null,
  };
}
