// Serves the archived event still shots (issue #90) from MERLE_FRAMES_DIR --
// the folder frame_archiver.py fills on pearl. This is the one image path
// that does NOT ride the /daemon proxy: daemon-down is the steady state for
// the 24/7 MCC, and the whole point of archiving on pearl is that journal
// thumbnails survive bluejay's nap. The browser never reaches bluejay for
// journal images.
//
// GET /frames/<id>          the full-size variant (annotated, stream-scale)
// GET /frames/<id>?thumb=1  the ~320px thumbnail the Field Journal renders
//
// Missing file, unset dir, or an unsafe id all answer a QUIET 404 -- a pruned
// image is the normal end of the retention window, not an error worth a
// journal line (the client shows a placeholder in the reserved slot).

import { promises as fs } from "fs";
import path from "path";
import type { NextRequest } from "next/server";
import { frameFilename } from "@/lib/frames";

export async function GET(
  req: NextRequest,
  ctx: { params: Promise<{ id: string }> },
) {
  const { id } = await ctx.params;
  const dir = process.env.MERLE_FRAMES_DIR;
  const name = frameFilename(id, req.nextUrl.searchParams.has("thumb"));
  if (!dir || !name) return new Response(null, { status: 404 });

  let bytes: Buffer;
  try {
    bytes = await fs.readFile(path.join(dir, name));
  } catch {
    return new Response(null, { status: 404 }); // pruned or never archived
  }
  // A frame_id's bytes never change once archived, so the browser may cache
  // forever -- journal republishes re-render entries without re-fetching.
  return new Response(new Uint8Array(bytes), {
    headers: {
      "content-type": "image/jpeg",
      "cache-control": "public, max-age=31536000, immutable",
    },
  });
}
