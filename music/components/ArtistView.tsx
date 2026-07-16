"use client";

// The artist page's interactive body (issues #116, #129): hero, bio with
// read-more, top tracks, and the discography grid. Top tracks are
// play_history-ranked now -- the section stays hidden until listening
// accumulates, which is the same rendering the fixture era promised.
// Similar-artists is a named follow-up, not a missing feature.

import { useState } from "react";
import Link from "next/link";
import { coverParams } from "@/lib/cover";
import { usePlayer } from "@/components/PlayerProvider";
import { CoverArt } from "@/components/CoverArt";
import { TrackList } from "@/components/TrackList";
import { PlayIcon, ShuffleIcon } from "@/components/icons";
import type { Artist, Track } from "@/lib/types";

export function ArtistView({ artist, topTracks }: { artist: Artist; topTracks: Track[] }) {
  const { playTracks, toggleShuffle, shuffle } = usePlayer();
  const [bioOpen, setBioOpen] = useState(false);

  const allTracks = artist.albums.flatMap((al) => al.tracks);
  const { hue1 } = coverParams(artist.albums[0]?.id ?? artist.id);
  const longBio = artist.bio.length > 260;

  const playAll = () => playTracks(allTracks, 0, artist.name);
  const shufflePlay = () => {
    playTracks(allTracks, 0, artist.name);
    if (!shuffle) toggleShuffle();
  };

  return (
    <div className="space-y-8">
      {/* hero: the artist's own hue as a dark wash, name in display type */}
      <section
        className="panel relative overflow-hidden rounded-sm border border-line"
        style={{
          background: `linear-gradient(160deg, hsl(${hue1} 40% 20%) 0%, var(--panel) 70%)`,
        }}
      >
        <div className="relative px-5 pb-5 pt-16 sm:px-6 sm:pt-24">
          <h1 className="text-4xl text-ink sm:text-5xl" style={{ fontFamily: "var(--font-display)" }}>
            {artist.name}
          </h1>
          <p className="stamp mt-2 text-[10px] text-inkdim">
            {artist.albums.length} {artist.albums.length === 1 ? "album" : "albums"} · {allTracks.length} tracks
          </p>
          {artist.bio && (
            <p className={`mt-3 max-w-2xl text-sm leading-relaxed text-inkdim ${bioOpen ? "" : "line-clamp-3"}`}>
              {artist.bio}
            </p>
          )}
          {longBio && (
            <button
              type="button"
              onClick={() => setBioOpen((o) => !o)}
              className="mt-1 text-sm text-ink underline decoration-line underline-offset-4 transition-colors hover:decoration-linebright"
            >
              {bioOpen ? "Read less" : "Read more"}
            </button>
          )}
          <div className="mt-5 flex items-center gap-3">
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
      </section>

      {topTracks.length > 0 && (
        <section className="panel rounded-sm border border-line bg-panel">
          <div className="flex items-baseline justify-between gap-3 px-4 pb-2 pt-3">
            <h2 className="text-lg text-ink" style={{ fontFamily: "var(--font-display)" }}>
              Top Tracks
            </h2>
            <span className="stamp text-[10px] text-inkfaint">ranked by your listening</span>
          </div>
          <div className="px-1 pb-2">
            <TrackList tracks={topTracks} playingFrom={artist.name} numbered={false} />
          </div>
        </section>
      )}

      <section className="panel rounded-sm border border-line bg-panel">
        <div className="flex items-baseline justify-between gap-3 px-4 pb-2 pt-3">
          <h2 className="text-lg text-ink" style={{ fontFamily: "var(--font-display)" }}>
            Discography
          </h2>
        </div>
        <div className="grid grid-cols-2 gap-4 px-4 pb-4 sm:grid-cols-3 md:grid-cols-4">
          {artist.albums
            .slice()
            .sort((a, b) => b.year - a.year)
            .map((al) => (
              <Link key={al.id} href={`/album/${al.id}`} className="group min-w-0">
                <span className="relative block aspect-square overflow-hidden rounded-sm border border-line transition-colors group-hover:border-linebright">
                  <CoverArt id={al.id} title={al.title} />
                </span>
                <span className="mt-2 block truncate text-sm text-ink">{al.title}</span>
                <span className="block text-xs text-inkfaint">{al.year}</span>
              </Link>
            ))}
        </div>
      </section>
    </div>
  );
}
