// The album card catalog (issue #118): the whole library, paginated, with
// genre filter, sort toggles, and an A-Z rail -- all URL state, all links,
// no client JS. No page ever renders more than PER_PAGE cards, which is the
// entire design constraint at 27k tracks.

import { browseAlbums, listGenres, type BrowseSort } from "@/lib/api";
import { lettersPresent, pageForLetter } from "@/lib/browse";
import { AlbumCard } from "@/components/cards";
import { buildQuery, LetterRail, Pager, SortToggle } from "@/components/browse-ui";
import Link from "next/link";

export default async function AlbumsPage({
  searchParams,
}: {
  searchParams: Promise<{ genre?: string; sort?: string; page?: string }>;
}) {
  const sp = await searchParams;
  const genres = listGenres();
  const genre = genres.includes(sp.genre ?? "") ? sp.genre : undefined;
  const sort: BrowseSort = sp.sort === "az" ? "az" : sp.sort === "new" ? "new" : "az";
  const page = Number(sp.page) || 1;

  const { items, pageInfo, total, names } = browseAlbums({ genre, sort, page });
  const extra = { genre };
  const letters = sort === "az" ? lettersPresent(names) : [];
  const pageByLetter = Object.fromEntries(letters.map((l) => [l, pageForLetter(names, l)]));
  // guard against the impossible-but-ugly: a letter whose page came back -1
  for (const l of letters) if (pageByLetter[l] === -1) pageByLetter[l] = 1;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h1 className="text-2xl text-ink" style={{ fontFamily: "var(--font-display)" }}>
          Albums
          {genre && <span className="text-inkdim"> · {genre}</span>}
        </h1>
        <SortToggle base="/albums" sort={sort} extra={extra} />
      </div>

      {/* genre pills, current one lit; "All" clears the filter */}
      <nav aria-label="Genre filter" className="scrollpane -mx-1 flex gap-2 overflow-x-auto px-1 pb-1">
        <Link
          href={`/albums${buildQuery({ sort })}`}
          className={`stamp shrink-0 rounded-full border px-4 py-1.5 text-[10px] transition-colors ${
            !genre ? "border-linebright bg-panel2 text-ink" : "border-line text-inkdim hover:text-ink"
          }`}
        >
          All
        </Link>
        {genres.map((g) => (
          <Link
            key={g}
            href={`/albums${buildQuery({ genre: g, sort })}`}
            className={`stamp shrink-0 whitespace-nowrap rounded-full border px-4 py-1.5 text-[10px] transition-colors ${
              genre === g ? "border-linebright bg-panel2 text-ink" : "border-line text-inkdim hover:text-ink"
            }`}
          >
            {g}
          </Link>
        ))}
      </nav>

      {letters.length > 0 && (
        <LetterRail base="/albums" letters={letters} pageByLetter={pageByLetter} extra={extra} />
      )}

      {items.length === 0 ? (
        <section className="panel rounded-sm border border-line bg-panel px-4 py-6 text-sm text-inkdim">
          Nothing on this shelf.
        </section>
      ) : (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5">
          {items.map((al) => (
            <AlbumCard key={al.id} album={al} />
          ))}
        </div>
      )}

      <Pager base="/albums" pageInfo={pageInfo} total={total} what="albums" extra={{ ...extra, sort }} />
    </div>
  );
}
