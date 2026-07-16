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

  it("splits m4a by bitrate: ALAC brags in the megabit range", () => {
    // The catalog can't see inside an mp4 container until Phase 1's codec
    // column; bitrate separates the two real populations cleanly (ALAC
    // ~700-1100 kbps, iTunes-store AAC <= 320).
    expect(formatFromCatalog("m4a", 1_053_815)).toBe("alac");
    expect(formatFromCatalog("m4a", 256_000)).toBe("aac");
    expect(formatFromCatalog("mp4", 900_000)).toBe("alac");
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
});
