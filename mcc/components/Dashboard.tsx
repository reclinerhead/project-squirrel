"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  DaemonState,
  STREAM_URL,
  SNAPSHOT_URL,
  eventClock,
  eventLine,
  fetchState,
  sendControl,
  sortedCounts,
} from "@/lib/daemon";

// Species chip colors = the actual box colors drawn on the stream, so the
// panel and the video read as one instrument. Anything unknown gets bone.
const SPECIES_COLOR: Record<string, string> = {
  squirrel: "var(--squirrel)",
  chipmunk: "var(--chipmunk)",
  turkey: "var(--turkey)",
};
const speciesColor = (name: string) => SPECIES_COLOR[name] ?? "var(--ink)";

const POLL_MS = 1000;

// Shared look for the snapshot / record pair so they sit as a matched set.
const CTRL_BTN =
  "inline-flex flex-1 items-center justify-center gap-2 rounded-sm border px-3 py-2 text-center text-sm transition-colors";

export default function Dashboard() {
  const [state, setState] = useState<DaemonState | null>(null);
  const [asleep, setAsleep] = useState(false);
  const [streamKey, setStreamKey] = useState(0); // bump to force <img> reconnect
  const wasAsleep = useRef(false);
  // Latest threshold, readable synchronously: rapid +/- clicks would otherwise
  // all read the same stale render state and send the same value three times
  // (found by actually clicking it three times fast).
  const thresholdRef = useRef<number | null>(null);

  const poll = useCallback(async () => {
    try {
      const s = await fetchState();
      thresholdRef.current = s.crowd_threshold;
      setState(s);
      setAsleep(false);
      if (wasAsleep.current) {
        wasAsleep.current = false;
        setStreamKey((k) => k + 1); // daemon came back: reconnect the stream
      }
    } catch {
      setAsleep(true);
      wasAsleep.current = true;
    }
  }, []);

  useEffect(() => {
    poll();
    const id = setInterval(poll, POLL_MS);
    return () => clearInterval(id);
  }, [poll]);

  // Mobile browsers kill the long-lived MJPEG socket when the app is
  // backgrounded; an <img> never retries on its own, so returning to the tab
  // showed a broken image under a happy LIVE badge (the /state poll, being
  // fresh requests, recovers by itself). Reconnect when the app comes back.
  //
  // Two hard-won iOS details:
  // - Listen to pageshow and focus as well as visibilitychange (iOS Chrome is
  //   inconsistent about which fires on app-switch return), but only reconnect
  //   if we actually went away first -- otherwise desktop would flash-reconnect
  //   the stream on every window focus.
  // - The reconnect must change the URL (see ?v= in VideoFeed): WebKit
  //   coalesces image loads by URL, so remounting with the same src can reuse
  //   the dead connection instead of opening a new one.
  const wentAway = useRef(false);
  useEffect(() => {
    const goneAway = () => {
      wentAway.current = true;
    };
    const cameBack = () => {
      if (document.visibilityState !== "visible") return;
      if (!wentAway.current) return;
      wentAway.current = false;
      setStreamKey((k) => k + 1);
    };
    const onVis = () =>
      document.visibilityState === "hidden" ? goneAway() : cameBack();
    document.addEventListener("visibilitychange", onVis);
    window.addEventListener("pagehide", goneAway);
    window.addEventListener("pageshow", cameBack);
    window.addEventListener("focus", cameBack);
    return () => {
      document.removeEventListener("visibilitychange", onVis);
      window.removeEventListener("pagehide", goneAway);
      window.removeEventListener("pageshow", cameBack);
      window.removeEventListener("focus", cameBack);
    };
  }, []);

  // Belt-and-suspenders: if the <img> itself errors (dead socket without a
  // visibility change), retry after a beat. Ref-guarded so a persistently-down
  // stream schedules one retry at a time, not an avalanche.
  const streamRetryPending = useRef(false);
  const onStreamError = useCallback(() => {
    if (streamRetryPending.current) return;
    streamRetryPending.current = true;
    setTimeout(() => {
      streamRetryPending.current = false;
      setStreamKey((k) => k + 1);
    }, 2000);
  }, []);

  const control = useCallback(
    async (action: string, value?: number) => {
      try {
        await sendControl(action, value);
        await poll(); // reflect the change immediately
      } catch {
        /* next poll will surface daemon state either way */
      }
    },
    [poll],
  );

  const stepThreshold = useCallback(
    (delta: number) => {
      const current = thresholdRef.current;
      if (current === null) return;
      const next = Math.max(1, current + delta);
      thresholdRef.current = next; // advance immediately so fast clicks compound
      control("set_crowd_threshold", next);
    },
    [control],
  );

  // Stand-down: daemon reachable but the perception engine is idle.
  const paused = !asleep && state !== null && !state.running;
  // Reconnecting: engine running but the source isn't delivering frames (camera
  // dropped/restarted). The feed is frozen on its last frame, not live.
  const reconnecting =
    !asleep && state !== null && state.running && !state.live.signal;

  return (
    <div className="mx-auto w-full max-w-[1500px] px-4 pb-10 sm:px-6">
      <Header state={state} asleep={asleep} />

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_360px]">
        {/* --- The watch: live feed --------------------------------------- */}
        <main className="min-w-0">
          <section className="panel overflow-hidden rounded-sm border border-line bg-panel">
            <PanelLabel
              title="Live Watch"
              right={
                asleep ? (
                  <span className="stamp text-xs text-inkfaint">no signal</span>
                ) : paused ? (
                  <span className="flex items-center gap-2 text-xs text-inkdim">
                    <PauseIcon className="h-3 w-3" />
                    <span className="stamp">standing down</span>
                  </span>
                ) : reconnecting ? (
                  <span className="flex items-center gap-2 text-xs text-turkey">
                    <span className="lamp inline-block h-2 w-2 rounded-full bg-turkey text-turkey" />
                    <span className="stamp">reconnecting</span>
                  </span>
                ) : (
                  <span className="flex items-center gap-2 text-xs text-squirrel">
                    <span className="lamp inline-block h-2 w-2 rounded-full bg-squirrel text-squirrel" />
                    <span className="stamp">live</span>
                    {state && (
                      <span className="text-inkdim">
                        {state.live.fps.toFixed(0)} fps
                      </span>
                    )}
                    {state?.recording && (
                      <span className="flex items-center gap-1 text-chipmunk">
                        <span className="lamp inline-block h-2 w-2 rounded-full bg-chipmunk text-chipmunk" />
                        <span className="stamp">rec</span>
                      </span>
                    )}
                  </span>
                )
              }
            />
            {asleep ? (
              <Asleep />
            ) : (
              <VideoFeed
                paused={paused}
                reconnecting={reconnecting}
                streamKey={streamKey}
                onStreamError={onStreamError}
              />
            )}
          </section>

          {/* Placeholder row: honest about what's not built yet. */}
          <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2">
            <ComingSoon title="Field Journal" note="the narrator files reports here" />
            <ComingSoon title="Weather Post" note="conditions at the seed pile" />
          </div>
        </main>

        {/* --- The rail: instruments -------------------------------------- */}
        <aside className="flex min-w-0 flex-col gap-4">
          <section className="panel rounded-sm border border-line bg-panel">
            <PanelLabel title="On the Pavement" right={<Sub>right now</Sub>} />
            <div className="px-4 pb-4">
              {state && sortedCounts(state.live.counts).length > 0 ? (
                <ul className="flex flex-col gap-2">
                  {sortedCounts(state.live.counts).map(([name, n]) => (
                    <SpeciesRow key={name} name={name} n={n} />
                  ))}
                </ul>
              ) : (
                <p className="py-2 text-sm text-inkfaint">
                  {asleep ? "—" : "all quiet out there…"}
                </p>
              )}
            </div>
          </section>

          <section className="panel rounded-sm border border-line bg-panel">
            <PanelLabel title="Run Census" right={<Sub>distinct visitors</Sub>} />
            <div className="px-4 pb-4">
              {state && sortedCounts(state.totals).length > 0 ? (
                <ul className="flex flex-col gap-2">
                  {sortedCounts(state.totals).map(([name, n]) => (
                    <SpeciesRow key={name} name={name} n={n} />
                  ))}
                </ul>
              ) : (
                <p className="py-2 text-sm text-inkfaint">no visitors yet</p>
              )}
              {state && (
                <p className="mt-3 border-t border-line pt-2 text-[11px] text-inkfaint">
                  session {state.session_id} · a lively upper estimate — track
                  fragments count twice
                </p>
              )}
            </div>
          </section>

          <section className="panel rounded-sm border border-line bg-panel">
            <PanelLabel title="Station Controls" />
            <div className="flex flex-col gap-3 px-4 pb-4">
              <Button
                onClick={() => control(state?.running ? "stop" : "start")}
                disabled={!state || asleep}
                // Neutral (not green/"resume") until we actually know the state
                // -- before the first poll lands the button would otherwise look
                // paused even while the feed is live (very visible on mobile).
                tone={!state || asleep ? undefined : state.running ? "dim" : "go"}
              >
                {asleep
                  ? "daemon asleep"
                  : !state
                    ? "connecting…"
                    : state.running
                      ? "◼ stand down"
                      : "▶ resume watch"}
              </Button>
              <div className="flex gap-2">
                {/* A still camera to grab one frame, a movie camera to roll a
                    clip -- the pair reads at a glance. Static download name: a
                    Date.now() here mismatches between SSR and hydration (learned
                    the hard way); the browser de-dupes repeats with (1), (2)… */}
                <a
                  href={SNAPSHOT_URL}
                  download="merle_snapshot.jpg"
                  className={`${CTRL_BTN} border-linebright text-ink hover:border-squirrel hover:text-squirrel ${asleep ? "pointer-events-none opacity-40" : ""}`}
                >
                  <StillCameraIcon />
                  snapshot
                </a>
                <button
                  type="button"
                  onClick={() =>
                    control(state?.recording ? "record_off" : "record_on")
                  }
                  disabled={!state || asleep}
                  className={`${CTRL_BTN} disabled:pointer-events-none disabled:opacity-40 ${
                    state?.recording
                      ? "border-chipmunk bg-chipmunk/10 text-chipmunk"
                      : "border-linebright text-ink hover:border-chipmunk hover:text-chipmunk"
                  }`}
                >
                  {state?.recording ? (
                    <>
                      <span className="lamp inline-block h-2.5 w-2.5 rounded-full bg-chipmunk text-chipmunk" />
                      stop
                    </>
                  ) : (
                    <>
                      <VideoCameraIcon />
                      record
                    </>
                  )}
                </button>
              </div>
              <div className="flex items-center justify-between gap-2 border-t border-line pt-3">
                <span className="stamp text-xs text-inkdim">crowd alert at</span>
                <div className="flex items-center gap-2">
                  <Button
                    small
                    disabled={!state || asleep || state.crowd_threshold <= 1}
                    onClick={() => stepThreshold(-1)}
                  >
                    −
                  </Button>
                  <span className="w-8 text-center font-bold text-squirrel">
                    {state?.crowd_threshold ?? "–"}
                  </span>
                  <Button
                    small
                    disabled={!state || asleep}
                    onClick={() => stepThreshold(+1)}
                  >
                    +
                  </Button>
                </div>
              </div>
            </div>
          </section>

          <section className="panel rounded-sm border border-line bg-panel">
            <PanelLabel title="Recent Events" right={<Sub>field log</Sub>} />
            <div className="px-4 pb-4">
              {state && state.recent_events.length > 0 ? (
                <ul className="flex flex-col gap-1.5">
                  {state.recent_events.map((e, i) => (
                    <li key={`${e.ts}-${i}`} className="flex gap-2 text-[13px]">
                      <span className="shrink-0 text-inkfaint">
                        {eventClock(e.ts)}
                      </span>
                      <span className="text-inkdim">▸</span>
                      <span className="min-w-0 text-ink">{eventLine(e)}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="py-2 text-sm text-inkfaint">
                  nothing logged yet — the driveway keeps its secrets
                </p>
              )}
            </div>
          </section>
        </aside>
      </div>

      <footer className="mt-8 flex items-baseline justify-between gap-4 border-t border-line pt-3 text-[11px] text-inkfaint">
        <span>
          MERLE · a learning project watching one driveway&apos;s wildlife, and
          nothing else
        </span>
        <span className="stamp shrink-0">est. 2026</span>
      </footer>
    </div>
  );
}

/* --- pieces ---------------------------------------------------------------- */

function Header({
  state,
  asleep,
}: {
  state: DaemonState | null;
  asleep: boolean;
}) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-2 py-6">
      <div>
        <div className="stamp text-[11px] text-inkfaint">
          field station · driveway sector 01
        </div>
        <h1
          className="text-4xl font-semibold text-ink sm:text-5xl"
          style={{ fontFamily: "var(--font-display)" }}
        >
          Merle <span className="text-squirrel">Control Center</span>
        </h1>
      </div>
      <div className="flex items-center gap-2 pb-1.5">
        <span
          className={`inline-block h-2.5 w-2.5 rounded-full ${
            asleep ? "breathe bg-inkfaint" : "lamp bg-led text-led"
          }`}
        />
        <span className="stamp text-xs text-inkdim">
          {asleep ? "daemon asleep" : state ? "daemon online" : "reaching out…"}
        </span>
      </div>
    </header>
  );
}

function PanelLabel({
  title,
  right,
}: {
  title: string;
  right?: React.ReactNode;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3 px-4 pb-2 pt-3">
      <h2
        className="text-lg text-ink"
        style={{ fontFamily: "var(--font-display)" }}
      >
        {title}
      </h2>
      {right}
    </div>
  );
}

function Sub({ children }: { children: React.ReactNode }) {
  return <span className="stamp text-[10px] text-inkfaint">{children}</span>;
}

function SpeciesRow({ name, n }: { name: string; n: number }) {
  const color = speciesColor(name);
  return (
    <li className="flex items-center justify-between gap-3 rounded-sm bg-panel2 px-3 py-2">
      <span className="flex min-w-0 items-center gap-2.5">
        <span
          className="inline-block h-3 w-3 shrink-0 border-2"
          style={{ borderColor: color }}
          title={`box color on the stream`}
        />
        <span className="truncate text-sm">{name}</span>
      </span>
      <span className="text-xl font-bold tabular-nums" style={{ color }}>
        {n}
      </span>
    </li>
  );
}

function Button({
  children,
  onClick,
  disabled,
  tone,
  small,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  tone?: "go" | "dim";
  small?: boolean;
}) {
  const toneCls =
    tone === "go"
      ? "border-led text-led hover:bg-led/10"
      : tone === "dim"
        ? "border-linebright text-inkdim hover:border-chipmunk hover:text-chipmunk"
        : "border-linebright text-ink hover:border-squirrel hover:text-squirrel";
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`rounded-sm border transition-colors disabled:pointer-events-none disabled:opacity-40 ${toneCls} ${
        small ? "h-7 w-7 text-base leading-none" : "flex-1 px-3 py-2 text-sm"
      }`}
    >
      {children}
    </button>
  );
}

function VideoFeed({
  paused,
  reconnecting,
  streamKey,
  onStreamError,
}: {
  paused: boolean;
  reconnecting: boolean;
  streamKey: number;
  onStreamError: () => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [nativeFull, setNativeFull] = useState(false);
  // iOS Safari can't fullscreen a non-<video> element (requestFullscreen is
  // absent on the container), so where the native API is unavailable we fall
  // back to a CSS overlay that fills the viewport. `full` covers both.
  const [cssFull, setCssFull] = useState(false);
  const full = nativeFull || cssFull;
  const dimmed = paused || reconnecting;

  // Track native fullscreen state (also catches Escape, handled by the browser).
  useEffect(() => {
    const onChange = () =>
      setNativeFull(document.fullscreenElement === containerRef.current);
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);

  // For the CSS-overlay path, wire Escape ourselves (the native path already has
  // it) and lock body scroll while the overlay is up.
  useEffect(() => {
    if (!cssFull) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setCssFull(false);
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [cssFull]);

  const toggleFull = useCallback(() => {
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else if (cssFull) {
      setCssFull(false);
    } else if (containerRef.current?.requestFullscreen) {
      containerRef.current.requestFullscreen().catch(() => setCssFull(true));
    } else {
      setCssFull(true); // iOS Safari and other no-API browsers
    }
  }, [cssFull]);

  return (
    <div
      ref={containerRef}
      onDoubleClick={toggleFull}
      // `relative` and `fixed` are both position values -- apply exactly one, or
      // they conflict and `relative` wins, trapping the CSS-fullscreen overlay
      // at panel size instead of filling the viewport.
      className={`group bg-black ${
        full ? "flex items-center justify-center" : ""
      } ${cssFull ? "fixed inset-0 z-50" : "relative"}`}
    >
      {/* ?v= cache-buster: WebKit (iOS Chrome/Safari) coalesces image loads by
          URL, so a remounted <img> with the SAME src can be handed the dead
          connection back instead of opening a new one -- observed as "black box
          until Chrome is fully killed". A changed URL forces a real new request.
          Deterministic (streamKey starts 0 on server and client), so no
          hydration mismatch. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        key={streamKey}
        src={`${STREAM_URL}?v=${streamKey}`}
        alt="Live annotated driveway feed"
        onError={onStreamError}
        className={`block bg-black object-contain transition-[opacity,filter] duration-500 ${
          full ? "h-full max-h-full w-full" : "aspect-video w-full"
        } ${dimmed ? "opacity-30 grayscale" : ""}`}
      />

      {/* Veil: the stream freezes on its last frame whenever it isn't live --
          stood down (engine idle) or reconnecting (camera dropped). Dim + stamp
          it so a frozen frame is never mistaken for a live one. */}
      {paused && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3">
          <PauseIcon className="h-12 w-12 text-ink/80" />
          <span className="stamp text-sm text-inkdim">watch on stand down</span>
          <span className="text-xs text-inkfaint">
            last frame shown · perception engine idle
          </span>
        </div>
      )}
      {reconnecting && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3">
          <span className="lamp h-10 w-10 rounded-full border-4 border-turkey text-turkey" />
          <span className="stamp text-sm text-turkey">
            reconnecting to camera…
          </span>
          <span className="text-xs text-inkfaint">
            last frame shown · the feed dropped, retrying
          </span>
        </div>
      )}

      {/* Fullscreen toggle, YouTube-style, bottom-right (reachable in fullscreen
          too). Always visible on touch (no hover there); hover-revealed on
          desktop. Escape or double-click also exit. */}
      <button
        type="button"
        onClick={toggleFull}
        aria-label={full ? "Exit full screen" : "Full screen"}
        className="absolute bottom-3 right-3 rounded-sm bg-black/50 p-2 text-ink/80 opacity-100 backdrop-blur-sm transition-opacity hover:text-squirrel focus-visible:opacity-100 sm:opacity-0 sm:group-hover:opacity-100"
      >
        {full ? <CompressIcon /> : <ExpandIcon />}
      </button>
    </div>
  );
}

function ExpandIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M8 3H5a2 2 0 0 0-2 2v3" />
      <path d="M16 3h3a2 2 0 0 1 2 2v3" />
      <path d="M8 21H5a2 2 0 0 1-2-2v-3" />
      <path d="M16 21h3a2 2 0 0 0 2-2v-3" />
    </svg>
  );
}

function CompressIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M8 3v3a2 2 0 0 1-2 2H3" />
      <path d="M21 8h-3a2 2 0 0 1-2-2V3" />
      <path d="M3 16h3a2 2 0 0 1 2 2v3" />
      <path d="M16 21v-3a2 2 0 0 1 2-2h3" />
    </svg>
  );
}

function PauseIcon({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="currentColor"
      className={className}
      aria-hidden="true"
    >
      <rect x="6" y="4" width="4.5" height="16" rx="1" />
      <rect x="13.5" y="4" width="4.5" height="16" rx="1" />
    </svg>
  );
}

// Functional button icons (camera glyphs), themeable via currentColor.
function StillCameraIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="15"
      height="15"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M4 8h3l1.4-2h7.2L17 8h3v11H4z" />
      <circle cx="12" cy="13" r="3.2" />
    </svg>
  );
}

function VideoCameraIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="15"
      height="15"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="3" y="7" width="12" height="10" rx="1.5" />
      <path d="M15 10.5 20.5 8v8L15 13.5z" />
    </svg>
  );
}

function Asleep() {
  return (
    <div className="flex aspect-video w-full flex-col items-center justify-center gap-3 bg-black/40 px-6 text-center">
      <span className="breathe text-4xl" role="img" aria-label="sleeping">
        🐿️💤
      </span>
      <p
        className="text-2xl text-inkdim"
        style={{ fontFamily: "var(--font-display)" }}
      >
        Merle is asleep
      </p>
      <p className="max-w-md text-xs leading-relaxed text-inkfaint">
        the daemon isn&apos;t reachable. wake it from the project root:
        <br />
        <code className="mt-1 inline-block rounded-sm bg-panel2 px-2 py-1 text-inkdim">
          python -m uvicorn merle_daemon:app --port 8000
        </code>
      </p>
    </div>
  );
}

function ComingSoon({ title, note }: { title: string; note: string }) {
  return (
    <section className="panel relative flex min-h-[110px] flex-col justify-between overflow-hidden rounded-sm border border-dashed border-line bg-transparent px-4 py-3">
      <h2
        className="text-lg text-inkdim"
        style={{ fontFamily: "var(--font-display)" }}
      >
        {title}
      </h2>
      <p className="text-xs text-inkfaint">{note}</p>
      <span className="stamp absolute right-3 top-3 rotate-6 rounded-sm border border-inkfaint px-1.5 py-0.5 text-[10px] text-inkfaint">
        coming soon
      </span>
    </section>
  );
}
