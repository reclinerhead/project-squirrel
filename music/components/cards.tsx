// The two library cards (issue #118) -- extracted so home shelves and the
// browse pages render the exact same cards. The album card's information
// budget (cover, title, artist, year) is owner-approved as-is from #116;
// changes here change every surface at once, which is the point.

import Link from "next/link";
import { CoverArt } from "./CoverArt";
import type { Album, Artist } from "@/lib/types";

export function AlbumCard({ album }: { album: Album }) {
  return (
    <Link href={`/album/${album.id}`} className="group block min-w-0">
      <span className="relative block aspect-square overflow-hidden rounded-sm border border-line transition-colors group-hover:border-linebright">
        <CoverArt id={album.id} title={album.title} artHash={album.artHash} />
      </span>
      <span className="mt-2 block truncate text-sm text-ink">{album.title}</span>
      <span className="block truncate text-xs text-inkfaint">
        {/* year 0 = unknown (issue #167): the artist stands alone, no "· 0" */}
        {album.artist}
        {album.year ? ` · ${album.year}` : ""}
      </span>
    </Link>
  );
}

export function ArtistCard({ artist }: { artist: Artist }) {
  return (
    <Link href={`/artist/${artist.id}`} className="group block min-w-0">
      <span className="relative block aspect-square overflow-hidden rounded-full border border-line transition-colors group-hover:border-linebright">
        {/* the artist's own image (promoted cover today) wins; their first
            album's art is the fallback, and its SVG the fallback's fallback */}
        {artist.albums[0] && (
          <CoverArt
            id={artist.albums[0].id}
            title={artist.name}
            artHash={artist.artHash ?? artist.albums[0].artHash}
          />
        )}
      </span>
      <span className="mt-2 block truncate text-center text-sm text-inkdim transition-colors group-hover:text-ink">
        {artist.name}
      </span>
    </Link>
  );
}
