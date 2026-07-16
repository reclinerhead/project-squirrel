// Album page (issue #116), server half (issue #129): awaits the real catalog
// and hands the album to the client view, which owns the play buttons. The
// split exists because lib/api.ts is server-only (node:sqlite) while
// usePlayer() is client-only -- one component can't be both.

import Link from "next/link";
import { getAlbum } from "@/lib/api";
import { AlbumView } from "@/components/AlbumView";

export default async function AlbumPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const album = await getAlbum(decodeURIComponent(id));

  if (!album) {
    return (
      <section className="panel rounded-sm border border-line bg-panel px-4 py-6 text-sm text-inkdim">
        No album by that name in the stacks.{" "}
        <Link href="/" className="text-ink underline decoration-line underline-offset-4">
          Back to the library
        </Link>
        .
      </section>
    );
  }

  return <AlbumView album={album} />;
}
