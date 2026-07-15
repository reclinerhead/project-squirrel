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
  /** null for lossy formats -- bit depth is a lossless concept. */
  bitDepth: number | null;
  sampleRateHz: number | null;
  /** null for lossless -- bitrate is how lossy files brag. */
  bitrateKbps: number | null;
};

export type Album = {
  id: string;
  title: string;
  artistId: string;
  artist: string;
  year: number;
  tracks: Track[];
};

export type Artist = {
  id: string;
  name: string;
  /** Last.fm-style prose; Phase 1's bio-fetcher fills this for real. */
  bio: string;
  albums: Album[];
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
