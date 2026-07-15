// --- Pure generative cover-art parameters (unit-tested in cover.test.ts) ---
// The fixture library has no real artwork, and empty gray squares would make
// every layout decision a lie. Instead each album gets a deterministic
// generated cover: a stable hash of its id picks two hues and a pattern, and
// the CoverArt component renders them as SVG. Deterministic matters twice --
// the same album must look the same on every page, and the album-page
// backdrop must match its own cover.

export type CoverParams = {
  hue1: number; // 0-359
  hue2: number; // 0-359, offset from hue1 so covers are two-toned
  pattern: number; // 0-3: rings, beams, blocks, waves
};

/** djb2 -- tiny, stable, good-enough spread for a fixture library. */
export function hashString(s: string): number {
  let h = 5381;
  for (let i = 0; i < s.length; i++) {
    h = (h * 33) ^ s.charCodeAt(i);
  }
  return h >>> 0;
}

export function coverParams(id: string): CoverParams {
  const h = hashString(id);
  const hue1 = h % 360;
  // 90-270 degrees away: always a real second color, never a near-duplicate.
  const hue2 = (hue1 + 90 + ((h >>> 9) % 180)) % 360;
  const pattern = (h >>> 17) % 4;
  return { hue1, hue2, pattern };
}
