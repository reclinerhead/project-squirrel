// The front door (issue #118). No longer an index -- a curation surface:
// genre pills, then bounded shelves. The full catalog lives at /artists and
// /albums. Server component on purpose: the rediscovery seed is computed
// here, at the request boundary, so the client never runs Date-in-render
// (the known hydration trap) -- and force-dynamic keeps the seed from being
// frozen into a static prerender at build time.

import Link from "next/link";
import { getShelves, libraryCounts, listGenres } from "@/lib/api";
import { Shelf } from "@/components/Shelf";

export const dynamic = "force-dynamic";

function todaySeed(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

export default function Home() {
  const shelves = getShelves(todaySeed());
  const genres = listGenres();
  const counts = libraryCounts();

  return (
    <div className="space-y-8">
      {/* genre pills -- each deep-links into the filtered album browse */}
      <nav aria-label="Genres" className="scrollpane -mx-1 flex gap-2 overflow-x-auto px-1 pb-1">
        {genres.map((g) => (
          <Link
            key={g}
            href={`/albums?genre=${encodeURIComponent(g)}`}
            className="stamp shrink-0 whitespace-nowrap rounded-full border border-line bg-panel px-4 py-1.5 text-[10px] text-inkdim transition-colors hover:border-linebright hover:text-ink"
          >
            {g}
          </Link>
        ))}
      </nav>

      <Shelf
        title="Recently played"
        note="fixture history · play_history later"
        albums={shelves.recentlyPlayed}
      />
      <Shelf
        title="Today's dig"
        note="changes daily · nothing you've played lately"
        albums={shelves.rediscovery}
      />
      <Shelf
        title="Recently added"
        note="by year until the catalog has ingest dates"
        albums={shelves.recentlyAdded}
        viewAllHref="/albums?sort=new"
      />

      {/* the card catalog: full-library browse, the thing TIDAL won't build */}
      <section className="panel flex flex-wrap items-baseline gap-x-6 gap-y-2 rounded-sm border border-line bg-panel px-4 py-3">
        <span className="stamp text-[10px] text-inkfaint">The full stacks</span>
        <Link
          href="/artists"
          className="text-sm text-inkdim underline decoration-line underline-offset-4 transition-colors hover:text-ink"
        >
          {counts.artists.toLocaleString()} artists
        </Link>
        <Link
          href="/albums"
          className="text-sm text-inkdim underline decoration-line underline-offset-4 transition-colors hover:text-ink"
        >
          {counts.albums.toLocaleString()} albums
        </Link>
        <span className="text-sm text-inkfaint">{counts.tracks.toLocaleString()} tracks</span>
      </section>
    </div>
  );
}
