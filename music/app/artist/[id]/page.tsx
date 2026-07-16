// Artist page (issue #116), server half (issue #129): awaits the catalog and
// the play_history-ranked top tracks, hands both to the client view. Same
// split as /album/[id] -- lib/api.ts is server-only, usePlayer() is client.

import Link from "next/link";
import { getArtist, getTopTracks } from "@/lib/api";
import { ArtistView } from "@/components/ArtistView";

export default async function ArtistPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const decoded = decodeURIComponent(id);
  const artist = await getArtist(decoded);

  if (!artist) {
    return (
      <section className="panel rounded-sm border border-line bg-panel px-4 py-6 text-sm text-inkdim">
        No artist by that name in the stacks.{" "}
        <Link href="/" className="text-ink underline decoration-line underline-offset-4">
          Back to the library
        </Link>
        .
      </section>
    );
  }

  const topTracks = await getTopTracks(decoded);
  return <ArtistView artist={artist} topTracks={topTracks} />;
}
