"use client";

// The persistent player bar (issue #116) -- the TIDAL-style deck pinned to
// the bottom of every route. Three clusters: now-playing (left), transport
// (center), utilities (right). The empty state renders the same skeleton at
// the same size as the playing state: rule #1, nothing may shift.

import { useRef, useState } from "react";
import { usePlayer } from "./PlayerProvider";
import { CoverArt } from "./CoverArt";
import { QualityBadge } from "./QualityBadge";
import { RatingControl } from "./RatingControl";
import { OutputPicker } from "./OutputPicker";
import { QueuePanel } from "./QueuePanel";
import {
  NextIcon,
  OutputIcon,
  PauseIcon,
  PlayIcon,
  PrevIcon,
  QueueIcon,
  RepeatIcon,
  ShuffleIcon,
  VolumeIcon,
} from "./icons";
import { formatDuration } from "@/lib/format";
import { qualityForTrack } from "@/lib/quality";

function SeekBar() {
  const { view, elapsedS, seek } = usePlayer();
  const barRef = useRef<HTMLDivElement>(null);
  const track = view.current;
  const duration = track?.durationS ?? 0;
  const ratio = duration > 0 ? Math.min(elapsedS / duration, 1) : 0;

  return (
    <div className="flex w-full items-center gap-2">
      <span className="w-9 shrink-0 text-right text-[10px] tabular-nums text-inkfaint">
        {track ? formatDuration(elapsedS) : "-:--"}
      </span>
      <div
        ref={barRef}
        role="slider"
        aria-label="Seek"
        aria-valuemin={0}
        aria-valuemax={duration}
        aria-valuenow={Math.floor(elapsedS)}
        tabIndex={track ? 0 : -1}
        className="group relative h-4 flex-1 cursor-pointer"
        onClick={(e) => {
          if (!track || !barRef.current) return;
          const r = barRef.current.getBoundingClientRect();
          seek(((e.clientX - r.left) / r.width) * duration);
        }}
        onKeyDown={(e) => {
          if (!track) return;
          if (e.key === "ArrowRight") seek(elapsedS + 5);
          if (e.key === "ArrowLeft") seek(elapsedS - 5);
        }}
      >
        <div className="absolute inset-x-0 top-1/2 h-[3px] -translate-y-1/2 rounded-full bg-line">
          <div
            className="h-full rounded-full bg-inkdim transition-colors group-hover:bg-ink"
            style={{ width: `${ratio * 100}%` }}
          />
        </div>
      </div>
      <span className="w-9 shrink-0 text-[10px] tabular-nums text-inkfaint">
        {track ? formatDuration(track.durationS) : "-:--"}
      </span>
    </div>
  );
}

function IconButton({
  label,
  active,
  onClick,
  children,
  className,
}: {
  label: string;
  active?: boolean;
  onClick: () => void;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      aria-pressed={active}
      title={label}
      onClick={onClick}
      className={`relative rounded-sm p-1.5 transition-colors ${
        active ? "text-ink" : "text-inkdim hover:text-ink"
      } ${className ?? ""}`}
    >
      {children}
      {/* active tick: present-but-invisible when off, so toggling shifts nothing */}
      <span
        className={`absolute bottom-0 left-1/2 h-[3px] w-[3px] -translate-x-1/2 rounded-full bg-led ${
          active ? "" : "invisible"
        }`}
      />
    </button>
  );
}

export function PlayerBar() {
  const player = usePlayer();
  const { view, isPlaying, shuffle, repeat, togglePlay, next, prev, toggleShuffle, cycleRepeat, ratingFor, rate } =
    player;
  const track = view.current;
  const [queueOpen, setQueueOpen] = useState(false);
  const [outputOpen, setOutputOpen] = useState(false);
  const [volume, setVolume] = useState(80);

  return (
    <>
      {queueOpen && <QueuePanel onClose={() => setQueueOpen(false)} />}
      <div className="fixed inset-x-0 bottom-0 z-30 border-t border-line bg-panel/95 backdrop-blur">
        <div className="grid h-[84px] grid-cols-[minmax(0,1fr)_auto] items-center gap-3 px-3 sm:h-[76px] sm:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)_minmax(0,1fr)] sm:gap-4 sm:px-4">
          {/* left: now playing -- skeleton keeps the exact box when idle */}
          <div className="flex min-w-0 items-center gap-3">
            <div className="relative h-12 w-12 shrink-0 overflow-hidden rounded-sm border border-line bg-panel2">
              {track && <CoverArt id={track.albumId} title={track.album} />}
            </div>
            <div className="min-w-0">
              <div className="truncate text-sm text-ink">
                {track ? track.title : <span className="text-inkfaint">Nothing on the platter</span>}
              </div>
              <div className="truncate text-xs text-inkdim">{track ? track.artist : " "}</div>
              <div className="hidden truncate text-[11px] text-inkfaint md:block">
                {track ? track.album : " "}
              </div>
            </div>
            {track ? (
              <RatingControl
                rating={ratingFor(track.id)}
                onRate={(c) => rate(track.id, c)}
                className="hidden shrink-0 sm:inline-flex"
              />
            ) : (
              <span className="hidden w-[68px] shrink-0 sm:inline-flex" aria-hidden />
            )}
          </div>

          {/* center: transport + seek */}
          <div className="flex flex-col items-center justify-center gap-1 sm:min-w-0">
            <div className="flex items-center gap-1 sm:gap-2">
              <IconButton label="Shuffle" active={shuffle} onClick={toggleShuffle} className="hidden sm:block">
                <ShuffleIcon className="h-4 w-4" />
              </IconButton>
              <IconButton label="Previous" onClick={prev}>
                <PrevIcon className="h-5 w-5" />
              </IconButton>
              <button
                type="button"
                aria-label={isPlaying ? "Pause" : "Play"}
                onClick={togglePlay}
                className="mx-1 flex h-10 w-10 items-center justify-center rounded-full bg-ink text-bg transition-transform hover:scale-105 active:scale-95"
              >
                {isPlaying ? <PauseIcon className="h-5 w-5" /> : <PlayIcon className="ml-0.5 h-5 w-5" />}
              </button>
              <IconButton label="Next" onClick={next}>
                <NextIcon className="h-5 w-5" />
              </IconButton>
              <IconButton
                label={repeat === "one" ? "Repeat one" : repeat === "all" ? "Repeat all" : "Repeat"}
                active={repeat !== "off"}
                onClick={cycleRepeat}
                className="hidden sm:block"
              >
                <span className="relative">
                  <RepeatIcon className="h-4 w-4" />
                  <span
                    className={`absolute -right-1 -top-1 text-[8px] font-bold tabular-nums ${
                      repeat === "one" ? "" : "invisible"
                    }`}
                  >
                    1
                  </span>
                </span>
              </IconButton>
            </div>
            <div className="hidden w-full max-w-md sm:block">
              <SeekBar />
            </div>
          </div>

          {/* right: utilities */}
          <div className="flex items-center justify-end gap-1 sm:gap-2">
            <IconButton label="Play queue" active={queueOpen} onClick={() => setQueueOpen((o) => !o)}>
              <QueueIcon className="h-4 w-4" />
            </IconButton>
            <div className="relative">
              <IconButton label="Sound output" active={outputOpen} onClick={() => setOutputOpen((o) => !o)}>
                <OutputIcon className="h-4 w-4" />
              </IconButton>
              {outputOpen && <OutputPicker onClose={() => setOutputOpen(false)} />}
            </div>
            <div className="hidden items-center gap-2 lg:flex">
              <VolumeIcon className="h-4 w-4 text-inkdim" />
              <input
                type="range"
                min={0}
                max={100}
                value={volume}
                onChange={(e) => setVolume(Number(e.target.value))}
                aria-label="Volume"
                className="h-1 w-20 cursor-pointer appearance-none rounded-full bg-line accent-(--ink)"
              />
            </div>
            {/* the brag pill -- reserves its slot with an invisible placeholder when idle */}
            <span className="hidden sm:block">
              {track ? (
                <QualityBadge badge={qualityForTrack(track)} />
              ) : (
                <span className="stamp invisible inline-flex rounded-full border px-2 py-0.5 text-[9px]">
                  24-bit 48 kHz
                </span>
              )}
            </span>
          </div>
        </div>
        {/* mobile seek: a hairline across the very top of the bar */}
        <div className="absolute inset-x-0 top-0 sm:hidden">
          <div className="h-[3px] bg-line">
            <div
              className="h-full bg-inkdim"
              style={{
                width: `${track && track.durationS > 0 ? Math.min(player.elapsedS / track.durationS, 1) * 100 : 0}%`,
              }}
            />
          </div>
        </div>
      </div>
    </>
  );
}
