// The album card catalog (issue #118): the whole library, loaded a window at
// a time as you scroll. This server component renders the first window and
// the chrome; <Browser> appends the rest from /api/albums.
//
// Filter/sort/letter are URL state and re-render here on the server; only the
// scroll-append is client-side. The <Browser> key is what enforces that
// distinction -- change a filter and you get a new list, not an appended one.

import { albumRail, browseAlbums, listGenres, type BrowseSort } from "@/lib/api";
import { Browser } from "@/components/Browser";
import { GenrePills, LetterRail, SortToggle } from "@/components/browse-ui";

export default async function AlbumsPage({
  searchParams,
}: {
  searchParams: Promise<{ genre?: string; sort?: string; letter?: string }>;
}) {
  const sp = await searchParams;
  const genres = listGenres();
  const genre = genres.includes(sp.genre ?? "") ? sp.genre : undefined;
  const sort: BrowseSort = sp.sort === "new" ? "new" : "az";

  // The rail is meaningless on a newest-first list, so it isn't offered --
  // and a letter in the URL is likewise ignored there.
  const rail = sort === "az" ? albumRail(genre) : [];
  const letter = sort === "az" ? sp.letter : undefined;
  const start = rail.find((r) => r.letter === letter)?.offset ?? 0;

  const { items, total, nextOffset } = browseAlbums({ genre, sort, offset: start });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h1 className="text-2xl text-ink" style={{ fontFamily: "var(--font-display)" }}>
          Albums
          {genre && <span className="text-inkdim"> · {genre}</span>}
        </h1>
        <SortToggle base="/albums" sort={sort} extra={{ genre }} />
      </div>

      <GenrePills base="/albums" genres={genres} active={genre} sort={sort} />
      <LetterRail base="/albums" rail={rail} active={letter} extra={{ genre }} />

      <Browser
        key={`albums|${genre ?? ""}|${sort}|${letter ?? ""}`}
        kind="albums"
        initialItems={items}
        initialNextOffset={nextOffset}
        startOffset={start}
        total={total}
        query={{ genre, sort }}
      />
    </div>
  );
}
