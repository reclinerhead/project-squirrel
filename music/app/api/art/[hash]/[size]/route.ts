// The art server (issue #153): a plain file read from the art store, and
// nothing else -- every image was content-addressed and pre-sized by the
// extraction pass on pearl, so this route does zero image work, ever.
//
// IMMUTABLE BY CONSTRUCTION: the URL's hash IS the content, so a changed
// cover is a different URL and this response can promise max-age=1y,
// immutable. After the first visit a grid renders with zero art requests --
// that is the whole caching story, and it lives in one header.
//
// The hash is wire input and meets the filesystem, so it runs the allowlist
// gauntlet first (frame_archiver's guard genre, same as track ids): exactly
// 32 hex chars or the request dies before any path is built. Sizes are a
// closed set for the same reason.
//
// MERLE_MUSIC_ART: the store's absolute path (no default -- dev boxes
// without a synced store serve 404s and the GUI keeps its generated SVGs,
// the kill-switch convention).

import { readFile } from "node:fs/promises";
import { join } from "node:path";

const HASH_RE = /^[0-9a-f]{32}$/;

// size -> (filename suffix, declared type). `.orig` is extension-less on
// disk (the pass stores untouched bytes); its type is sniffed from magic
// numbers because a name is a claim and the first bytes are a fact.
const SIZES: Record<string, { suffix: string; type: string | null }> = {
  thumb: { suffix: ".thumb.webp", type: "image/webp" },
  large: { suffix: ".large.webp", type: "image/webp" },
  orig: { suffix: ".orig", type: null },
};

function sniff(bytes: Buffer): string {
  if (bytes[0] === 0xff && bytes[1] === 0xd8) return "image/jpeg";
  if (bytes[0] === 0x89 && bytes[1] === 0x50) return "image/png";
  if (bytes.subarray(8, 12).toString("latin1") === "WEBP") return "image/webp";
  return "application/octet-stream";
}

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ hash: string; size: string }> },
) {
  const { hash, size } = await params;
  const spec = SIZES[size];
  if (!spec || !HASH_RE.test(hash)) {
    return new Response("not art", { status: 400 });
  }
  const root = process.env.MERLE_MUSIC_ART?.trim();
  if (!root) {
    return new Response("art store not configured", { status: 404 });
  }
  try {
    const bytes = await readFile(join(root, hash + spec.suffix));
    return new Response(new Uint8Array(bytes), {
      headers: {
        "content-type": spec.type ?? sniff(bytes),
        "cache-control": "public, max-age=31536000, immutable",
      },
    });
  } catch {
    return new Response("no such art", { status: 404 });
  }
}
