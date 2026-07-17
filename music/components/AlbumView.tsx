"use client";

// The album page's interactive body (issues #116, #129, #155): the album's
// cover blurred into a full-bleed backdrop (the TIDAL move), the cover
// itself big enough to anchor the page, metadata with the quality badge,
// and the tracklist with the now-playing row lit. Clicking any row queues
// the whole album from that point; clicking the cover opens the FULL-SIZE
// original in a lightbox -- the untouched bytes the extractor stored, the
// one surface that serves /orig. Hand-rolled overlay on the OutputPicker's
// backdrop idiom rather than a dependency: it's one image and two ways to
// close (click-away, Escape).
//
// Client component because the play buttons need usePlayer(); the DATA
// arrives as a prop from the server page, which is what owns talking to the
// catalog now that lib/api.ts is server-only.

import { useEffect, useState } from "react";
import Link from "next/link";
import { formatTotalDuration } from "@/lib/format";
import { qualityForAlbum } from "@/lib/quality";
import { usePlayer } from "@/components/PlayerProvider";
import { CoverArt } from "@/components/CoverArt";
import { QualityBadge } from "@/components/QualityBadge";
import { TrackList } from "@/components/TrackList";
import { PlayIcon, ShuffleIcon } from "@/components/icons";
import type { Album } from "@/lib/types";

export function AlbumView({ album }: { album: Album }) {
  const { playTracks, toggleShuffle, shuffle } = usePlayer();
  const [lightbox, setLightbox] = useState(false);

  // Escape closes the lightbox -- bound only while it's open, so the page's
  // ordinary keys are untouched the rest of the time.
  useEffect(() => {
    if (!lightbox) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setLightbox(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [lightbox]);

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
          <CoverArt id={album.id} title="" artHash={album.artHash} size="large" />
        </div>
        <div className="absolute inset-0 bg-gradient-to-t from-panel via-panel/70 to-transparent" aria-hidden />

        <div className="relative flex flex-col gap-5 px-5 pb-5 pt-6 sm:flex-row sm:items-end sm:px-6">
          {/* The cover anchors the page now (issue #155: it had shipped
              thumbnail-sized). A button exactly when there's art to zoom --
              the generated SVG has no "full size" to show. */}
          {album.artHash ? (
            <button
              type="button"
              onClick={() => setLightbox(true)}
              aria-label={`View full-size cover art for ${album.title}`}
              className="relative block h-64 w-64 shrink-0 cursor-zoom-in overflow-hidden rounded-sm border border-linebright shadow-[0_12px_40px_rgba(0,0,0,0.5)] transition-transform hover:scale-[1.01] sm:h-96 sm:w-96"
            >
              <CoverArt id={album.id} title={album.title} artHash={album.artHash} size="large" />
            </button>
          ) : (
            <span className="relative block h-64 w-64 shrink-0 overflow-hidden rounded-sm border border-linebright shadow-[0_12px_40px_rgba(0,0,0,0.5)] sm:h-96 sm:w-96">
              <CoverArt id={album.id} title={album.title} artHash={album.artHash} size="large" />
            </span>
          )}
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

      {lightbox && album.artHash && (
        // The full-size original (issue #155) -- /orig's untouched bytes,
        // finally on a surface. OutputPicker's backdrop idiom at full
        // bleed: the backdrop IS the close button, Escape works (effect
        // above), and the figure rides the house .panel reveal.
        <div
          role="dialog"
          aria-modal="true"
          aria-label={`Full-size cover art: ${album.title}`}
          className="fixed inset-0 z-50 flex items-center justify-center"
        >
          <button
            type="button"
            aria-label="Close full-size cover art"
            onClick={() => setLightbox(false)}
            className="absolute inset-0 cursor-zoom-out bg-black/85 backdrop-blur-sm"
          />
          <figure className="panel pointer-events-none relative m-4">
            {/* eslint-disable-next-line @next/next/no-img-element -- see CoverArt */}
            <img
              src={`/api/art/${album.artHash}/orig`}
              alt={`Cover art: ${album.title}`}
              className="max-h-[88vh] max-w-[92vw] rounded-sm border border-linebright object-contain shadow-[0_24px_80px_rgba(0,0,0,0.8)]"
            />
            <figcaption className="stamp mt-2 text-center text-[10px] text-inkfaint">
              {album.title} · original scan
            </figcaption>
          </figure>
        </div>
      )}
    </div>
  );
}
