"use client";

// Client-side player state (issue #116). In v1 this simulates playback --
// elapsed time ticks, tracks auto-advance, the queue is real state -- so
// every control is exercisable before Phase 2's daemon exists. When it does,
// these actions become calls to it and the shape here stays put.

import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { getSeedQueue } from "@/lib/api";
import { queueView, removeUpcoming, shuffleUpcoming, type QueueView } from "@/lib/queue";
import { nextRating, type ThumbClick } from "@/lib/rating";
import type { Rating, Track } from "@/lib/types";

type PlayerState = {
  view: QueueView;
  playingFrom: string;
  isPlaying: boolean;
  elapsedS: number;
  shuffle: boolean;
  repeat: "off" | "all" | "one";
  outputId: string;
  ratingFor: (trackId: string) => Rating;

  playTracks: (tracks: Track[], startIndex: number, from: string) => void;
  togglePlay: () => void;
  next: () => void;
  prev: () => void;
  seek: (s: number) => void;
  rate: (trackId: string, click: ThumbClick) => void;
  removeUpNext: (upNextIndex: number) => void;
  toggleShuffle: () => void;
  cycleRepeat: () => void;
  setOutputId: (id: string) => void;
};

const PlayerContext = createContext<PlayerState | null>(null);

export function usePlayer(): PlayerState {
  const ctx = useContext(PlayerContext);
  if (!ctx) throw new Error("usePlayer outside <PlayerProvider>");
  return ctx;
}

export function PlayerProvider({ children }: { children: React.ReactNode }) {
  const seed = useMemo(() => getSeedQueue(), []);
  const [sequence, setSequence] = useState<Track[]>(seed.sequence);
  const [currentIndex, setCurrentIndex] = useState(seed.currentIndex);
  const [playingFrom, setPlayingFrom] = useState(seed.playingFrom);
  const [isPlaying, setIsPlaying] = useState(false);
  const [elapsedS, setElapsedS] = useState(0);
  const [shuffle, setShuffle] = useState(false);
  const [repeat, setRepeat] = useState<"off" | "all" | "one">("off");
  const [outputId, setOutputId] = useState("browser");
  const [ratings, setRatings] = useState<Record<string, Rating>>({});

  const view = useMemo(() => queueView(sequence, currentIndex), [sequence, currentIndex]);
  const current = view.current;

  // The simulated clock: a one-second timeout chain while "playing" -- each
  // tick schedules the next, and track end advances the cursor (honoring
  // repeat) inside the same callback. A seek changes elapsedS, which just
  // re-arms the chain from the new position. Real playback replaces this
  // with the daemon's reported position.
  useEffect(() => {
    if (!isPlaying || !current) return;
    const id = setTimeout(() => {
      const next = elapsedS + 1;
      if (next < current.durationS) {
        setElapsedS(next);
      } else if (repeat === "one") {
        setElapsedS(0);
      } else if (currentIndex + 1 < sequence.length) {
        setCurrentIndex(currentIndex + 1);
        setElapsedS(0);
      } else if (repeat === "all" && sequence.length > 0) {
        setCurrentIndex(0);
        setElapsedS(0);
      } else {
        setIsPlaying(false);
        setElapsedS(current.durationS);
      }
    }, 1000);
    return () => clearTimeout(id);
  }, [isPlaying, current, elapsedS, repeat, currentIndex, sequence.length]);

  const value: PlayerState = {
    view,
    playingFrom,
    isPlaying,
    elapsedS,
    shuffle,
    repeat,
    outputId,
    ratingFor: (trackId) => ratings[trackId] ?? 0,

    playTracks: (tracks, startIndex, from) => {
      if (tracks.length === 0) return;
      const i = Math.min(Math.max(startIndex, 0), tracks.length - 1);
      setSequence(tracks);
      setCurrentIndex(i);
      setPlayingFrom(from);
      setElapsedS(0);
      setIsPlaying(true);
    },
    togglePlay: () => {
      if (current) setIsPlaying((p) => !p);
    },
    next: () => {
      if (currentIndex + 1 < sequence.length) {
        setCurrentIndex(currentIndex + 1);
        setElapsedS(0);
      } else if (repeat === "all" && sequence.length > 0) {
        setCurrentIndex(0);
        setElapsedS(0);
      }
    },
    prev: () => {
      // The convention every player shares: early in a track means "previous
      // track", later means "restart this one".
      if (elapsedS > 3 || currentIndex === 0) {
        setElapsedS(0);
      } else {
        setCurrentIndex(currentIndex - 1);
        setElapsedS(0);
      }
    },
    seek: (s) => {
      if (current) setElapsedS(Math.min(Math.max(0, s), current.durationS));
    },
    rate: (trackId, click) => {
      setRatings((r) => ({ ...r, [trackId]: nextRating(r[trackId] ?? 0, click) }));
    },
    removeUpNext: (upNextIndex) => {
      setSequence((seq) => removeUpcoming(seq, currentIndex, upNextIndex));
    },
    toggleShuffle: () => {
      // Turning shuffle ON reorders what's upcoming, once. Math.random is
      // fine here -- the pure Fisher-Yates is in lib/queue.ts under test;
      // the component layer just supplies entropy.
      setShuffle((on) => {
        if (!on) setSequence((seq) => shuffleUpcoming(seq, currentIndex, Math.random));
        return !on;
      });
    },
    cycleRepeat: () => {
      setRepeat((r) => (r === "off" ? "all" : r === "all" ? "one" : "off"));
    },
    setOutputId,
  };

  return <PlayerContext.Provider value={value}>{children}</PlayerContext.Provider>;
}
