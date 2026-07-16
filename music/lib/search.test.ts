import { describe, expect, it } from "vitest";
import { GROUP_CAPS, matchScore, searchLibrary } from "./search";
import type { Album, Artist, Track } from "./types";

function track(id: string, title: string, artist: string): Track {
  return {
    id,
    title,
    artistId: artist.toLowerCase(),
    artist,
    albumId: "al",
    album: "Album",
    trackNo: 1,
    durationS: 200,
    format: "mp3",
    bitDepth: null,
    sampleRateHz: 44100,
    bitrateKbps: 320,
    rating: 0,
  };
}

function artist(name: string, albums: [string, string[]][]): Artist {
  return {
    id: name.toLowerCase().replace(/\s+/g, "-"),
    name,
    bio: "",
    albums: albums.map(
      ([title, tracks], i): Album => ({
        id: `${name.toLowerCase().replace(/\s+/g, "-")}-al${i}`,
        title,
        artistId: name.toLowerCase(),
        artist: name,
        year: 2020,
        tracks: tracks.map((t, j) => track(`${name}-${i}-${j}`, t, name)),
      }),
    ),
  };
}

const CORPUS: Artist[] = [
  artist("Signal Creek", [["One Bar", ["Coverage Map", "Weather System"]]]),
  artist("Driveway Ghosts", [["Gravel Static", ["Motion Light", "Signal Fire"]]]),
  artist("The Cold Frame", [["Under Glass", ["Germination"]]]),
];

describe("matchScore", () => {
  it("ranks exact > prefix > word-prefix > substring > none", () => {
    expect(matchScore("Signal Creek", "signal creek")).toBe(4);
    expect(matchScore("Signal Creek", "sig")).toBe(3);
    expect(matchScore("Signal Creek", "creek")).toBe(2);
    expect(matchScore("Signal Creek", "gnal")).toBe(1);
    expect(matchScore("Signal Creek", "owl")).toBe(0);
  });

  it("is case- and whitespace-insensitive, and empty never matches", () => {
    expect(matchScore("Signal Creek", "  SIGNAL  ")).toBe(3);
    expect(matchScore("Signal Creek", "")).toBe(0);
    expect(matchScore("", "signal")).toBe(0);
  });
});

describe("searchLibrary", () => {
  it("returns nothing for queries under two characters", () => {
    const r = searchLibrary(CORPUS, "s");
    expect(r).toEqual({ artists: [], albums: [], tracks: [] });
  });

  it("groups matches by kind", () => {
    const r = searchLibrary(CORPUS, "signal");
    expect(r.artists.map((a) => a.name)).toEqual(["Signal Creek"]);
    expect(r.albums).toEqual([]);
    // Title hit ("Signal Fire") outranks the artist-name hit's tracks.
    expect(r.tracks[0].title).toBe("Signal Fire");
  });

  it("surfaces an artist's tracks on an artist-name query", () => {
    const r = searchLibrary(CORPUS, "signal creek");
    const titles = r.tracks.map((t) => t.title);
    expect(titles).toContain("Coverage Map");
    expect(titles).toContain("Weather System");
  });

  it("ranks a title-prefix hit above artist-name hits", () => {
    const r = searchLibrary(CORPUS, "si");
    // "Signal Fire" matches on its own title (doubled); Signal Creek's tracks
    // ride along on the artist name, alphabetically behind it.
    expect(r.tracks.map((t) => t.title)).toEqual(["Signal Fire", "Coverage Map", "Weather System"]);
  });

  it("caps each group", () => {
    const big = [
      artist("Wren", [["Nest", Array.from({ length: 20 }, (_, i) => `Nest Song ${i + 1}`)]]),
    ];
    const r = searchLibrary(big, "nest");
    expect(r.tracks.length).toBe(GROUP_CAPS.tracks);
  });
});
