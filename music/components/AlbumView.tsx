"use client";

// The album page's interactive body (issues #116, #129, #155, #157): the
// album's cover as a RECOGNIZABLE full-bleed backdrop (the TIDAL move,
// finally at TIDAL's volume), the cover itself big enough to anchor the
// page, metadata with the quality + format pills, the old heavy-blur wash
// demoted to the tracklist panel, and the now-playing row lit. Clicking any
// row queues
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
import { formatBadgeForAlbum } from "@/lib/format-badge";
import { qualityForAlbum } from "@/lib/quality";
import { usePlayer } from "@/components/PlayerProvider";
import { CoverArt } from "@/components/CoverArt";
import { FormatBadge } from "@/components/FormatBadge";
import { QualityBadge } from "@/components/QualityBadge";
import { TrackList } from "@/components/TrackList";
import SourceStamp from "@/components/SourceStamp";
import { PlayIcon, ShuffleIcon } from "@/components/icons";
import type { Album, ArtistBio } from "@/lib/types";

export function AlbumView({ album, about }: { album: Album; about?: ArtistBio | null }) {
  const { playTracks, toggleShuffle, shuffle } = usePlayer();
  const [lightbox, setLightbox] = useState(false);
  const [descOpen, setDescOpen] = useState(false);

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
  const formatLabel = formatBadgeForAlbum(album.tracks);

  const playAll = () => playTracks(album.tracks, 0, album.title);
  const shufflePlay = () => {
    playTracks(album.tracks, 0, album.title);
    if (!shuffle) toggleShuffle();
  };

  return (
    <div className="space-y-6">
      <section className="panel relative overflow-hidden rounded-sm border border-line bg-panel">
        {/* The cover as the backdrop -- RECOGNIZABLE now (issue #157). The
            old blur-2xl wash read as a color smear; TIDAL's move is a lightly
            softened band of the actual art, and object-cover on the wide box
            already crops the square cover to that middle slice. The slight
            scale keeps blur-softened edges off-frame. */}
        <div className="absolute inset-0 scale-105 opacity-75 blur-md saturate-[1.2]" aria-hidden>
          <CoverArt id={album.id} title="" artHash={album.artHash} size="large" focalY={album.artFocalY} />
        </div>
        {/* Two scrims share the legibility job now that the art shows: the
            house bottom-up fade, plus a bottom-left anchor under the text
            block specifically -- TIDAL's trick, darkest exactly where the
            title sits, scrim-free where the art is the point. */}
        <div className="absolute inset-0 bg-gradient-to-t from-panel via-panel/60 to-transparent" aria-hidden />
        <div className="absolute inset-0 bg-gradient-to-tr from-panel/90 via-transparent to-transparent" aria-hidden />

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
            {/* Typography scale is issue #157's contract: the title owns the
                page (wrapping welcome, never truncated), the artist is the
                clear second line, and the stamp row stays the small one --
                a notch up from before, but still the small one. */}
            <h1
              className="text-4xl text-ink sm:text-5xl lg:text-6xl"
              style={{ fontFamily: "var(--font-display)" }}
            >
              {album.title}
            </h1>
            <p className="mt-2 text-xl sm:text-2xl">
              <Link
                href={`/artist/${album.artistId}`}
                className="text-inkdim transition-colors hover:text-ink"
              >
                {album.artist}
              </Link>
            </p>
            <p className="stamp mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-inkfaint">
              {/* year 0 = unknown (issue #167): omit rather than stamp "0" */}
              {album.year ? <span>{album.year}</span> : null}
              <span>
                {album.tracks.length} tracks ({formatTotalDuration(totalS)})
              </span>
              <QualityBadge badge={badge} className="text-[10px]" />
              <FormatBadge label={formatLabel} className="text-[10px]" />
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

      <section className="panel relative overflow-hidden rounded-sm border border-line bg-panel">
        {/* The hero's OLD treatment, demoted downstairs (issue #157): the
            heavy-blur wash was too quiet for a hero but is exactly right
            behind dense rows. Top-anchored and dissolving into bg-panel so
            it reads as the header's atmosphere spilling over the seam --
            the bottom of a long list stays calm, and the faintest text on
            the page (numbers, durations) never sits on more than a whisper
            of it. Quieter than the hero ever was: opacity-25 vs its old 40. */}
        <div className="absolute inset-x-0 top-0 h-72 overflow-hidden" aria-hidden>
          <div className="absolute inset-0 scale-110 opacity-25 blur-2xl saturate-[1.2]">
            <CoverArt id={album.id} title="" artHash={album.artHash} size="large" focalY={album.artFocalY} />
          </div>
          <div className="absolute inset-0 bg-gradient-to-b from-transparent via-panel/40 to-panel" />
        </div>
        <div className="relative px-1 py-2">
          <TrackList tracks={album.tracks} playingFrom={album.title} />
        </div>
      </section>

      {/* About the artist (issue #170) -- the TIDAL/Apple Music idiom, below
          the tracklist. SERVER-RENDERED: `about` arrives as a prop from the
          page, already resolved, so the panel is present or absent on first
          paint with no client pop-in. An artist with no bio renders the page
          exactly as it did before this shipped -- no empty shell, no
          reserved gap, nothing to shift. */}
      {about && (
        <section className="panel rounded-sm border border-line bg-panel">
          <div className="flex items-baseline justify-between gap-3 px-4 pb-2 pt-3">
            <h2 className="text-lg text-ink" style={{ fontFamily: "var(--font-display)" }}>
              About {about.name}
            </h2>
            <Link
              href={`/artist/${about.id}`}
              className="stamp text-[10px] text-inkfaint underline decoration-line underline-offset-4 transition-colors hover:decoration-linebright"
            >
              artist page
            </Link>
          </div>
          <div className="px-4 pb-4">
            {/* Clamped to three lines, the artist hero's treatment. No
                read-more here on purpose: the full prose lives one click
                away on the artist page, and a second expand-in-place control
                would grow the page under the reader's cursor. */}
            <p className="line-clamp-3 max-w-3xl text-sm leading-relaxed text-inkdim">
              {about.bio}
            </p>
            <SourceStamp text={about.bio} src={about.bioSrc} url={about.bioUrl} />
          </div>
        </section>
      )}

      {/* The album's own copy (issue #171), below the artist panel because
          that is the order the issue specifies. Worth revisiting: album-first
          then artist-context reads more naturally, and it is a one-line swap.
          Absent entirely for the ~80% of albums whose tags carried nothing --
          no empty shell, nothing to shift. */}
      {album.description && (
        <section className="panel rounded-sm border border-line bg-panel">
          <div className="flex items-baseline justify-between gap-3 px-4 pb-2 pt-3">
            <h2 className="text-lg text-ink" style={{ fontFamily: "var(--font-display)" }}>
              About this album
            </h2>
          </div>
          <div className="px-4 pb-4">
            <p
              className={`max-w-3xl text-sm leading-relaxed text-inkdim ${
                descOpen ? "" : "line-clamp-3"
              }`}
            >
              {album.description}
            </p>
            {/* Read-more only when there is more (the artist hero's 260-char
                threshold). Expanding is user-initiated, so it does not
                violate the nothing-moves-on-its-own rule. */}
            {album.description.length > 260 && (
              <button
                type="button"
                onClick={() => setDescOpen((o) => !o)}
                className="mt-1 text-sm text-ink underline decoration-line underline-offset-4 transition-colors hover:decoration-linebright"
              >
                {descOpen ? "Read less" : "Read more"}
              </button>
            )}
            <SourceStamp text={album.description} src={album.descriptionSrc} />
          </div>
        </section>
      )}

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
