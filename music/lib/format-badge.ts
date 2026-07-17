// --- Pure format-pill derivation (issue #157, unit-tested) ---
// The nerdy sibling of quality.ts: where the quality badge states FIDELITY
// (16-bit 44.1 kHz), this pill states what the file IS -- codec and container.
// Same principle as the quality badge: a catalog lookup, never a probe. The
// codec answer is #149's stsd atom walk riding through trackFromRow.
//
// Label shapes:
//   m4a/mp4 container  -> "ALAC · M4A" / "AAC · M4A"  (codec and wrapper
//                         genuinely differ -- the whole reason to say both)
//   flac/wav/mp3       -> "FLAC" / "WAV" / "MP3"      (the extension IS the
//                         codec; "FLAC · FLAC" would be noise)

import type { Track } from "./types";

type FormatSpec = Pick<Track, "format" | "container">;

/** The container, from the catalog when it rode along, else derived from the
 * codec-level format: alac/aac only ever arrive in m4a here (the catalog's
 * m4a/mp4 split is the only multi-codec container this library holds). */
function containerOf(t: FormatSpec): string {
  if (t.container) return t.container;
  return t.format === "alac" || t.format === "aac" ? "m4a" : t.format;
}

export function formatBadgeForTrack(t: FormatSpec): string {
  const container = containerOf(t);
  if (container === "m4a" || container === "mp4") {
    return `${t.format.toUpperCase()} · ${container.toUpperCase()}`;
  }
  return container.toUpperCase();
}

/** One representative label for the album header, sitting beside the quality
 * badge (which wears the BEST track's tier). "Best" has no meaning between
 * containers, so this one is majoritarian instead: the label most of the
 * album's tracks would wear. Ties break lexicographically -- arbitrary but
 * stable, so a 50/50 album shows the same pill on every load. Empty album
 * -> null, the caller renders nothing. */
export function formatBadgeForAlbum(tracks: FormatSpec[]): string | null {
  const counts = new Map<string, number>();
  for (const t of tracks) {
    const label = formatBadgeForTrack(t);
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  let best: string | null = null;
  let bestN = 0;
  for (const [label, n] of counts) {
    if (n > bestN || (n === bestN && best !== null && label < best)) {
      best = label;
      bestN = n;
    }
  }
  return best;
}
