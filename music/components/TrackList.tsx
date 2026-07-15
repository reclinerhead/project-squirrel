"use client";

// Shared tracklist (issue #116) -- album pages and artist top-tracks both
// render this. The audibly-playing row wears --led and the eq glyph (green
// means LIVE); every other row shows its number in the same box, so playback
// moving through the list never changes a row's size. Rating controls are
// always mounted at full width and only fade -- reveal-on-hover must never
// reflow (rule #1).

import { usePlayer } from "./PlayerProvider";
import { EqGlyph } from "./EqGlyph";
import { RatingControl } from "./RatingControl";
import { formatDuration } from "@/lib/format";
import type { Track } from "@/lib/types";

export function TrackList({
  tracks,
  queue,
  playingFrom,
  showArtist = false,
  numbered = true,
}: {
  /** The rows to display. */
  tracks: Track[];
  /** What playTracks() enqueues on a row click -- usually the full album,
   * so clicking track 7 still queues 8, 9, 10 behind it. Defaults to the
   * displayed rows. */
  queue?: Track[];
  playingFrom: string;
  showArtist?: boolean;
  numbered?: boolean;
}) {
  const { view, isPlaying, playTracks, ratingFor, rate } = usePlayer();
  const playFrom = queue ?? tracks;

  return (
    <div>
      {tracks.map((t, row) => {
        const isCurrent = view.current?.id === t.id;
        return (
          <div
            key={t.id}
            role="button"
            tabIndex={0}
            onClick={() => {
              const i = playFrom.findIndex((x) => x.id === t.id);
              playTracks(playFrom, Math.max(i, 0), playingFrom);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                const i = playFrom.findIndex((x) => x.id === t.id);
                playTracks(playFrom, Math.max(i, 0), playingFrom);
              }
            }}
            className="group grid cursor-pointer grid-cols-[2rem_minmax(0,1fr)_auto_auto] items-center gap-3 rounded-sm px-3 py-2 transition-colors hover:bg-panel2"
          >
            <span className="flex w-8 items-center justify-center">
              {isCurrent ? (
                <EqGlyph paused={!isPlaying} className="text-led" />
              ) : (
                <span className="text-sm tabular-nums text-inkfaint">
                  {numbered ? t.trackNo : row + 1}
                </span>
              )}
            </span>
            <span className="min-w-0">
              <span className={`block truncate text-sm ${isCurrent ? "text-led" : "text-ink"}`}>
                {t.title}
              </span>
              {showArtist && <span className="block truncate text-xs text-inkfaint">{t.artist}</span>}
            </span>
            <RatingControl
              rating={ratingFor(t.id)}
              onRate={(c) => rate(t.id, c)}
              className={`transition-opacity ${
                ratingFor(t.id) !== 0 ? "" : "opacity-0 focus-within:opacity-100 group-hover:opacity-100"
              }`}
            />
            <span className="w-10 text-right text-sm tabular-nums text-inkfaint">
              {formatDuration(t.durationS)}
            </span>
          </div>
        );
      })}
    </div>
  );
}
