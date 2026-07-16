import { describe, expect, it } from "vitest";
import { queueView, removeUpcoming, shuffleUpcoming } from "./queue";
import type { Track } from "./types";

function t(id: string): Track {
  return {
    id,
    title: id,
    artistId: "a",
    artist: "A",
    albumId: "al",
    album: "Album",
    trackNo: 1,
    durationS: 180,
    format: "alac",
    bitDepth: 24,
    sampleRateHz: 48000,
    bitrateKbps: null,
    rating: 0,
  };
}

const SEQ = ["one", "two", "three", "four", "five"].map(t);

describe("queueView", () => {
  it("splits history / current / upNext around the cursor", () => {
    const v = queueView(SEQ, 2);
    expect(v.history.map((x) => x.id)).toEqual(["one", "two"]);
    expect(v.current?.id).toBe("three");
    expect(v.upNext.map((x) => x.id)).toEqual(["four", "five"]);
  });

  it("handles the edges: first track has no history, last has no upNext", () => {
    expect(queueView(SEQ, 0).history).toEqual([]);
    expect(queueView(SEQ, 4).upNext).toEqual([]);
  });

  it("returns the empty view for an out-of-range cursor or empty sequence", () => {
    expect(queueView(SEQ, -1)).toEqual({ history: [], current: null, upNext: [] });
    expect(queueView(SEQ, 5)).toEqual({ history: [], current: null, upNext: [] });
    expect(queueView([], 0)).toEqual({ history: [], current: null, upNext: [] });
  });
});

describe("removeUpcoming", () => {
  it("removes exactly the addressed up-next row, preserving order", () => {
    // cursor on "three"; upNext is [four, five]; remove upNext[0]
    const out = removeUpcoming(SEQ, 2, 0);
    expect(out.map((x) => x.id)).toEqual(["one", "two", "three", "five"]);
  });

  it("never touches history or the current track", () => {
    const out = removeUpcoming(SEQ, 2, 1);
    expect(out.slice(0, 3).map((x) => x.id)).toEqual(["one", "two", "three"]);
  });

  it("is a no-op for stale or out-of-range indexes", () => {
    expect(removeUpcoming(SEQ, 2, 5)).toBe(SEQ);
    expect(removeUpcoming(SEQ, 2, -1)).toBe(SEQ);
    expect(removeUpcoming(SEQ, 4, 0)).toBe(SEQ); // nothing upcoming
  });

  it("does not mutate the input sequence", () => {
    const before = [...SEQ];
    removeUpcoming(SEQ, 2, 0);
    expect(SEQ).toEqual(before);
  });
});

describe("shuffleUpcoming", () => {
  it("reorders only the tracks after the cursor", () => {
    // rng always 0 -> Fisher-Yates swaps every element to the front
    const out = shuffleUpcoming(SEQ, 1, () => 0);
    expect(out.slice(0, 2).map((x) => x.id)).toEqual(["one", "two"]);
    expect(new Set(out.slice(2).map((x) => x.id))).toEqual(new Set(["three", "four", "five"]));
  });

  it("is deterministic under an injected rng", () => {
    let calls = 0;
    const rng = () => [0.9, 0.1, 0.5][calls++ % 3];
    const a = shuffleUpcoming(SEQ, 0, rng);
    calls = 0;
    const b = shuffleUpcoming(SEQ, 0, rng);
    expect(a.map((x) => x.id)).toEqual(b.map((x) => x.id));
  });

  it("keeps the full set of tracks -- shuffle loses nothing", () => {
    const out = shuffleUpcoming(SEQ, 0, () => 0.42);
    expect(new Set(out.map((x) => x.id)).size).toBe(5);
  });
});
