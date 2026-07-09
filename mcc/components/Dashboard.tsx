"use client";

// The browser build, NOT the default "mqtt" (Node) build: the Node build can't
// serialize packets in the browser under Turbopack, so its CONNECT never sends
// and the client dies with "connack timeout". See lib/mqtt.browser.d.ts.
import mqtt from "mqtt/dist/mqtt.esm";
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
import {
  NARRATION_TOPIC,
  NARRATOR_STATUS_WILDCARD,
  NarrationLine,
  busUrl,
  parseLine,
  pickVoice,
  statusTopicId,
} from "@/lib/bus";
import {
  DayCensus,
  DayHours,
  History,
  censusPeak,
  dayLabel,
  dayTotal,
  fetchDayHours,
  fetchHistory,
  hoursPeak,
  runsNewestFirst,
  speciesInWindow,
  stackDay,
} from "@/lib/history";

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

          <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2">
            <FieldJournal />
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

      {/* --- Station Records: the history shelf (epic #1, Phase 4) ---------- */}
      <StationRecords />

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

/** Speak one narration line via the browser's TTS, matching the persona's
 * voice hint against installed voices (default voice when nothing matches). */
function speakLine(line: NarrationLine) {
  const synth = window.speechSynthesis;
  if (!synth) return;
  const u = new SpeechSynthesisUtterance(line.text);
  const voice = pickVoice(synth.getVoices(), line.voice);
  if (voice) u.voice = voice;
  synth.speak(u);
}

// A journal entry with a stable client-side key: narration lines have no id on
// the bus, and ts alone can collide when two lines land in the same second.
type JournalEntry = NarrationLine & { key: number };

const JOURNAL_LIMIT = 50;

function FieldJournal() {
  const [entries, setEntries] = useState<JournalEntry[]>([]);
  const [presence, setPresence] = useState<Record<string, string>>({});
  const [busUp, setBusUp] = useState(false);
  const [busError, setBusError] = useState<string | null>(null);
  // TTS defaults muted: browsers won't allow audio before a user gesture
  // anyway, and a surprise voice at 6am is a bad first impression. The ref
  // mirrors the state so the (stable) mqtt message handler reads it live.
  const [speaking, setSpeaking] = useState(false);
  const speakingRef = useRef(false);
  const nextKey = useRef(0);

  useEffect(() => {
    // Straight to the broker over WebSockets -- the /daemon rewrite can't
    // carry them. Same host the page came from, so the phone-on-LAN case
    // works without config; mqtt.js reconnects on its own.
    const url = busUrl(
      window.location.hostname,
      process.env.NEXT_PUBLIC_MERLE_MQTT_WS,
    );
    const client = mqtt.connect(url, { reconnectPeriod: 3000 });
    client.on("connect", () => {
      console.debug("[bus] connect", url);
      setBusUp(true);
      setBusError(null);
      client.subscribe([NARRATION_TOPIC, NARRATOR_STATUS_WILDCARD]);
    });
    client.on("close", () => {
      console.debug("[bus] close");
      setBusUp(false);
    });
    client.on("reconnect", () => console.debug("[bus] reconnect ->", url));
    client.on("offline", () => console.debug("[bus] offline"));
    // mqtt.js is an EventEmitter: an unhandled "error" throws in the browser and
    // can wedge the client's own reconnect loop (connection opens but never
    // completes the MQTT handshake, retrying forever). Handle it so reconnect
    // stays healthy, and surface the reason instead of failing silently.
    client.on("error", (err) => {
      console.debug("[bus] error", err?.message ?? err);
      setBusError(err?.message ?? String(err));
    });
    client.on("message", (topic, payload) => {
      const narratorId = statusTopicId(topic);
      if (narratorId) {
        // Retained per-narrator status: "online", "offline", or whatever a
        // future narrator reports ("coffee break") -- shown verbatim.
        setPresence((p) => ({ ...p, [narratorId]: payload.toString() }));
        return;
      }
      const line = parseLine(payload.toString());
      if (!line) return;
      setEntries((prev) =>
        [{ ...line, key: nextKey.current++ }, ...prev].slice(0, JOURNAL_LIMIT),
      );
      if (speakingRef.current) speakLine(line);
    });
    return () => {
      client.end(true);
      window.speechSynthesis?.cancel();
    };
  }, []);

  const toggleSpeaking = useCallback(() => {
    setSpeaking((on) => {
      const next = !on;
      speakingRef.current = next;
      if (!next) window.speechSynthesis?.cancel(); // muting cuts mid-sentence
      return next;
    });
  }, []);

  const narrators = Object.entries(presence).sort(([a], [b]) =>
    a.localeCompare(b),
  );
  const anyoneOn = narrators.some(([, status]) => status === "online");

  return (
    <section className="panel flex flex-col rounded-sm border border-line bg-panel">
      <PanelLabel
        title="Field Journal"
        right={
          <span className="flex items-center gap-3">
            {!busUp ? (
              <span className="stamp text-xs text-inkfaint">bus quiet</span>
            ) : narrators.length === 0 ? (
              <span className="stamp text-xs text-inkfaint">
                no narrator hired
              </span>
            ) : (
              narrators.map(([id, status]) => (
                <span key={id} className="flex items-center gap-1.5 text-xs">
                  {status === "online" ? (
                    <>
                      <span className="lamp inline-block h-2 w-2 rounded-full bg-led text-led" />
                      <span className="stamp text-led">{id} · on the air</span>
                    </>
                  ) : status === "offline" ? (
                    <>
                      <span className="inline-block h-2 w-2 rounded-full bg-inkfaint" />
                      <span className="stamp text-inkfaint">
                        {id} · off the air
                      </span>
                    </>
                  ) : (
                    <>
                      <span className="breathe inline-block h-2 w-2 rounded-full bg-turkey" />
                      <span className="stamp text-turkey">
                        {id} · {status}
                      </span>
                    </>
                  )}
                </span>
              ))
            )}
            <button
              type="button"
              onClick={toggleSpeaking}
              aria-pressed={speaking}
              aria-label={speaking ? "Mute narration" : "Speak narration aloud"}
              title={speaking ? "mute narration" : "speak narration aloud"}
              className={`rounded-sm border p-1.5 transition-colors ${
                speaking
                  ? "border-squirrel text-squirrel"
                  : "border-linebright text-inkdim hover:border-squirrel hover:text-squirrel"
              }`}
            >
              <SpeakerIcon muted={!speaking} />
            </button>
          </span>
        }
      />
      <div className="min-h-[110px] flex-1 px-4 pb-4">
        {entries.length > 0 ? (
          <ul className="flex max-h-72 flex-col gap-3 overflow-y-auto pr-1">
            {entries.map((e, i) => (
              <li
                key={e.key}
                className={`journal-in border-l-2 pl-3 ${
                  i === 0 ? "border-led" : "border-line"
                }`}
              >
                <div className="flex gap-2 text-[11px]">
                  <span className="text-inkfaint">{eventClock(e.ts)}</span>
                  <span className="stamp text-inkdim">{e.narrator}</span>
                </div>
                <p
                  className="mt-0.5 text-[15px] leading-snug text-ink"
                  style={{ fontFamily: "var(--font-display)" }}
                >
                  {e.text}
                </p>
              </li>
            ))}
          </ul>
        ) : (
          <p className="py-2 text-sm leading-relaxed text-inkfaint">
            {!busUp ? (
              <>
                the event bus isn&apos;t reachable. the broker lives on pearl —
                check it there:
                <code className="mt-1 block w-fit rounded-sm bg-panel2 px-2 py-1 text-xs text-inkdim">
                  ssh pearl systemctl status mosquitto
                </code>
                {busError && (
                  <span className="mt-2 block text-xs text-chipmunk">
                    last error: {busError}
                  </span>
                )}
              </>
            ) : anyoneOn ? (
              "nothing filed yet — the driveway is between stories"
            ) : (
              <>
                the bus is up but nobody&apos;s reporting. put Marlin on the
                air (he lives on pearl):
                <code className="mt-1 block w-fit rounded-sm bg-panel2 px-2 py-1 text-xs text-inkdim">
                  python narrator.py --persona personas/marlin.yaml
                </code>
              </>
            )}
          </p>
        )}
      </div>
    </section>
  );
}

function SpeakerIcon({ muted }: { muted: boolean }) {
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
      <path d="M11 5 6.5 8.5H3v7h3.5L11 19z" />
      {muted ? (
        <path d="m15.5 9.5 5 5m0-5-5 5" />
      ) : (
        <>
          <path d="M14.5 9.5a4 4 0 0 1 0 5" />
          <path d="M17 7a7.5 7.5 0 0 1 0 10" />
        </>
      )}
    </svg>
  );
}

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

/* --- Station Records (Phase 4): census chart, harvest trend, run lineage --- */

const HISTORY_DAYS = 14;
const HISTORY_REFRESH_MS = 60_000; // history moves by the day, not the second

function StationRecords() {
  const [hist, setHist] = useState<History | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [dayHours, setDayHours] = useState<DayHours | null>(null);

  const load = useCallback(async () => {
    try {
      const h = await fetchHistory(HISTORY_DAYS);
      setHist(h);
      setUnavailable(false);
      // First load lands on the newest day, i.e. "what visited today/yesterday".
      setSelected((cur) => cur ?? h.census[h.census.length - 1]?.date ?? null);
    } catch {
      setUnavailable(true);
    }
  }, []);

  useEffect(() => {
    // First fetch deferred a microtask: satisfies set-state-in-effect (no
    // synchronous setState path from the effect body) at zero visible cost.
    queueMicrotask(load);
    const id = setInterval(load, HISTORY_REFRESH_MS);
    return () => clearInterval(id);
  }, [load]);

  useEffect(() => {
    if (!selected) return;
    let stale = false;
    fetchDayHours(selected)
      .then((d) => {
        if (!stale) setDayHours(d);
      })
      .catch(() => setDayHours(null));
    return () => {
      stale = true;
    };
  }, [selected]);

  return (
    <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_360px]">
      <FieldCensus
        census={hist?.census ?? null}
        unavailable={unavailable}
        selected={selected}
        onSelect={setSelected}
        dayHours={dayHours}
      />
      <div className="flex min-w-0 flex-col gap-4">
        <HardFrameHarvest days={hist?.hard_frames ?? null} unavailable={unavailable} />
        <TrainingRounds runs={hist?.training_runs ?? null} unavailable={unavailable} />
      </div>
    </div>
  );
}

function FieldCensus({
  census,
  unavailable,
  selected,
  onSelect,
  dayHours,
}: {
  census: DayCensus[] | null;
  unavailable: boolean;
  selected: string | null;
  onSelect: (d: string) => void;
  dayHours: DayHours | null;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const species = census ? speciesInWindow(census) : [];
  const peak = census ? censusPeak(census) : 1;
  const selectedIdx = census?.findIndex((d) => d.date === selected) ?? -1;
  const peakIdx =
    census && census.length
      ? census.reduce(
          (best, d, i) => (dayTotal(d.counts) > dayTotal(census[best].counts) ? i : best),
          0,
        )
      : -1;

  return (
    <section className="panel rounded-sm border border-line bg-panel">
      <PanelLabel
        title="Field Census"
        right={<Sub>distinct visitors / day · last {HISTORY_DAYS} days</Sub>}
      />
      <div className="px-4 pb-4">
        {census && census.length > 0 ? (
          <>
            {/* Legend: identity is never color-alone -- named chips, and the
                selected-day breakdown below doubles as the table view. */}
            <div className="mb-3 flex flex-wrap gap-x-4 gap-y-1">
              {species.map((s) => (
                <span key={s} className="flex items-center gap-1.5 text-[11px] text-inkdim">
                  <span
                    className="inline-block h-2.5 w-2.5 border-2"
                    style={{ borderColor: speciesColor(s) }}
                  />
                  {s}
                </span>
              ))}
              {species.length === 0 && (
                <span className="text-[11px] text-inkfaint">
                  nothing on record in this window
                </span>
              )}
            </div>

            <div className="flex items-end gap-1.5">
              {census.map((d, i) => {
                const segs = stackDay(d.counts);
                const total = dayTotal(d.counts);
                const isSel = i === selectedIdx;
                return (
                  <div key={d.date} className="relative min-w-0 flex-1">
                    {hover === i && (
                      <div className="pointer-events-none absolute bottom-full left-1/2 z-10 mb-1 -translate-x-1/2 whitespace-nowrap rounded-sm border border-linebright bg-panel2 px-2 py-1 text-[11px]">
                        <span className="stamp text-inkfaint">{dayLabel(d.date)}</span>
                        {segs.length ? (
                          segs.map((s) => (
                            <span key={s.species} className="ml-2 text-ink">
                              {s.n} {s.species}
                            </span>
                          ))
                        ) : (
                          <span className="ml-2 text-inkfaint">all quiet</span>
                        )}
                      </div>
                    )}
                    <button
                      type="button"
                      onClick={() => onSelect(d.date)}
                      onMouseEnter={() => setHover(i)}
                      onMouseLeave={() => setHover(null)}
                      aria-label={`${dayLabel(d.date)}: ${total} visitors`}
                      className={`flex h-36 w-full flex-col justify-end rounded-sm px-[2px] pt-1 transition-colors ${
                        isSel ? "bg-panel2" : "hover:bg-panel2/60"
                      }`}
                    >
                      {/* Selective direct labels: only the peak + selected day. */}
                      {(i === peakIdx || isSel) && total > 0 && (
                        <span className="mb-0.5 text-center text-[10px] tabular-nums text-inkdim">
                          {total}
                        </span>
                      )}
                      {/* Top-to-bottom render = reverse stack order, so the
                          baseline species sits on the baseline. 2px gaps keep
                          segments CVD-separable. Rounded cap on the data end. */}
                      {[...segs].reverse().map((s, j) => (
                        <span
                          key={s.species}
                          className={j === 0 ? "rounded-t-sm" : ""}
                          style={{
                            height: `${(s.n / peak) * 100}%`,
                            minHeight: 3,
                            backgroundColor: speciesColor(s.species),
                            marginTop: j > 0 ? 2 : 0,
                          }}
                        />
                      ))}
                    </button>
                    <div
                      className={`mt-1 truncate text-center text-[10px] ${
                        isSel ? "stamp text-ink" : "text-inkfaint"
                      }`}
                    >
                      {dayLabel(d.date)}
                    </div>
                  </div>
                );
              })}
            </div>
            <div className="border-t border-line" />

            {selected && (
              <DayDetail
                census={census}
                selectedIdx={selectedIdx}
                onSelect={onSelect}
                dayHours={dayHours}
              />
            )}
          </>
        ) : (
          <p className="py-2 text-sm text-inkfaint">
            {unavailable
              ? "records unavailable — the daemon is asleep"
              : "reading the ledger…"}
          </p>
        )}
      </div>
    </section>
  );
}

function DayDetail({
  census,
  selectedIdx,
  onSelect,
  dayHours,
}: {
  census: DayCensus[];
  selectedIdx: number;
  onSelect: (d: string) => void;
  dayHours: DayHours | null;
}) {
  if (selectedIdx < 0) return null;
  const day = census[selectedIdx];
  const segs = stackDay(day.counts);
  const hours = dayHours?.date === day.date ? dayHours.hours : null;
  const hPeak = hours ? hoursPeak(hours) : 1;

  return (
    <div className="mt-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Button small disabled={selectedIdx <= 0} onClick={() => onSelect(census[selectedIdx - 1].date)}>
            ‹
          </Button>
          <span className="stamp w-16 text-center text-xs text-ink">
            {dayLabel(day.date)}
          </span>
          <Button
            small
            disabled={selectedIdx >= census.length - 1}
            onClick={() => onSelect(census[selectedIdx + 1].date)}
          >
            ›
          </Button>
        </div>
        {/* The readable record -- also the no-color path to the same facts. */}
        <div className="flex flex-wrap items-center justify-end gap-x-3 gap-y-1 text-[13px]">
          {segs.length ? (
            segs.map((s) => (
              <span key={s.species} className="flex items-center gap-1.5">
                <span
                  className="inline-block h-2.5 w-2.5 border-2"
                  style={{ borderColor: speciesColor(s.species) }}
                />
                <span className="tabular-nums text-ink">
                  {s.n} {s.species}
                </span>
              </span>
            ))
          ) : (
            <span className="text-inkfaint">no visitors logged</span>
          )}
        </div>
      </div>

      {/* Hourly strip: when the day's traffic happened. */}
      <div className="mt-2 flex items-end gap-px" aria-label="arrivals by hour">
        {Array.from({ length: 24 }, (_, h) => {
          const counts = hours?.[String(h)] ?? {};
          const hSegs = stackDay(counts);
          const total = dayTotal(counts);
          return (
            <div
              key={h}
              title={
                total
                  ? `${h}:00 — ${hSegs.map((s) => `${s.n} ${s.species}`).join(", ")}`
                  : `${h}:00 — quiet`
              }
              className="flex h-9 flex-1 flex-col justify-end rounded-[1px] bg-panel2/60"
            >
              {[...hSegs].reverse().map((s) => (
                <span
                  key={s.species}
                  style={{
                    height: `${(s.n / hPeak) * 100}%`,
                    minHeight: total ? 2 : 0,
                    backgroundColor: speciesColor(s.species),
                  }}
                />
              ))}
            </div>
          );
        })}
      </div>
      <div className="mt-0.5 flex justify-between text-[9px] text-inkfaint">
        <span>0h</span>
        <span>6h</span>
        <span>12h</span>
        <span>18h</span>
        <span>23h</span>
      </div>
    </div>
  );
}

function HardFrameHarvest({
  days,
  unavailable,
}: {
  days: { date: string; n: number }[] | null;
  unavailable: boolean;
}) {
  const total = days?.reduce((a, d) => a + d.n, 0) ?? 0;
  const peak = Math.max(1, ...(days ?? []).map((d) => d.n));
  return (
    <section className="panel rounded-sm border border-line bg-panel">
      <PanelLabel title="Hard-Frame Harvest" right={<Sub>training fuel</Sub>} />
      <div className="px-4 pb-4">
        {days ? (
          <>
            <div className="flex items-baseline gap-2">
              <span className="text-3xl font-bold tabular-nums text-led">{total}</span>
              <span className="text-xs text-inkfaint">
                flicker-band frames banked · {HISTORY_DAYS} days
              </span>
            </div>
            <div className="mt-2 flex items-end gap-1">
              {days.map((d) => (
                <div
                  key={d.date}
                  title={`${dayLabel(d.date)} — ${d.n} frame${d.n === 1 ? "" : "s"}`}
                  className="flex h-10 flex-1 flex-col justify-end"
                >
                  <span
                    className="rounded-t-sm bg-led/80"
                    style={{ height: `${(d.n / peak) * 100}%`, minHeight: d.n ? 3 : 1 }}
                  />
                </div>
              ))}
            </div>
            <p className="mt-2 border-t border-line pt-2 text-[11px] text-inkfaint">
              frames the model found hard — review-and-nudge fodder for the next round
            </p>
          </>
        ) : (
          <p className="py-2 text-sm text-inkfaint">
            {unavailable ? "—" : "counting the harvest…"}
          </p>
        )}
      </div>
    </section>
  );
}

function TrainingRounds({
  runs,
  unavailable,
}: {
  runs: History["training_runs"] | null;
  unavailable: boolean;
}) {
  return (
    <section className="panel rounded-sm border border-line bg-panel">
      <PanelLabel title="Training Rounds" right={<Sub>model lineage</Sub>} />
      <div className="px-4 pb-4">
        {runs && runs.length > 0 ? (
          <table className="w-full text-[12px]">
            <thead>
              <tr className="stamp text-left text-[10px] text-inkfaint">
                <th className="pb-1 font-normal">round</th>
                <th className="pb-1 text-right font-normal">mAP50</th>
                <th className="pb-1 text-right font-normal">recall</th>
                <th className="pb-1 text-right font-normal">split</th>
              </tr>
            </thead>
            <tbody>
              {runsNewestFirst(runs).map((r) => (
                <tr
                  key={r.run_name}
                  title={r.notes ?? undefined}
                  className="border-t border-line text-ink"
                >
                  <td className="py-1.5">{r.run_name}</td>
                  <td className="py-1.5 text-right tabular-nums">
                    {r.map50?.toFixed(3) ?? "—"}
                  </td>
                  <td className="py-1.5 text-right tabular-nums">
                    {r.recall?.toFixed(3) ?? "—"}
                  </td>
                  <td className="py-1.5 text-right text-inkdim">{r.val_split ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="py-2 text-sm text-inkfaint">
            {unavailable ? "—" : "no rounds on file"}
          </p>
        )}
        {runs && runs.length > 0 && (
          <p className="mt-1 border-t border-line pt-2 text-[11px] text-inkfaint">
            splits differ across the 2-class move — compare within a split, not across
          </p>
        )}
      </div>
    </section>
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
