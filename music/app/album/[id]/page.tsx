// Album page (issue #116), server half (issue #129): awaits the real catalog
// and hands the album to the client view, which owns the play buttons. The
// split exists because lib/api.ts is server-only (node:sqlite) while
// usePlayer() is client-only -- one component can't be both.

import Link from "next/link";
import { getAlbum, getArtistBio } from "@/lib/api";
import { AlbumView } from "@/components/AlbumView";

export default async function AlbumPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const album = await getAlbum(decodeURIComponent(id));
  // The About panel's prose (issue #170), resolved HERE rather than in the
  // client view so the panel is part of the first paint -- no pop-in, no
  // layout shift. Sequential after getAlbum because it needs the artist
  // name; it is one indexed point read, not a second traversal.
  const about = album ? await getArtistBio(album.artist) : null;

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

  return <AlbumView album={album} about={about} />;
}
