// One album by id (issue #129): what the search overlay's track rows fetch
// so "click a track" can queue its whole album from that point -- the same
// gesture the fixture era had, minus the in-memory library that made it free.
// 404 keeps the not-found contract of the [id] page; the overlay just
// degrades to playing nothing.

import { getAlbum } from "@/lib/api";

export async function GET(request: Request) {
  const id = new URL(request.url).searchParams.get("id");
  if (!id) return Response.json({ error: "id required" }, { status: 422 });
  const album = await getAlbum(id);
  if (!album) return Response.json({ error: "unknown album" }, { status: 404 });
  return Response.json(album, { headers: { "cache-control": "no-store" } });
}
