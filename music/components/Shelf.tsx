// A home shelf (issue #118): titled, capped, TWO ROWS of album cards with an
// optional "view all" deep link. The two-row clamp and the column steps live
// in .shelf-grid (globals.css) -- a horizontal scrollbar was the first draft
// and it hid half the shelf behind a gesture. Shelves with no data render
// nothing at all -- an absent shelf is absence, not a reserved hole; the
// no-layout-shift rule governs within surfaces.

import Link from "next/link";
import { AlbumCard } from "./cards";
import type { Album } from "@/lib/types";

export function Shelf({
  title,
  note,
  albums,
  viewAllHref,
}: {
  title: string;
  note?: string;
  albums: Album[];
  viewAllHref?: string;
}) {
  if (albums.length === 0) return null;
  return (
    <section className="panel">
      <div className="flex items-baseline justify-between gap-3 pb-3">
        <h2 className="text-lg text-ink" style={{ fontFamily: "var(--font-display)" }}>
          {title}
        </h2>
        <span className="flex items-baseline gap-3">
          {note && <span className="stamp hidden text-[10px] text-inkfaint sm:inline">{note}</span>}
          {viewAllHref && (
            <Link
              href={viewAllHref}
              className="stamp text-[10px] text-inkdim underline decoration-line underline-offset-4 transition-colors hover:text-ink"
            >
              View all
            </Link>
          )}
        </span>
      </div>
      <div className="shelf-grid">
        {albums.map((al) => (
          <AlbumCard key={al.id} album={al} />
        ))}
      </div>
    </section>
  );
}
