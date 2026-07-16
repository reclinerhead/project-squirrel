// The artist card catalog (issue #118) -- the page TIDAL refuses to build.
// Same mechanics as /albums: first window server-rendered, the rest appended
// by <Browser> from /api/artists.
//
// Genre here filters on "has any album in this genre" rather than on an
// artist-level field, because artists span genres in a real library.
// "Newest" means most recent release -- the question "what have they done
// lately" actually asks.

import { artistRail, browseArtists, listGenres, type BrowseSort } from "@/lib/api";
import { Browser } from "@/components/Browser";
import { GenrePills, LetterRail, SortToggle } from "@/components/browse-ui";

export default async function ArtistsPage({
  searchParams,
}: {
  searchParams: Promise<{ genre?: string; sort?: string; letter?: string }>;
}) {
  const sp = await searchParams;
  const genres = await listGenres();
  const genre = genres.includes(sp.genre ?? "") ? sp.genre : undefined;
  const sort: BrowseSort = sp.sort === "new" ? "new" : "az";

  const rail = sort === "az" ? await artistRail(genre) : [];
  const letter = sort === "az" ? sp.letter : undefined;
  const start = rail.find((r) => r.letter === letter)?.offset ?? 0;

  const { items, total, nextOffset } = await browseArtists({ genre, sort, offset: start });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h1 className="text-2xl text-ink" style={{ fontFamily: "var(--font-display)" }}>
          Artists
          {genre && <span className="text-inkdim"> · {genre}</span>}
        </h1>
        <SortToggle base="/artists" sort={sort} extra={{ genre }} />
      </div>

      <GenrePills base="/artists" genres={genres} active={genre} sort={sort} />
      <LetterRail base="/artists" rail={rail} active={letter} extra={{ genre }} />

      <Browser
        key={`artists|${genre ?? ""}|${sort}|${letter ?? ""}`}
        kind="artists"
        initialItems={items}
        initialNextOffset={nextOffset}
        startOffset={start}
        total={total}
        query={{ genre, sort }}
      />
    </div>
  );
}
