// Shared shapes for the music UI (issue #116). These mirror what epic #115's
// Phase 0 catalog will hold -- content-hash ids, format/bit-depth/sample-rate
// straight off the index -- so that when the real API lands, the fixture layer
// in lib/api.ts is replaced and nothing above it changes shape.

export type AudioFormat = "alac" | "flac" | "wav" | "mp3" | "aac";

export type Track = {
  /** Content hash in the real catalog; opaque fixture ids here. */
  id: string;
  title: string;
  artistId: string;
  artist: string;
  albumId: string;
  album: string;
  trackNo: number;
  durationS: number;
  format: AudioFormat;
  /** The catalog's raw format column -- the CONTAINER (m4a/mp4/flac/wav/mp3),
   * where `format` above is the codec-level answer (issue #157: the format
   * pill states both). Optional so pre-container shapes -- fixtures, the
   * daemon's /queue payload -- type-check untouched; absent falls back to
   * deriving the container from `format`. */
  container?: string | null;
  /** null for lossy formats -- bit depth is a lossless concept. */
  bitDepth: number | null;
  sampleRateHz: number | null;
  /** null for lossless -- bitrate is how lossy files brag. */
  bitrateKbps: number | null;
  /** The listener's thumb as the catalog holds it, 0 when unrated (issue
   * #135). It rides on the track because every surface that renders a rating
   * already receives one of these -- and because a rating that only existed
   * in client state is what this replaced. Edits made this session live in
   * PlayerProvider and take precedence; this is the baseline it starts from. */
  rating: Rating;
  /** The ALBUM's art (issue #153), riding on the track because the player
   * bar, queue, and search rows render covers off tracks alone. Optional so
   * the fixture library (which predates art) type-checks untouched; null or
   * absent renders the generated SVG. */
  artHash?: string | null;
};

export type Album = {
  id: string;
  title: string;
  artistId: string;
  artist: string;
  year: number;
  /** One genre per album at the UI seam (issue #118). The real catalog tags
   * genre per track; the seam will derive an album's genre from its tracks'
   * dominant tag once Phase 0/1's normalization exists. */
  genre: string;
  tracks: Track[];
  /** Real cover art's content hash (issue #153), or null/absent for the
   * ~10% the extractor found nothing for -- those keep the generated SVG. */
  artHash?: string | null;
};

export type Artist = {
  id: string;
  name: string;
  /** Last.fm-style prose; Phase 1's bio-fetcher fills this for real. */
  bio: string;
  albums: Album[];
  /** The artist image (issue #153): a promoted album cover today
   * (source='derived'), the owner's own photo once that lands. */
  artHash?: string | null;
};

/** Four-level feedback (epic #115): -2 hard-filters, +2 boosts. 0 = unrated. */
export type Rating = -2 | -1 | 0 | 1 | 2;

export type QualityTier = "hires" | "lossless" | "lossy";

/** Phase 2's three confirmed playback targets, verbatim. */
export type Output = {
  id: string;
  name: string;
  kind: "browser" | "dlna";
};
