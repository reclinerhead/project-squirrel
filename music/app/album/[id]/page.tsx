"use client";

// Album page (issue #116): the album's own generated cover blurred into a
// full-bleed backdrop (the TIDAL move), metadata with the quality badge, and
// the tracklist with the now-playing row lit. Clicking any row queues the
// whole album from that point.

import { use } from "react";
import Link from "next/link";
import { getAlbum } from "@/lib/api";
import { formatTotalDuration } from "@/lib/format";
import { qualityForAlbum } from "@/lib/quality";
import { usePlayer } from "@/components/PlayerProvider";
import { CoverArt } from "@/components/CoverArt";
import { QualityBadge } from "@/components/QualityBadge";
import { TrackList } from "@/components/TrackList";
import { PlayIcon, ShuffleIcon } from "@/components/icons";

export default function AlbumPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const album = getAlbum(id);
  const { playTracks, toggleShuffle, shuffle } = usePlayer();

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

  const totalS = album.tracks.reduce((s, t) => s + t.durationS, 0);
  const badge = qualityForAlbum(album.tracks);

  const playAll = () => playTracks(album.tracks, 0, album.title);
  const shufflePlay = () => {
    playTracks(album.tracks, 0, album.title);
    if (!shuffle) toggleShuffle();
  };

  return (
    <div className="space-y-6">
      <section className="panel relative overflow-hidden rounded-sm border border-line bg-panel">
        {/* the cover itself, blown out and blurred, as the backdrop */}
        <div className="absolute inset-0 opacity-40 blur-2xl saturate-[1.2]" aria-hidden>
          <CoverArt id={album.id} title="" />
        </div>
        <div className="absolute inset-0 bg-gradient-to-t from-panel via-panel/70 to-transparent" aria-hidden />

        <div className="relative flex flex-col gap-5 px-5 pb-5 pt-6 sm:flex-row sm:items-end sm:px-6">
          <span className="relative block h-40 w-40 shrink-0 overflow-hidden rounded-sm border border-linebright shadow-[0_12px_40px_rgba(0,0,0,0.5)] sm:h-48 sm:w-48">
            <CoverArt id={album.id} title={album.title} />
          </span>
          <div className="min-w-0">
            <h1 className="text-3xl text-ink sm:text-4xl" style={{ fontFamily: "var(--font-display)" }}>
              {album.title}
            </h1>
            <p className="mt-1 text-sm">
              <Link
                href={`/artist/${album.artistId}`}
                className="text-inkdim transition-colors hover:text-ink"
              >
                {album.artist}
              </Link>
            </p>
            <p className="stamp mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-inkfaint">
              <span>{album.year}</span>
              <span>
                {album.tracks.length} tracks ({formatTotalDuration(totalS)})
              </span>
              <QualityBadge badge={badge} />
            </p>
            <div className="mt-4 flex items-center gap-3">
              <button
                type="button"
                onClick={playAll}
                className="flex items-center gap-2 rounded-full bg-ink px-5 py-2 text-sm font-medium text-bg transition-transform hover:scale-[1.03] active:scale-95"
              >
                <PlayIcon className="h-4 w-4" /> Play
              </button>
              <button
                type="button"
                onClick={shufflePlay}
                className="flex items-center gap-2 rounded-full border border-linebright px-5 py-2 text-sm text-ink transition-colors hover:bg-panel2"
              >
                <ShuffleIcon className="h-4 w-4" /> Shuffle
              </button>
            </div>
          </div>
        </div>
      </section>

      <section className="panel rounded-sm border border-line bg-panel">
        <div className="px-1 py-2">
          <TrackList tracks={album.tracks} playingFrom={album.title} />
        </div>
      </section>
    </div>
  );
}
