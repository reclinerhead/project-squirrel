"use client";

// Client-side player state (issue #116), wired to the real daemon (issue
// #129). The queue lives HERE -- the daemon knows one track at a time, on
// purpose (a server-side queue is Phase 3's engine's job, not plumbing's) --
// but time and truth come from pearl: a 2s poll of /api/player/state supplies
// the position, and "the track ended" is observed (the daemon's watcher
// adjudicated it and cleared its slate), never simulated. The v1 fake clock
// is gone; if the poll can't reach the daemon, the bar freezes at the last
// known position rather than inventing progress.
//
// The seed queue arrives as a server-rendered prop from layout.tsx (the app
// opens mid-album on whatever played last, paused) -- this component can't
// fetch it itself without a loading flash, and the layout is a server
// component that already knows.

import { createContext, useContext, useEffect, useMemo, useRef, useState } from "react";
import { queueView, removeUpcoming, shuffleUpcoming, type QueueView } from "@/lib/queue";
import { nextRating, type ThumbClick } from "@/lib/rating";
import type { Rating, Track } from "@/lib/types";

const POLL_MS = 2000;

export type SeedQueue = {
  sequence: Track[];
  currentIndex: number;
  playingFrom: string;
};

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

/** Fire a verb at the daemon via the proxy. False means it didn't happen --
 * callers keep their optimistic state honest. Never throws: a daemon that's
 * down makes the controls inert, not the app broken. */
async function post(verb: string, body?: object): Promise<boolean> {
  try {
    const res = await fetch(`/api/player/${verb}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
    return res.ok;
  } catch {
    return false;
  }
}

type DaemonState = {
  transport: string;
  position_s: number | null;
  track: { id: string } | null;
};

export function PlayerProvider({
  seed,
  children,
}: {
  seed: SeedQueue;
  children: React.ReactNode;
}) {
  const [sequence, setSequence] = useState<Track[]>(seed.sequence);
  const [currentIndex, setCurrentIndex] = useState(seed.currentIndex);
  const [playingFrom, setPlayingFrom] = useState(seed.playingFrom);
  const [isPlaying, setIsPlaying] = useState(false);
  const [elapsedS, setElapsedS] = useState(0);
  const [shuffle, setShuffle] = useState(false);
  const [repeat, setRepeat] = useState<"off" | "all" | "one">("off");
  const [outputId, setOutputId] = useState("denon");
  const [ratings, setRatings] = useState<Record<string, Rating>>({});
  // Whether the daemon currently holds OUR track -- distinguishes "paused"
  // (resumable with a bare play) from "never started / already adjudicated".
  const loadedRef = useRef(false);

  const view = useMemo(() => queueView(sequence, currentIndex), [sequence, currentIndex]);
  const current = view.current;

  const startTrack = async (track: Track, output: string) => {
    setElapsedS(0);
    setIsPlaying(true); // optimistic; corrected below if the daemon refuses
    const ok = await post("play", { track_id: track.id, output });
    loadedRef.current = ok;
    if (!ok) setIsPlaying(false);
  };

  // The poll: while we believe something is playing, ask pearl what's true.
  // Position flows down; the end of a track is recognized by the daemon's
  // slate going empty (its watcher recorded the history row and cleared),
  // and THEN the queue advances -- order matters, or a fast next-click could
  // double-record.
  useEffect(() => {
    if (!isPlaying || !current) return;
    let busy = false;
    const id = setInterval(async () => {
      if (busy) return; // a slow poll must not stack behind itself
      busy = true;
      try {
        const res = await fetch("/api/player/state", { cache: "no-store" });
        if (!res.ok) return; // daemon down: freeze, don't invent
        const s = (await res.json()) as DaemonState;
        if (s.track && s.track.id === current.id) {
          if (typeof s.position_s === "number") setElapsedS(s.position_s);
          return;
        }
        // Our track is off the daemon's slate: it ended (or was stopped at
        // the AVR itself -- same fact, the session with this track is over).
        loadedRef.current = false;
        if (repeat === "one") {
          void startTrack(current, outputId);
        } else if (currentIndex + 1 < sequence.length) {
          setCurrentIndex(currentIndex + 1);
          void startTrack(sequence[currentIndex + 1], outputId);
        } else if (repeat === "all" && sequence.length > 0) {
          setCurrentIndex(0);
          void startTrack(sequence[0], outputId);
        } else {
          setIsPlaying(false);
          setElapsedS(current.durationS);
        }
      } catch {
        // network blip: keep the last known position, try again next tick
      } finally {
        busy = false;
      }
    }, POLL_MS);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPlaying, current?.id, currentIndex, sequence, repeat, outputId]);

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
      void startTrack(tracks[i], outputId);
    },
    togglePlay: () => {
      if (!current) return;
      if (isPlaying) {
        setIsPlaying(false);
        void post("pause");
      } else if (loadedRef.current) {
        setIsPlaying(true);
        void post("play"); // bare play = resume the pause
      } else {
        void startTrack(current, outputId); // seed queue's first real start
      }
    },
    next: () => {
      if (currentIndex + 1 < sequence.length) {
        setCurrentIndex(currentIndex + 1);
        void startTrack(sequence[currentIndex + 1], outputId);
      } else if (repeat === "all" && sequence.length > 0) {
        setCurrentIndex(0);
        void startTrack(sequence[0], outputId);
      }
    },
    prev: () => {
      // The convention every player shares: early in a track means "previous
      // track", later means "restart this one". Restart re-issues play
      // rather than seeking to 0: the Denon answers AVTransport Seek with
      // HTTP 500 on external streams (verified against the real AVR), while
      // a fresh SetAVTransportURI+Play always works.
      if (elapsedS > 3 || currentIndex === 0) {
        if (current) void startTrack(current, outputId);
      } else {
        setCurrentIndex(currentIndex - 1);
        void startTrack(sequence[currentIndex - 1], outputId);
      }
    },
    seek: (s) => {
      // Optimistic; the 2s poll restores the truth. On the Denon that means
      // a scrub snaps back -- it refuses Seek (see prev) -- which is honest:
      // the bar shows where the music actually is. The browser output (2b)
      // seeks for real via Range.
      if (!current) return;
      const clamped = Math.min(Math.max(0, s), current.durationS);
      setElapsedS(clamped);
      if (loadedRef.current) void post("seek", { seconds: clamped });
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
