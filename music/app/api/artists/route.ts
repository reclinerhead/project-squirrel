// Artist window feed (issue #118) -- the /api/albums contract, for artists.
// See that file's banner for why this shape exists and what replaces its
// innards at Phase 0.

import { browseArtists, type BrowseSort } from "@/lib/api";
import { PAGE_LIMIT } from "@/lib/browse";

export async function GET(request: Request) {
  const p = new URL(request.url).searchParams;
  const sort: BrowseSort = p.get("sort") === "new" ? "new" : "az";
  const genre = p.get("genre") || undefined;
  const offset = Number(p.get("offset")) || 0;
  const limit = Math.min(Math.max(1, Number(p.get("limit")) || PAGE_LIMIT), PAGE_LIMIT);

  const { items, total, nextOffset } = browseArtists({ genre, sort, offset, limit });
  return Response.json({ items, total, nextOffset });
}
