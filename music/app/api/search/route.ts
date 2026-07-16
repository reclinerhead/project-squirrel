// Search feed (issue #129). Exists because the search overlay is a client
// component and lib/api.ts went server-only: the overlay debounces keystrokes
// into GET /api/search?q=, this runs the tested scorer over a bounded LIKE
// sweep, and the grouped results come back in lib/search.ts's SearchResults
// shape. Under-2-char queries answer empty without touching the catalog --
// the same floor searchLibrary() itself enforces.

import { search } from "@/lib/api";

export async function GET(request: Request) {
  const q = new URL(request.url).searchParams.get("q") ?? "";
  return Response.json(await search(q), {
    headers: { "cache-control": "no-store" },
  });
}
