"use client";

// Client-side player state (issue #116), wired to the real daemon (issue
// #129). The queue lives HERE -- the daemon knows one track at a time, on
// purpose (a server-side queue is Phase 3's engine's job, not plumbing's).
//
// TWO TRANSPORTS, ONE PROVIDER (issue #149). On the Denon, time and truth
// come from pearl: a 2s poll of /api/player/state supplies the position, and
// "the track ended" is observed (the daemon's watcher adjudicated it),
// never simulated -- if the poll can't reach the daemon, the bar freezes
// rather than inventing progress. On the BROWSER output the hidden <audio>
// element below IS the transport: position is its currentTime, the end is
// its `ended` event, seeking is real (Range against the cached FLAC), and
// the poll stays off. History still lands on pearl either way -- the
// element can't be polled from there, so this side REPORTS each session's
// end (POST report) and the daemon adjudicates completed-vs-skipped with
// the same rule the watcher uses.
//
// The audio src comes from the daemon's own stream_base (fetched once via
// the proxy), NOT through a Next route: piping audio through the app server
// would re-buffer the stream and break Range for no gain.
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

// The remembered sound output (issue #169). Per-browser on purpose: "this
// browser" is itself a per-browser output, so a server-side preference would
// be lying the moment a second device opened the app. Read in the state
// initializer rather than a mount effect -- nothing rendered depends on
// outputId (the picker only mounts on click), so there's no hydration
// mismatch to dodge, and starting on the stored value means no window where
// the provider believes it's on the Denon.
const OUTPUT_KEY = "music.outputId";

function storedOutputId(): string {
  // Storage can throw outright (Safari private mode, blocked third-party
  // contexts) -- an unreadable preference is just an absent one.
  try {
    return window.localStorage.getItem(OUTPUT_KEY) || "denon";
  } catch {
    return "denon";
  }
}

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
  stream_base?: string;
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
  const [outputId, setOutputIdState] = useState(() =>
    typeof window === "undefined" ? "denon" : storedOutputId(),
  );
  // The browser transport (issue #149): the hidden element, the daemon's
  // stream base (fetched once, on first need), and which track the element
  // currently holds -- the outgoing session that gets a skip report when
  // the listener moves on.
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const streamBaseRef = useRef<string | null>(null);
  const browserLoadedRef = useRef<string | null>(null);
  // Session edits only -- the catalog's answer arrives on each Track and is
  // the baseline (issue #135). Not seeded from the queue: a map that started
  // as the truth would have to be re-seeded on every navigation, and the
  // tracks already carry it.
  const [ratings, setRatings] = useState<Record<string, Rating>>({});
  // Whether the current transport holds OUR track -- the daemon's slate on
  // the Denon, the <audio> element's src on the browser. Distinguishes
  // "paused" (resumable in place) from "never started / already adjudicated".
  const loadedRef = useRef(false);
  // Radio mode: the seed the engine keeps refilling from, null when the
  // queue is an ordinary album/artist sequence. Any explicit playTracks
  // exits radio -- the listener chose something, stop generating.
  const [radioSeed, setRadioSeed] = useState<RadioSeed | null>(null);
  const refillBusy = useRef(false);

  const view = useMemo(() => queueView(sequence, currentIndex), [sequence, currentIndex]);
  const current = view.current;

  /** The daemon's LAN-visible stream URL base, learned from /state on first
   * need and kept for the session -- server config stays server-side (the
   * proxy's rule); this is the one value the <audio> element genuinely
   * needs, because the audio itself must not ride the proxy. */
  const streamBase = async (): Promise<string | null> => {
    if (streamBaseRef.current) return streamBaseRef.current;
    try {
      const res = await fetch("/api/player/state", { cache: "no-store" });
      if (!res.ok) return null;
      const s = (await res.json()) as DaemonState;
      streamBaseRef.current = s.stream_base ?? null;
    } catch {
      return null;
    }
    return streamBaseRef.current;
  };

  /** Report the element's current session to play_history and forget it.
   * Fire-and-forget: history is pearl's bookkeeping, and a lost report
   * costs one row, never playback. */
  const reportBrowserSession = () => {
    const el = audioRef.current;
    const id = browserLoadedRef.current;
    if (!el || !id) return;
    if (el.currentTime > 0) {
      void post("report", { track_id: id, position_s: el.currentTime });
    }
    browserLoadedRef.current = null;
  };

  const startTrack = async (track: Track, output: string) => {
    setElapsedS(0);
    setIsPlaying(true); // optimistic; corrected below if the start fails
    if (output === "browser") {
      const el = audioRef.current;
      const base = await streamBase();
      if (!el || !base) {
        setIsPlaying(false); // daemon unreachable: inert, not broken
        return;
      }
      // Moving on mid-track is the skip signal -- same fact the daemon's
      // own /play records for the Denon, reported here before the src swap
      // tears the session down.
      reportBrowserSession();
      browserLoadedRef.current = track.id;
      el.src = `${base}/stream/${encodeURIComponent(track.id)}?output=browser`;
      try {
        await el.play();
        loadedRef.current = true;
      } catch {
        // Autoplay refusals and dead streams land here; the bar goes back
        // to paused instead of pretending.
        loadedRef.current = false;
        browserLoadedRef.current = null;
        setIsPlaying(false);
      }
      return;
    }
    const ok = await post("play", { track_id: track.id, output });
    loadedRef.current = ok;
    if (!ok) setIsPlaying(false);
  };

  /** What happens when the current track's session is over and the queue
   * should move -- one rule for both transports: the poll's slate-cleared
   * branch (Denon) and the element's `ended` event (browser) both land
   * here, so repeat/advance behavior cannot drift between outputs. */
  const advanceAfterEnd = () => {
    loadedRef.current = false;
    if (repeat === "one" && current) {
      void startTrack(current, outputId);
    } else if (currentIndex + 1 < sequence.length) {
      setCurrentIndex(currentIndex + 1);
      void startTrack(sequence[currentIndex + 1], outputId);
    } else if (repeat === "all" && sequence.length > 0) {
      setCurrentIndex(0);
      void startTrack(sequence[0], outputId);
    } else {
      setIsPlaying(false);
      if (current) setElapsedS(current.durationS);
    }
  };

  // The poll: while we believe something is playing, ask pearl what's true.
  // Position flows down; the end of a track is recognized by the daemon's
  // slate going empty (its watcher recorded the history row and cleared),
  // and THEN the queue advances -- order matters, or a fast next-click could
  // double-record.
  useEffect(() => {
    // The browser transport doesn't poll: its truth is the <audio> element
    // in this very tab -- timeupdate supplies the position, `ended` the
    // advance. Polling pearl about it would be asking someone else how
    // we're feeling.
    if (outputId === "browser") return;
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
        advanceAfterEnd();
      } catch {
        // network blip: keep the last known position, try again next tick
      } finally {
        busy = false;
      }
    }, POLL_MS);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPlaying, current?.id, currentIndex, sequence, repeat, outputId]);

  // Queue warming (issue #149): in browser mode, name the next two tracks
  // to the daemon whenever the playhead moves, so their transcodes run
  // while the current one plays and a queue advance is always a cache hit.
  // Fire-and-forget -- the daemon skips raw formats and cache hits itself,
  // and a lost warm costs a few seconds of cold start, never playback.
  useEffect(() => {
    if (outputId !== "browser") return;
    const next = sequence
      .slice(currentIndex + 1, currentIndex + 3)
      .map((t) => t.id);
    if (next.length > 0) void post("precache", { track_ids: next });
  }, [outputId, currentIndex, sequence]);

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
      if (outputId === "browser") {
        const el = audioRef.current;
        if (isPlaying) {
          el?.pause(); // a pause holds the element; nothing to tell pearl
          setIsPlaying(false);
        } else if (loadedRef.current && el &&
                   browserLoadedRef.current === current.id) {
          void el.play();
          setIsPlaying(true);
        } else {
          void startTrack(current, outputId);
        }
        return;
      }
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
      // Optimistic; the truth restores it either way. On the Denon the 2s
      // poll snaps a scrub back -- it refuses Seek (see prev) -- which is
      // honest: the bar shows where the music actually is. On the browser
      // the seek is REAL (Range against the cached FLAC); the one place the
      // element declines is the first seconds of a cold first play, where
      // the tail isn't transcoded yet -- timeupdate then keeps the bar on
      // the audio's actual position rather than the wish.
      if (!current) return;
      const clamped = Math.min(Math.max(0, s), current.durationS);
      setElapsedS(clamped);
      if (outputId === "browser") {
        const el = audioRef.current;
        if (el && loadedRef.current) {
          try {
            el.currentTime = clamped;
          } catch {
            // unseekable (cold stream): the element keeps playing; honest
          }
        }
        return;
      }
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
    setOutputId: (id) => {
      if (id === outputId) return;
      // Switching outputs mid-track STOPS rather than migrates (the v1
      // rule): the two transports don't share a position, and a silent
      // handoff that restarts the song from zero would feel like a bug
      // wearing a feature's name. The next play lands on the new output.
      if (isPlaying || loadedRef.current) {
        if (outputId === "browser") {
          reportBrowserSession(); // the abandoned session is a skip
          const el = audioRef.current;
          if (el) {
            el.pause();
            el.removeAttribute("src");
            el.load(); // actually release the stream, not just mute it
          }
        } else {
          void post("stop");
        }
        loadedRef.current = false;
        setIsPlaying(false);
        setElapsedS(0);
      }
      setOutputIdState(id);
      // Remembered for the next session (issue #169). Written on the way out
      // of a real switch, so a stored id that no longer exists self-heals the
      // next time the listener picks something.
      try {
        window.localStorage.setItem(OUTPUT_KEY, id);
      } catch {
        // A browser that won't store it still switches -- just not stickily.
      }
    },
  };

  return (
    <PlayerContext.Provider value={value}>
      {/* The browser output's transport (issue #149): parked and srcless in
          Denon mode, the actual player in browser mode. Hidden because the
          player bar is the UI -- this element is plumbing, and it reserves
          no space so nothing shifts (rule #1). */}
      <audio
        ref={audioRef}
        hidden
        preload="auto"
        onTimeUpdate={(e) => setElapsedS(e.currentTarget.currentTime)}
        onEnded={() => {
          // The element reached the end: report the completion (position
          // == duration, which the daemon's outcome_for credits), then
          // move the queue with the same rule the Denon poll uses.
          reportBrowserSession();
          advanceAfterEnd();
        }}
      />
      {children}
    </PlayerContext.Provider>
  );
}
