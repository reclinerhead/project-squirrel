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
import { trackFromRow, type TrackRow } from "@/lib/catalog-rows";
import { queueView, removeUpcoming, shuffleUpcoming, type QueueView } from "@/lib/queue";
import { nextRating, type ThumbClick } from "@/lib/rating";
import type { Rating, Track } from "@/lib/types";

const POLL_MS = 2000;

// Radio mode (issue #139): how many tracks each engine fill returns, and how
// few upcoming tracks trigger the next one. Count-based, not time-based --
// clockless, and the provider already knows its index.
const RADIO_FILL_N = 25;
const RADIO_REFILL_AT = 3;

/** What the engine can be seeded with today; mood/weather are Phase 5's. */
export type RadioSeed = { track_id: string } | { artist: string };

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
  /** Takes the track, not an id: the catalog's rating rides on it (the
   * baseline every surface already receives), and the map below holds only
   * what THIS session changed. An id alone couldn't answer for a track that
   * isn't in the queue -- an album page's rows, for instance. */
  ratingFor: (track: Track) => Rating;

  playTracks: (tracks: Track[], startIndex: number, from: string) => void;
  /** Seed the engine, replace the queue, play. The queue then refills itself
   * near its end for as long as radio mode holds -- never-ending on purpose. */
  startRadio: (seed: RadioSeed, from: string) => void;
  togglePlay: () => void;
  next: () => void;
  prev: () => void;
  seek: (s: number) => void;
  rate: (track: Track, click: ThumbClick) => void;
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

/** One engine fill: TrackRow shapes off the wire, mapped through the same
 * tested trackFromRow every other surface uses. Empty on any failure -- a
 * daemon that's down makes radio inert, not the app broken. */
async function fetchRadioFill(seed: RadioSeed, exclude: string[]): Promise<Track[]> {
  try {
    const res = await fetch("/api/player/queue", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ seed, n: RADIO_FILL_N, exclude }),
    });
    if (!res.ok) return [];
    const body = (await res.json()) as { tracks?: TrackRow[] };
    return (body.tracks ?? []).map(trackFromRow);
  } catch {
    return [];
  }
}

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
  // Session edits only -- the catalog's answer arrives on each Track and is
  // the baseline (issue #135). Not seeded from the queue: a map that started
  // as the truth would have to be re-seeded on every navigation, and the
  // tracks already carry it.
  const [ratings, setRatings] = useState<Record<string, Rating>>({});
  // Whether the daemon currently holds OUR track -- distinguishes "paused"
  // (resumable with a bare play) from "never started / already adjudicated".
  const loadedRef = useRef(false);
  // Radio mode: the seed the engine keeps refilling from, null when the
  // queue is an ordinary album/artist sequence. Any explicit playTracks
  // exits radio -- the listener chose something, stop generating.
  const [radioSeed, setRadioSeed] = useState<RadioSeed | null>(null);
  const refillBusy = useRef(false);

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

  // The refill loop: when radio mode runs low on upcoming tracks, fetch the
  // next window with EVERYTHING this queue holds excluded -- played, playing,
  // and upcoming -- so a refill never repeats it (the engine's own cooldown
  // covers what actually played; the exclusion list covers what hasn't yet).
  // Continuous play is this loop, not a server-side queue: the daemon stays
  // one-track-at-a-time and generates lists on request.
  useEffect(() => {
    if (!radioSeed) return;
    if (sequence.length - 1 - currentIndex > RADIO_REFILL_AT) return;
    if (refillBusy.current) return;
    refillBusy.current = true;
    void fetchRadioFill(radioSeed, sequence.map((t) => t.id)).then((more) => {
      refillBusy.current = false;
      if (more.length === 0) return; // engine dry or daemon down: play out what's left
      // The id filter is a belt over the engine's suspenders: the sequence
      // may have grown (a second refill racing a slow first) between the
      // fetch and this append.
      setSequence((seq) => [...seq, ...more.filter((t) => !seq.some((s) => s.id === t.id))]);
    });
  }, [radioSeed, sequence, currentIndex]);

  const value: PlayerState = {
    view,
    playingFrom,
    isPlaying,
    elapsedS,
    shuffle,
    repeat,
    outputId,
    // ?? not ||: a cleared rating is 0, which is falsy but is the answer.
    ratingFor: (track) => ratings[track.id] ?? track.rating,

    playTracks: (tracks, startIndex, from) => {
      if (tracks.length === 0) return;
      setRadioSeed(null); // an explicit pick ends radio mode
      const i = Math.min(Math.max(startIndex, 0), tracks.length - 1);
      setSequence(tracks);
      setCurrentIndex(i);
      setPlayingFrom(from);
      void startTrack(tracks[i], outputId);
    },
    startRadio: (seed, from) => {
      void fetchRadioFill(seed, []).then((tracks) => {
        // An empty fill (unknown seed, daemon down, engine dry) leaves the
        // current queue alone -- radio that can't start must not eat what
        // was playing.
        if (tracks.length === 0) return;
        setRadioSeed(seed);
        setSequence(tracks);
        setCurrentIndex(0);
        setPlayingFrom(from);
        void startTrack(tracks[0], outputId);
      });
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
    rate: (track, click) => {
      // Optimistic, like startTrack: a thumb must land under the finger. The
      // daemon owns the timestamp, so the body carries only what it can't
      // know. value 0 (the third click) is a real message -- it clears.
      const was = ratings[track.id] ?? track.rating;
      const now = nextRating(was, click);
      setRatings((r) => ({ ...r, [track.id]: now }));
      void post("rate", { track_id: track.id, value: now }).then((ok) => {
        if (ok) return;
        // Roll back only if this click is still the one showing. The control
        // is built to be clicked in a burst (set -> escalate -> clear), so a
        // slow failed POST must not stomp a newer click's state.
        setRatings((r) => (r[track.id] === now ? { ...r, [track.id]: was } : r));
      });
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
