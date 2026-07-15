// --- Pure quality-badge derivation (unit-tested in quality.test.ts) ---
// The badge is a catalog lookup, never a probe (epic #115, principle 4):
// Phase 0 indexes format/bit-depth/sample-rate, so what a track IS was
// decided at index time and this just renders it. Three tiers:
//   hires    -- lossless, >16-bit: the gold pill, TIDAL-style bragging
//   lossless -- CD-quality lossless: quietly stated
//   lossy    -- format + bitrate, no ceremony

import { formatKhz } from "./format";
import type { QualityTier, Track } from "./types";

const LOSSLESS_FORMATS = new Set(["alac", "flac", "wav"]);

export type QualityBadge = { tier: QualityTier; label: string };

export function qualityForTrack(
  t: Pick<Track, "format" | "bitDepth" | "sampleRateHz" | "bitrateKbps">,
): QualityBadge {
  if (LOSSLESS_FORMATS.has(t.format)) {
    const bits = t.bitDepth ?? 16;
    const khz = t.sampleRateHz != null ? `${formatKhz(t.sampleRateHz)} kHz` : "";
    const label = khz ? `${bits}-bit ${khz}` : `${bits}-bit`;
    return { tier: bits > 16 ? "hires" : "lossless", label };
  }
  const rate = t.bitrateKbps != null ? ` ${t.bitrateKbps}` : "";
  return { tier: "lossy", label: `${t.format.toUpperCase()}${rate}` };
}

/** An album wears its best track's badge -- mixed-format albums exist in the
 * real library, and "best" matches how a listener would brag about it. */
export function qualityForAlbum(
  tracks: Pick<Track, "format" | "bitDepth" | "sampleRateHz" | "bitrateKbps">[],
): QualityBadge {
  const order: QualityTier[] = ["hires", "lossless", "lossy"];
  let best: QualityBadge | null = null;
  for (const t of tracks) {
    const q = qualityForTrack(t);
    if (best === null || order.indexOf(q.tier) < order.indexOf(best.tier)) best = q;
  }
  return best ?? { tier: "lossy", label: "" };
}
