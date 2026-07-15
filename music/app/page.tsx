"use client";

// The library front door (issue #116): search lives in the chrome; this page
// is the browsable shelf -- every artist, then every album. Explore's
// pregenerated-playlist pills land here later as their own issue.

import Link from "next/link";
import { listArtists } from "@/lib/api";
import { CoverArt } from "@/components/CoverArt";

export default function Home() {
  const artists = listArtists();
  const albums = artists.flatMap((a) => a.albums).sort((x, y) => y.year - x.year);

  return (
    <div className="space-y-8">
      <section className="panel">
        <div className="flex items-baseline justify-between pb-3">
          <h2 className="text-lg text-ink" style={{ fontFamily: "var(--font-display)" }}>
            Artists
          </h2>
          <span className="stamp text-[10px] text-inkfaint">{artists.length} in the stacks</span>
        </div>
        <div className="grid grid-cols-3 gap-4 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6">
          {artists.map((a) => (
            <Link key={a.id} href={`/artist/${a.id}`} className="group min-w-0">
              <span className="relative block aspect-square overflow-hidden rounded-full border border-line transition-colors group-hover:border-linebright">
                {a.albums[0] && <CoverArt id={a.albums[0].id} title={a.name} />}
              </span>
              <span className="mt-2 block truncate text-center text-sm text-inkdim transition-colors group-hover:text-ink">
                {a.name}
              </span>
            </Link>
          ))}
        </div>
      </section>

      <section className="panel">
        <div className="flex items-baseline justify-between pb-3">
          <h2 className="text-lg text-ink" style={{ fontFamily: "var(--font-display)" }}>
            Albums
          </h2>
          <span className="stamp text-[10px] text-inkfaint">newest first</span>
        </div>
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5">
          {albums.map((al) => (
            <Link key={al.id} href={`/album/${al.id}`} className="group min-w-0">
              <span className="relative block aspect-square overflow-hidden rounded-sm border border-line transition-colors group-hover:border-linebright">
                <CoverArt id={al.id} title={al.title} />
              </span>
              <span className="mt-2 block truncate text-sm text-ink">{al.title}</span>
              <span className="block truncate text-xs text-inkfaint">
                {al.artist} · {al.year}
              </span>
            </Link>
          ))}
        </div>
      </section>
    </div>
  );
}
