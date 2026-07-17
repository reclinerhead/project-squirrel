import { describe, expect, it } from "vitest";
import { formatBadgeForAlbum, formatBadgeForTrack } from "./format-badge";
import type { Track } from "./types";

function spec(over: Partial<Track>): Pick<Track, "format" | "container"> {
  return { format: "alac", container: "m4a", ...over };
}

describe("formatBadgeForTrack", () => {
  it("states codec AND container when they differ -- the m4a family", () => {
    expect(formatBadgeForTrack(spec({}))).toBe("ALAC · M4A");
    expect(formatBadgeForTrack(spec({ format: "aac" }))).toBe("AAC · M4A");
    expect(formatBadgeForTrack(spec({ format: "aac", container: "mp4" }))).toBe("AAC · MP4");
  });

  it("collapses to one token where the extension IS the codec", () => {
    // "FLAC · FLAC" would be noise -- single token, no redundancy.
    expect(formatBadgeForTrack(spec({ format: "flac", container: "flac" }))).toBe("FLAC");
    expect(formatBadgeForTrack(spec({ format: "wav", container: "wav" }))).toBe("WAV");
    expect(formatBadgeForTrack(spec({ format: "mp3", container: "mp3" }))).toBe("MP3");
  });

  it("derives the container when it didn't ride along -- fixture/daemon shapes", () => {
    // Pre-container shapes (lib/fixtures.ts, the daemon's /queue payload)
    // carry only the codec-level format; alac/aac imply the m4a wrapper.
    expect(formatBadgeForTrack(spec({ container: undefined }))).toBe("ALAC · M4A");
    expect(formatBadgeForTrack(spec({ format: "aac", container: null }))).toBe("AAC · M4A");
    expect(formatBadgeForTrack(spec({ format: "mp3", container: undefined }))).toBe("MP3");
  });
});

describe("formatBadgeForAlbum", () => {
  it("wears the majority label on a mixed album", () => {
    expect(
      formatBadgeForAlbum([spec({}), spec({}), spec({ format: "mp3", container: "mp3" })]),
    ).toBe("ALAC · M4A");
  });

  it("breaks a tie deterministically -- same pill on every load", () => {
    const fifty = [spec({}), spec({ format: "mp3", container: "mp3" })];
    expect(formatBadgeForAlbum(fifty)).toBe("ALAC · M4A");
    expect(formatBadgeForAlbum([...fifty].reverse())).toBe("ALAC · M4A");
  });

  it("returns null for an empty tracklist -- the caller renders nothing", () => {
    expect(formatBadgeForAlbum([])).toBeNull();
  });
});
