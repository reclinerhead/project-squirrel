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
  /** The album-level artist identity the ids derive from -- canonical
   * casing since #152 (`artist_norm`), and the name the search overlay
   * shows for an artist row. Differs from `artist` on compilations (the
   * performer credit) and on tracks tagged with a minority casing.
   * Optional so pre-#152 shapes (fixtures, the daemon's /queue payload)
   * type-check untouched; absent falls back to `artist`. */
  albumArtist?: string;
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
  /** The tagged release year (issue #167) -- rides the track so search's
   * album stubs can wear a real year. Optional/null on pre-#167 shapes
   * (fixtures, the daemon's /queue payload); null = unknown. */
  year?: number | null;
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
  /** One genre per album at the UI seam (issue #118): the dominant tag
   * across the album's tracks. CANONICAL since #163 -- the seam reads
   * genre_norm (genre_rules.yaml's small vocabulary), never the raw iTunes
   * taxonomy; tracks the pass hasn't placed surface as "Uncategorized". */
  genre: string;
  tracks: Track[];
  /** Real cover art's content hash (issue #153), or null/absent for the
   * ~10% the extractor found nothing for -- those keep the generated SVG. */
  artHash?: string | null;
  /** Where the cover's interest lives vertically, 0..1 (issue #159) -- the
   * extraction pass's edge-density centroid. Anchors the album page's
   * cropped backdrop band; null/absent = center, exactly the pre-focal
   * behavior. */
  artFocalY?: number | null;
  /** Editorial copy about this record (issue #171), lifted from the files'
   * comment tags and trimmed back to whole sentences. Present for ~20% of
   * albums; null/absent renders no panel at all. Optional so pre-#171
   * shapes -- fixtures, the daemon's payloads -- type-check untouched. */
  description?: string | null;
  /** Where that copy came from: "comment-tag" | "external" | "owner". */
  descriptionSrc?: string | null;
};

export type Artist = {
  id: string;
  name: string;
  /** Encyclopedia prose, filled by the Phase 1b fetcher (issue #170).
   * Stays `string` with "" for absent -- the contract every other producer
   * (search, browseArtists) hardcodes, and what the GUI's empty path has
   * always relied on. */
  bio: string;
  /** Where the bio came from: "wikipedia" | "lastfm" | "owner" (issue #170).
   * Optional so pre-#170 shapes -- fixtures, the daemon's payloads -- still
   * type-check; absent or null renders no source stamp. */
  bioSrc?: string | null;
  /** The attribution link for the prose. Wikipedia's CC BY-SA makes this
   * part of using the text properly, not decoration. */
  bioUrl?: string | null;
  albums: Album[];
  /** The artist image (issue #153): a promoted album cover today
   * (source='derived'), the owner's own photo once that lands. */
  artHash?: string | null;
};

/** Just the prose and its attribution (issue #170) -- what the album page's
 * "About {artist}" panel needs. Deliberately NOT a whole Artist: hydrating
 * every album of an artist to render one paragraph under a tracklist would
 * be a query storm for text. */
export type ArtistBio = {
  name: string;
  /** The artist page's URL id, so the panel can link through. */
  id: string;
  bio: string;
  bioSrc?: string | null;
  bioUrl?: string | null;
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
