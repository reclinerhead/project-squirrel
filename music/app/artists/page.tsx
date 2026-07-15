// The artist card catalog (issue #118) -- the page TIDAL refuses to build.
// Same mechanics as /albums: URL state, links only, one page of cards max.
// "Newest" here means most recent release, which is the question "what have
// they done lately" actually asks.

import { browseArtists, type BrowseSort } from "@/lib/api";
import { lettersPresent, pageForLetter } from "@/lib/browse";
import { ArtistCard } from "@/components/cards";
import { LetterRail, Pager, SortToggle } from "@/components/browse-ui";

export default async function ArtistsPage({
  searchParams,
}: {
  searchParams: Promise<{ sort?: string; page?: string }>;
}) {
  const sp = await searchParams;
  const sort: BrowseSort = sp.sort === "new" ? "new" : "az";
  const page = Number(sp.page) || 1;

  const { items, pageInfo, total, names } = browseArtists({ sort, page });
  const letters = sort === "az" ? lettersPresent(names) : [];
  const pageByLetter = Object.fromEntries(letters.map((l) => [l, Math.max(1, pageForLetter(names, l))]));

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h1 className="text-2xl text-ink" style={{ fontFamily: "var(--font-display)" }}>
          Artists
        </h1>
        <SortToggle base="/artists" sort={sort} />
      </div>

      {letters.length > 0 && (
        <LetterRail base="/artists" letters={letters} pageByLetter={pageByLetter} />
      )}

      {items.length === 0 ? (
        <section className="panel rounded-sm border border-line bg-panel px-4 py-6 text-sm text-inkdim">
          Nobody on this shelf.
        </section>
      ) : (
        <div className="grid grid-cols-3 gap-4 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6">
          {items.map((a) => (
            <ArtistCard key={a.id} artist={a} />
          ))}
        </div>
      )}

      <Pager base="/artists" pageInfo={pageInfo} total={total} what="artists" extra={{ sort }} />
    </div>
  );
}
