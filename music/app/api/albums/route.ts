// Album window feed (issue #118). The browse page server-renders its first
// window; this is what infinite scroll asks for every window after that.
//
// It exists so the client never holds the catalog: at 27k tracks, shipping
// the library to the browser to slice it there is the exact failure the
// browse pages were built to avoid. The contract is offset+limit in, items +
// nextOffset out -- which is `LIMIT ? OFFSET ?` wearing a URL, so Phase 0
// replaces the browseAlbums() call with a query against music.db and nothing
// upstream changes (mcc/app/weather/history/route.ts is the precedent: open
// the DB read-only per request, degrade quietly).
//
// Under /api/ rather than mcc's bare /weather/history shape only because
// /albums is already a page route here and route.ts would collide with it.

import { browseAlbums, type BrowseSort } from "@/lib/api";
import { PAGE_LIMIT } from "@/lib/browse";

export async function GET(request: Request) {
  const p = new URL(request.url).searchParams;
  const sort: BrowseSort = p.get("sort") === "new" ? "new" : "az";
  const genre = p.get("genre") || undefined;
  const offset = Number(p.get("offset")) || 0;
  // Clamp the limit: a client asking for 27,000 rows at once is either a bug
  // or someone poking the URL, and neither should get to allocate the world.
  const limit = Math.min(Math.max(1, Number(p.get("limit")) || PAGE_LIMIT), PAGE_LIMIT);

  const { items, total, nextOffset } = await browseAlbums({ genre, sort, offset, limit });
  return Response.json({ items, total, nextOffset });
}
