import { describe, expect, it } from "vitest";
import { qualityForAlbum, qualityForTrack } from "./quality";
import type { Track } from "./types";

function spec(over: Partial<Track>): Pick<Track, "format" | "bitDepth" | "sampleRateHz" | "bitrateKbps"> {
  return { format: "alac", bitDepth: 24, sampleRateHz: 48000, bitrateKbps: null, ...over };
}

describe("qualityForTrack", () => {
  it("calls >16-bit lossless hi-res with a bit/kHz label", () => {
    expect(qualityForTrack(spec({}))).toEqual({ tier: "hires", label: "24-bit 48 kHz" });
    expect(qualityForTrack(spec({ format: "flac", sampleRateHz: 96000 }))).toEqual({
      tier: "hires",
      label: "24-bit 96 kHz",
    });
  });

  it("calls 16-bit lossless plain lossless -- CD quality is the boundary", () => {
    expect(qualityForTrack(spec({ bitDepth: 16, sampleRateHz: 44100 }))).toEqual({
      tier: "lossless",
      label: "16-bit 44.1 kHz",
    });
  });

  it("labels lossy as FORMAT + bitrate and never as bits", () => {
    expect(qualityForTrack(spec({ format: "mp3", bitDepth: null, bitrateKbps: 320 }))).toEqual({
      tier: "lossy",
      label: "MP3 320",
    });
    expect(qualityForTrack(spec({ format: "aac", bitDepth: null, bitrateKbps: 256 }))).toEqual({
      tier: "lossy",
      label: "AAC 256",
    });
  });

  it("degrades on missing metadata instead of rendering NaN", () => {
    // Real tags are missing fields constantly (epic #115) -- a null bit depth
    // on lossless assumes 16, a null bitrate on lossy drops the number.
    expect(qualityForTrack(spec({ bitDepth: null, sampleRateHz: 44100 }))).toEqual({
      tier: "lossless",
      label: "16-bit 44.1 kHz",
    });
    expect(qualityForTrack(spec({ format: "mp3", bitDepth: null, bitrateKbps: null }))).toEqual({
      tier: "lossy",
      label: "MP3",
    });
  });
});

describe("qualityForAlbum", () => {
  it("wears the best track's badge on a mixed album", () => {
    const q = qualityForAlbum([
      spec({ format: "mp3", bitDepth: null, bitrateKbps: 320 }),
      spec({ bitDepth: 16, sampleRateHz: 44100 }),
      spec({}),
    ]);
    expect(q).toEqual({ tier: "hires", label: "24-bit 48 kHz" });
  });

  it("survives an empty tracklist", () => {
    expect(qualityForAlbum([]).tier).toBe("lossy");
  });
});
