// The catalog seam's pure half (issue #129): the id codec that puts real
// names in URLs, the format heuristic the quality badge rests on, and the
// row->shape mapping every page renders from. A bug here misfiles a track or
// 404s an artist silently -- exactly the class `pnpm build` can't catch.

import { describe, expect, it } from "vitest";
import {
  albumFromRow,
  albumIdOf,
  artistIdOf,
  decodeAlbumId,
  decodeArtistId,
  formatFromCatalog,
  ratingFromRow,
  trackFromRow,
  type TrackRow,
} from "./catalog-rows";

describe("the id codec", () => {
  it("round-trips names verbatim", () => {
    for (const name of ["Capital Cities", "AC/DC", "(Hed) P.e_", "Sigur Rós",
                        "10,000 Maniacs", "❦ odd unicode ❦"]) {
      expect(decodeArtistId(artistIdOf(name))).toBe(name);
    }
  });

  it("round-trips album pairs, including separators-looking titles", () => {
    const id = albumIdOf("AC/DC", "Back in Black / Live");
    expect(decodeAlbumId(id)).toEqual({ artist: "AC/DC", album: "Back in Black / Live" });
  });

  it("mints only URL-safe characters, because pages build raw hrefs", () => {
    // /album/${id} with no encodeURIComponent is the shipped link shape --
    // an id with a slash or question mark would silently truncate the route.
    const id = albumIdOf("AC/DC?", "T.N.T. #1 & more");
    expect(id).toMatch(/^[A-Za-z0-9_-]+$/);
  });

  it("distinct names cannot collide", () => {
    expect(albumIdOf("a", "b/c")).not.toBe(albumIdOf("a/b", "c"));
    expect(artistIdOf("ab")).not.toBe(artistIdOf("a b"));
  });

  it("a mangled URL decodes to null, not a crash", () => {
    expect(decodeArtistId("!!!not-base64!!!")).toBeNull();
    expect(decodeAlbumId(artistIdOf("no separator here"))).toBeNull();
  });
});

describe("the format heuristic", () => {
  it("passes the honest formats straight through", () => {
    expect(formatFromCatalog("mp3", 320_000)).toBe("mp3");
    expect(formatFromCatalog("flac", null)).toBe("flac");
    expect(formatFromCatalog("wav", null)).toBe("wav");
  });

  it("believes the real codec column over any bitrate (issue #157)", () => {
    // #149's stsd atom walk beats the heuristic even when they disagree --
    // a low-bitrate ALAC (sparse solo piano) and a hypothetical fat AAC
    // both land where the header says, not where the bitrate guesses.
    expect(formatFromCatalog("m4a", 420_000, "alac")).toBe("alac");
    expect(formatFromCatalog("m4a", 900_000, "aac")).toBe("aac");
    expect(formatFromCatalog("mp4", 256_000, "alac")).toBe("alac");
  });

  it("splits m4a by bitrate when the codec never rode along", () => {
    // The fallback for pre-backfill rows and the daemon's older wire shapes;
    // bitrate separates the two real populations cleanly (ALAC ~700-1100
    // kbps, iTunes-store AAC <= 320).
    expect(formatFromCatalog("m4a", 1_053_815)).toBe("alac");
    expect(formatFromCatalog("m4a", 256_000)).toBe("aac");
    expect(formatFromCatalog("m4a", 256_000, null)).toBe("aac");
    expect(formatFromCatalog("mp4", 900_000, null)).toBe("alac");
    expect(formatFromCatalog("m4a", null)).toBe("aac");
  });
});

const row = (over: Partial<TrackRow> = {}): TrackRow => ({
  id: "b:abc",
  title: "Safe And Sound",
  artist: "Capital Cities",
  album: "In A Tidal Wave Of Mystery",
  album_artist: null,
  track_no: 1,
  duration_s: 193.0,
  rating: null,
  format: "m4a",
  bitrate: 1_053_815,
  samplerate: 44100,
  ...over,
});

describe("trackFromRow", () => {
  it("maps the daemon-shared id and the display fields", () => {
    const t = trackFromRow(row());
    expect(t.id).toBe("b:abc");
    expect(t.title).toBe("Safe And Sound");
    expect(t.format).toBe("alac");
    expect(t.durationS).toBe(193.0);
    expect(t.sampleRateHz).toBe(44100);
  });

  it("bitrateKbps only for lossy; lossless brags elsewhere", () => {
    expect(trackFromRow(row()).bitrateKbps).toBeNull(); // alac
    expect(trackFromRow(row({ format: "mp3", bitrate: 320_000 })).bitrateKbps).toBe(320);
  });

  it("files under a compilation belong to the album artist's album", () => {
    const t = trackFromRow(row({ album_artist: "Various Artists" }));
    expect(t.artist).toBe("Capital Cities"); // the performer, on the row
    expect(t.artistId).toBe(artistIdOf("Various Artists")); // the shelf card
    expect(decodeAlbumId(t.albumId)?.artist).toBe("Various Artists");
  });

  it("carries the tagged year, null when untagged (issue #167)", () => {
    // Search assembles album stubs from track rows -- before the year rode
    // along, every album in the overlay wore a hardcoded "· 0".
    expect(trackFromRow(row({ year: 2001 })).year).toBe(2001);
    expect(trackFromRow(row({ year: null })).year).toBeNull();
    expect(trackFromRow(row()).year).toBeNull(); // pre-#167 wire shape
  });

  it("carries the raw container and believes the codec column (issue #157)", () => {
    const t = trackFromRow(row({ codec: "aac", bitrate: 900_000 }));
    expect(t.container).toBe("m4a"); // the raw format column, for the pill
    expect(t.format).toBe("aac"); // the codec wins over the fat bitrate
    expect(t.bitrateKbps).toBe(900); // and lossy bookkeeping follows the codec
  });

  it("carries the catalog's thumb, so a rated track renders rated", () => {
    expect(trackFromRow(row({ rating: -2 })).rating).toBe(-2);
    expect(trackFromRow(row()).rating).toBe(0); // no ratings row
  });

  it("null tags degrade to placeholders, never crash", () => {
    const t = trackFromRow(row({ title: null, artist: null, album: null,
                                 track_no: null, duration_s: null }));
    expect(t.title).toBe("Untitled");
    expect(t.artist).toBe("Unknown Artist");
    expect(t.album).toBe("Unknown Album");
    expect(t.trackNo).toBe(0);
    expect(t.durationS).toBe(0);
  });
});

describe("albumFromRow", () => {
  it("builds the id from the same codec the links use", () => {
    const al = albumFromRow(
      { artist: "Capital Cities", album: "Solarize", year: 2018, genre: "Pop" },
      [],
    );
    expect(al.id).toBe(albumIdOf("Capital Cities", "Solarize"));
    expect(al.genre).toBe("Pop");
  });

  it("missing year and genre have honest defaults", () => {
    const al = albumFromRow({ artist: "X", album: "Y", year: null, genre: null }, []);
    expect(al.year).toBe(0);
    expect(al.genre).toBe("Uncategorized");
  });

  it("carries the focal anchor, centering when unanalyzed (issue #159)", () => {
    const base = { artist: "X", album: "Y", year: 2020, genre: "Rock" };
    expect(albumFromRow({ ...base, focal_y: 0.71 }, []).artFocalY).toBe(0.71);
    expect(albumFromRow({ ...base, focal_y: null }, []).artFocalY).toBeNull();
    expect(albumFromRow(base, []).artFocalY).toBeNull(); // pre-focal shape
  });
});

describe("ratingFromRow", () => {
  it("maps the four real values through", () => {
    for (const v of [-2, -1, 1, 2]) expect(ratingFromRow(v)).toBe(v);
  });

  it("null -- no ratings row -- is unrated", () => {
    // The store holds only opinions; the absence of one is not a stored zero.
    expect(ratingFromRow(null)).toBe(0);
  });

  it("clamps a value the store should never hold to unrated", () => {
    // The control's transition table assumes five states. A stray 3 arriving
    // from a hand-edited catalog would strand the thumb in a state no click
    // could leave, which is worse than forgetting it.
    for (const v of [3, -3, 99, 0.5]) expect(ratingFromRow(v)).toBe(0);
  });
});

// --- the album-key contract with the Python extractor (issue #153) ---------
//
// music_catalog.ALBUM_KEY_SQL mints "artist<U+241F>album" strings and the
// art tables key on them; albumIdOf base64urls the SAME joined string. These
// fixtures are pinned VERBATIM in test_music_catalog.py
// (test_album_key_sql_matches_the_gui_derivation) -- change one side and
// the other side's test is what catches you.

import { describe as describe153, expect as expect153, it as it153 } from "vitest";

const b64url = (s: string) =>
  Buffer.from(s, "utf-8").toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

describe153("albumIdOf matches the extractor's album_key (issue #153)", () => {
  it153("plain artist + album", () => {
    expect153(albumIdOf("Capital Cities", "In A Tidal Wave Of Mystery")).toBe(
      b64url("Capital Cities␟In A Tidal Wave Of Mystery"),
    );
  });
  it153("compilations key on the album artist", () => {
    expect153(albumIdOf("Various Artists", "Now That's Music")).toBe(
      b64url("Various Artists␟Now That's Music"),
    );
  });
  it153("the nameless tail keys on the display fallbacks", () => {
    expect153(albumIdOf("Unknown Artist", "Unknown Album")).toBe(
      b64url("Unknown Artist␟Unknown Album"),
    );
  });
  it153("the canonical identity leads the key when the pass has run (#152)", () => {
    // Python twin: test_album_key_sql_matches_the_gui_derivation's b:4 --
    // a track tagged `Gwar` with artist_norm `GWAR` mints the CANONICAL key.
    const t = trackFromRow({
      id: "b:4", title: "T", artist: "Gwar", album: "Scumdogs of the Universe",
      album_artist: null, artist_norm: "GWAR", track_no: 1, duration_s: 1,
      format: "mp3", bitrate: 128000, samplerate: 44100, rating: null,
    });
    expect153(t.albumId).toBe(b64url("GWAR␟Scumdogs of the Universe"));
    expect153(t.artistId).toBe(b64url("GWAR"));
  });
});

describe153("case-split artists collapse through artist_norm (#152)", () => {
  const row = (id: string, artist: string) => ({
    id, title: "T", artist, album: "Al", album_artist: null,
    artist_norm: "GWAR", track_no: 1, duration_s: 1, format: "mp3",
    bitrate: 128000, samplerate: 44100, rating: null,
  });
  it153("two casings mint one artistId, one albumId, one identity name", () => {
    const a = trackFromRow(row("b:1", "Gwar"));
    const b = trackFromRow(row("b:2", "GWAR"));
    expect153(a.artistId).toBe(b.artistId);
    expect153(a.albumId).toBe(b.albumId);
    expect153(a.albumArtist).toBe("GWAR");
    expect153(a.artist).toBe("Gwar"); // the raw per-track credit survives
  });
  it153("absent artist_norm falls back to the raw derivation", () => {
    const t = trackFromRow({ ...row("b:3", "Gwar"), artist_norm: null });
    expect153(t.artistId).toBe(b64url("Gwar"));
    expect153(t.albumArtist).toBe("Gwar");
  });
});

describe153("art rides the row mappers (issue #153)", () => {
  const base = {
    id: "b:1", title: "T", artist: "A", album: "Al", album_artist: null,
    track_no: 1, duration_s: 100, format: "mp3", bitrate: 128000,
    samplerate: 44100, rating: null,
  };
  it153("trackFromRow carries art_hash and defaults to null", () => {
    expect153(trackFromRow({ ...base, art_hash: "abc" }).artHash).toBe("abc");
    expect153(trackFromRow(base).artHash).toBeNull();
  });
  it153("albumFromRow carries art_hash and defaults to null", () => {
    const row = { artist: "A", album: "Al", year: 2000, genre: "Pop" };
    expect153(albumFromRow({ ...row, art_hash: "abc" }, []).artHash).toBe("abc");
    expect153(albumFromRow(row, []).artHash).toBeNull();
  });
});
