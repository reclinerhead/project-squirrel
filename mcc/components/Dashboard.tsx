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
  rosterCounts,
  sendControl,
} from "@/lib/daemon";
import {
  JournalEntry,
  NARRATION_JOURNAL_WILDCARD,
  NARRATION_TOPIC,
  NARRATOR_STATUS_WILDCARD,
  NarrationLine,
  busUrl,
  journalTopicId,
  mergeJournals,
  parseJournal,
  parseLine,
  pickVoice,
  statusTopicId,
  toJournalEntries,
  voiceColor,
} from "@/lib/bus";
import { frameUrl } from "@/lib/frames";
// The journal lightbox (issue #96): full-size stills on thumbnail click.
// Styles are themed to the station in globals.css (.yarl__root overrides).
import Lightbox from "yet-another-react-lightbox";
import Captions from "yet-another-react-lightbox/plugins/captions";
import "yet-another-react-lightbox/styles.css";
import "yet-another-react-lightbox/plugins/captions.css";
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
import {
  ConditionIconKey,
  CurrentWeather,
  DEW_TREND_EPS_F,
  DayTick,
  FUTURE_S,
  HUMIDITY_TREND_EPS_PCT,
  PAST_S,
  REPORT_STALE_S,
  STALE_AFTER_S,
  STATION_FUTURE_S,
  STATION_SPAN_S,
  TEMP_TREND_EPS_F,
  WEATHER_CURRENT_TOPIC,
  WEATHER_FORECAST_TOPIC,
  WEATHER_HISTORY_TOPIC,
  WEATHER_REPORT_TOPIC,
  WEATHER_STATUS_TOPIC,
  Trend,
  WeatherPoint,
  WeatherReport,
  WeatherStatus,
  FORECAST_SHADE_CEIL,
  RAIN_SHADE_FLOOR,
  SNOW_SHADE_FLOOR,
  ageText,
  clampWindow,
  compass,
  conditionIcon,
  dayTicks,
  fetchArchive,
  linePath,
  mergePoints,
  precipFill,
  precipShade,
  tempMarks,
  nearestPoint,
  nightBands,
  parseCurrent,
  parsePoints,
  parseReport,
  parseStatus,
  pressureRange,
  pressureTrend,
  seriesCeil,
  seriesTrend,
  snowSeason,
  tempRange,
  timeTicks,
  trendSeries,
  windCeil,
  windowEdgeLabel,
} from "@/lib/weather";

// Species chip colors = the actual box colors drawn on the stream, so the
// panel and the video read as one instrument. Anything unknown gets bone.
const SPECIES_COLOR: Record<string, string> = {
  squirrel: "var(--squirrel)",
  chipmunk: "var(--chipmunk)",
  turkey: "var(--turkey)",
};
const speciesColor = (name: string) => SPECIES_COLOR[name] ?? "var(--ink)";

const POLL_MS = 1000;
// While the daemon is asleep -- its normal state, since bluejay only runs it
// during test sessions -- check in at a walk, not a sprint (issue #35). Waking
// is still snappy: any control click polls immediately, and 10s is a fine
// worst case for "I just started the daemon, when does the dashboard notice".
const ASLEEP_POLL_MS = 10_000;

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
    const id = setInterval(poll, asleep ? ASLEEP_POLL_MS : POLL_MS);
    return () => clearInterval(id);
  }, [poll, asleep]);

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
                ) : !state ? (
                  // Pre-first-poll: don't advertise LIVE while the daemon is
                  // still an open question (very visible when the first /state
                  // rides out the proxy's headers timeout).
                  <span className="stamp text-xs text-inkfaint">
                    reaching out…
                  </span>
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
            <WeatherPost />
          </div>
        </main>

        {/* --- The rail: instruments -------------------------------------- */}
        <aside className="flex min-w-0 flex-col gap-4">
          <section className="panel rounded-sm border border-line bg-panel">
            <PanelLabel title="On the Pavement" right={<Sub>right now</Sub>} />
            <div className="px-4 pb-4">
              {/* Fixed slots (issue #16): every roster species gets a row, zero
                  or not, so a species blinking in/out lights its gauge instead
                  of inserting a row and shoving the rail around. */}
              {state && rosterCounts(state.species ?? [], state.live.counts).length > 0 ? (
                <ul className="flex flex-col gap-2">
                  {rosterCounts(state.species ?? [], state.live.counts).map(
                    ([name, n]) => (
                      <SpeciesRow key={name} name={name} n={n} />
                    ),
                  )}
                </ul>
              ) : (
                <QuietRow label={asleep ? "—" : "all quiet out there…"} />
              )}
            </div>
          </section>

          <section className="panel rounded-sm border border-line bg-panel">
            <PanelLabel title="Run Census" right={<Sub>distinct visitors</Sub>} />
            <div className="px-4 pb-4">
              {/* Same fixed slots as On the Pavement, in the same order, so the
                  two panels read as one instrument stack. */}
              {state && rosterCounts(state.species ?? [], state.totals).length > 0 ? (
                <ul className="flex flex-col gap-2">
                  {rosterCounts(state.species ?? [], state.totals).map(
                    ([name, n]) => (
                      <SpeciesRow key={name} name={name} n={n} />
                    ),
                  )}
                </ul>
              ) : (
                <QuietRow label="no visitors yet" />
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

// The entry cap, matching the narrator's JOURNAL_LINES window. JournalEntry
// (content-derived stable keys) lives in lib/bus.ts with the parsers.
const JOURNAL_LIMIT = 50;

/** The still shot filed with a journal entry (issue #90): the annotated frame
 * the daemon captured at event time, archived on pearl, served by /frames --
 * so thumbnails survive bluejay's nap the way the journal itself does. The
 * slot is FIXED-SIZE and rendered from first paint (an entry's image presence
 * is known on arrival), so neither the image loading nor the image being
 * pruned can shift the layout: a lost print fills the same frame with a quiet
 * stamp, never a broken-image icon. Component-local error state rides the
 * entry's stable content-derived key, so a window republish keeps it.
 * Since issue #96 the print floats in the entry's text flow (magazine wrap)
 * and opens the full-size frame in the journal lightbox; a faded print is
 * inert -- the archive prunes both sizes together, so there is nothing to
 * open. Both states share one geometry so fading can't shift the layout.
 * It floats LEFT (issue #107): the column of text runs down its right side,
 * the way a magazine sets a picture into a story.
 *
 * Two sizes (issue #102), differing only in geometry and which archived
 * variant they pull. "panel" is the rail's 144x81 thumb. "broadcast" is the
 * reading room's print: ~40% of the column (the float drops on narrow
 * screens -- a 40% float in a phone-width column leaves a two-words-per-line
 * ribbon beside a postage stamp), which since #107's full-width room is
 * ~580px, and it pulls the FULL-SIZE variant, because the archived thumb is
 * only ~320px wide and would land far under its display size -- soft on any
 * screen, let alone HiDPI. Lazy loading means only the prints in view fetch,
 * and those bytes are the ones the lightbox wants next, so opening one from
 * there is warm-cache instant. Both sizes hold a fixed 16:9 box: reserving
 * it is what keeps the broadcast view's pinned scroll honest (an image
 * growing after the pin strands it mid-thread), not just no-layout-shift. */
const FRAME_BOX_BASE =
  "overflow-hidden rounded-sm border border-line bg-panel2";
const FRAME_BOX: Record<"panel" | "broadcast", string> = {
  panel: "float-left mb-1 mr-3 h-[81px] w-36",
  broadcast:
    "mb-3 aspect-video w-full sm:float-left sm:mb-2 sm:mr-5 sm:w-2/5",
};
function FrameThumb({
  frameId,
  narrator,
  onOpen,
  size = "panel",
}: {
  frameId: string;
  narrator: string;
  onOpen: () => void;
  size?: "panel" | "broadcast";
}) {
  const [lost, setLost] = useState(false);
  const box = `${FRAME_BOX[size]} ${FRAME_BOX_BASE}`;
  if (lost) {
    return (
      <span className={`${box} flex items-center justify-center`}>
        <span className="stamp text-[9px] text-inkfaint">faded</span>
      </span>
    );
  }
  return (
    <button
      type="button"
      onClick={onOpen}
      aria-label={`open the still shot filed with ${narrator}'s entry`}
      className={`${box} block cursor-zoom-in transition-colors duration-200 hover:border-linebright focus-visible:border-linebright focus-visible:outline-none`}
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={frameUrl(frameId, size !== "broadcast")}
        alt={`still shot filed with ${narrator}'s entry`}
        loading="lazy"
        className="h-full w-full object-cover"
        onError={() => setLost(true)}
      />
    </button>
  );
}

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
  // The broadcast view (issue #89): the station-view treatment for the
  // narrators' back-and-forth -- same entries state, real reading room.
  const [broadcastView, setBroadcastView] = useState(false);
  // The journal lightbox (issue #96): which entry's still is open, by the
  // entry's stable content-derived key -- NOT an index. Entries prepend and
  // windows republish while the lightbox is up, so an index would drift to a
  // different photo mid-view; the key pins the slide, and the index is
  // re-derived each render. An entry aging out of the window closes it.
  const [lightboxKey, setLightboxKey] = useState<string | null>(null);
  // Per-narrator journal windows (issue #80), keyed by mqtt_id from the topic.
  // A ref, not state: only the merged entries render, and the mqtt handler
  // must read the latest windows synchronously when any one of them arrives.
  const journalsRef = useRef<Record<string, NarrationLine[]>>({});

  useEffect(() => {
    // Straight to the broker over WebSockets -- the /daemon proxy can't
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
      // The retained journal windows (issue #58, per-narrator since #80) are
      // the entries' source of truth -- the broker replays every narrator's
      // window the moment we subscribe the wildcard, so a fresh tab gets the
      // whole show back the way the Weather Post gets its state. The live
      // lines topic stays subscribed for TTS: speaking is a MOMENT (never
      // re-speak a replayed window), exactly the state-vs-moment split the
      // bus already draws.
      client.subscribe([
        NARRATION_JOURNAL_WILDCARD,
        NARRATION_TOPIC,
        NARRATOR_STATUS_WILDCARD,
      ]);
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
      const journalId = journalTopicId(topic);
      if (journalId) {
        // One narrator's retained window arrived (or was republished whole).
        // Replace just that narrator's window and re-merge across the roster
        // by ts (issue #80) -- either narrator restarting or republishing can
        // never blank the other's history.
        const lines = parseJournal(payload.toString());
        if (lines) {
          journalsRef.current = { ...journalsRef.current, [journalId]: lines };
          setEntries(
            toJournalEntries(mergeJournals(journalsRef.current, JOURNAL_LIMIT)),
          );
        }
        return;
      }
      const line = parseLine(payload.toString());
      if (!line) return;
      // A live line lands here milliseconds before the narrator's window
      // republish replaces the whole list; prepending it keeps the panel
      // instant, and it also keeps a pre-#58 narrator (journal topic never
      // published) working through a deploy. Content-derived keys make the
      // two paths agree, so the replace is a no-op re-render, not a remount.
      setEntries((prev) => {
        const [entry] = toJournalEntries([line]);
        if (prev.some((e) => e.key === entry.key)) return prev;
        return [entry, ...prev].slice(0, JOURNAL_LIMIT);
      });
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

  // The lightbox pages the whole window's stills (issue #96), and its slide
  // order follows the READING ORDER of the surface that opened it (#102):
  // the panel runs newest-first, the broadcast view oldest-first, so a fixed
  // order would send the arrows backwards through the column the viewer just
  // clicked in. Only one of the two is ever clickable (the view covers the
  // panel), so `broadcastView` names the live surface. Reordering is free
  // because the open slide is re-derived from its stable content-derived KEY
  // every render, never an index -- so neither this flip nor entries arriving
  // nor a window republish can swap the photo under the viewer.
  const stills = entries.filter(
    (e): e is JournalEntry & { frame_id: string } => Boolean(e.frame_id),
  );
  if (broadcastView) stills.reverse();
  const lightboxIndex =
    lightboxKey === null ? -1 : stills.findIndex((e) => e.key === lightboxKey);

  return (
    <>
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
              <PresenceChips narrators={narrators} />
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
            <button
              type="button"
              onClick={() => setBroadcastView(true)}
              aria-label="Open the broadcast view"
              className="text-inkfaint transition-colors hover:text-squirrel"
            >
              <ExpandIcon />
            </button>
          </span>
        }
      />
      {/* relative + absolute-fill list: the list is out of flow, so it can't
          set the grid row's height -- the tallest sibling panel (Weather Post)
          does, and the journal stretches to match, scrolling internally.
          min-h-72 keeps a working height when the sibling is short (mobile
          single-column), and the empty state reserves the same box. */}
      <div className="relative min-h-72 flex-1">
        {entries.length > 0 ? (
          <ul className="scrollpane absolute inset-0 flex flex-col gap-3 overflow-y-auto px-4 pb-4">
            {entries.map((e, i) => {
              // Voice colors (issue #89 follow-up): the rail and name stamp
              // wear the narrator's stable accent -- hue carries identity,
              // intensity carries recency (newest at full strength, the rest
              // dimmed). Body text stays ink.
              const voice = voiceColor(e.narrator);
              return (
                <li
                  key={e.key}
                  className="journal-filed border-l-2 pl-3"
                  style={{
                    borderLeftColor:
                      i === 0
                        ? voice
                        : `color-mix(in srgb, ${voice} 45%, transparent)`,
                  }}
                >
                  <div className="flex gap-2 text-[11px]">
                    <span className="text-inkfaint">{eventClock(e.ts)}</span>
                    <span className="stamp" style={{ color: voice }}>
                      {e.narrator}
                    </span>
                  </div>
                  {/* Entries with a still shot (issue #90) reserve its slot
                      from first paint; entries without never gain one -- the
                      no-layout-shift rule. The thumb floats ahead of the text
                      (issue #96) so prose wraps around it magazine-style;
                      flow-root contains the float, so a short line still
                      holds the entry open to the print's full height. */}
                  <div className="mt-0.5 flow-root">
                    {e.frame_id && (
                      <FrameThumb
                        frameId={e.frame_id}
                        narrator={e.narrator}
                        onOpen={() => setLightboxKey(e.key)}
                      />
                    )}
                    <p
                      className="text-[15px] leading-snug text-ink"
                      style={{ fontFamily: "var(--font-display)" }}
                    >
                      {e.text}
                    </p>
                  </div>
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="px-4 pb-4 pt-2 text-sm leading-relaxed text-inkfaint">
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
    {/* The journal lightbox (issue #96): the full-size /frames/<id> variant
        that lib/frames.ts always reserved for this view. Finite carousel --
        the window has real ends, and a disabled arrow says so honestly.
        closeOnBackdropClick + Escape are the exits; the slide title wears
        the entry's byline in the stamp treatment (globals.css themes all
        the chrome to the station). */}
    <Lightbox
      open={lightboxIndex >= 0}
      close={() => setLightboxKey(null)}
      index={Math.max(lightboxIndex, 0)}
      slides={stills.map((e) => ({
        src: frameUrl(e.frame_id),
        alt: `still shot filed with ${e.narrator}'s entry`,
        title: `${e.narrator} · ${eventClock(e.ts)}`,
      }))}
      plugins={[Captions]}
      carousel={{ finite: true }}
      controller={{ closeOnBackdropClick: true }}
      on={{
        view: ({ index }) => {
          const key = stills[index]?.key;
          if (key) setLightboxKey(key);
        },
      }}
    />
    {broadcastView && (
      <FieldJournalView
        entries={entries}
        narrators={narrators}
        busUp={busUp}
        anyoneOn={anyoneOn}
        speaking={speaking}
        onToggleSpeaking={toggleSpeaking}
        onClose={() => setBroadcastView(false)}
        onOpenStill={setLightboxKey}
        stillOpen={lightboxIndex >= 0}
      />
    )}
    </>
  );
}

/** Per-narrator presence: a dot + the name (issue #84) -- the pulsing green
 * lamp reads as "live" on its own, so a roster of two fits a masthead
 * without "· on/off the air" suffixes crowding it. A non-standard retained
 * status (a future "coffee break") still shows its text: the name alone
 * can't convey an arbitrary state. Shared by the panel and the broadcast
 * view (issue #89). */
function PresenceChips({ narrators }: { narrators: [string, string][] }) {
  return (
    <>
      {narrators.map(([id, status]) => {
        const online = status === "online";
        const offline = status === "offline";
        return (
          <span key={id} className="flex items-center gap-1.5 text-xs">
            <span
              className={`inline-block h-2 w-2 rounded-full ${
                online
                  ? "lamp bg-led text-led"
                  : offline
                    ? "bg-inkfaint"
                    : "breathe bg-turkey"
              }`}
            />
            <span
              className={`stamp ${
                online
                  ? "text-led"
                  : offline
                    ? "text-inkfaint"
                    : "text-turkey"
              }`}
            >
              {online || offline ? id : `${id} · ${status}`}
            </span>
          </span>
        );
      })}
    </>
  );
}

/** The broadcast view (issue #89): the Field Journal writ large -- the
 * station-view treatment for the narrators' back-and-forth. Live from the
 * same entries state the panel holds (one bus client, no new
 * subscriptions), but read like a conversation: oldest first, pinned to
 * the newest line at the bottom, with mention follow-ups (event_kind
 * "colleague_mention") indented under the report they answer -- threading
 * is data-driven, no narrator hardcoding. More will land on this page
 * later; for now it is the reading room. */
function FieldJournalView({
  entries,
  narrators,
  busUp,
  anyoneOn,
  speaking,
  onToggleSpeaking,
  onClose,
  onOpenStill,
  stillOpen,
}: {
  entries: JournalEntry[];
  narrators: [string, string][];
  busUp: boolean;
  anyoneOn: boolean;
  speaking: boolean;
  onToggleSpeaking: () => void;
  onClose: () => void;
  onOpenStill: (key: string) => void;
  stillOpen: boolean;
}) {
  // The Live Watch CSS-overlay contract (the WeatherStationView mechanics):
  // Escape closes, body scroll locks while the view is up.
  //
  // The guard (issue #102): this listener is on WINDOW, and the lightbox's
  // own Escape bubbles up to it -- so with a still open, one keypress would
  // close the photo AND the reading room underneath it. Escape belongs to
  // the topmost layer, so the view stands down while a still is up; the
  // lightbox closes, this listener re-arms, and a second Escape exits.
  useEffect(() => {
    if (stillOpen) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, stillOpen]);

  // Scroll lock is its own effect, deliberately NOT sharing the Escape
  // guard's deps: the lightbox writes body.overflow too, and if this
  // unlocked/relocked around a still opening, the lightbox's own restore on
  // close would race it and could strand the reading room scrollable.
  useEffect(() => {
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = "";
    };
  }, []);

  // What was already on the wire when the room opened (issue #107). The
  // flash means "a fresh dispatch just landed", so it must never replay for
  // history. The panel gets that free -- its list stays mounted, so only a
  // genuinely new entry mounts and animates -- but this view unmounts on
  // close and remounts on open, re-running EVERY entry's mount animation, so
  // reopening lit the whole thread up as new. These keys render with plain
  // `journal-in` (slide in, stay quiet); only lines arriving while the room
  // is open get the full `journal-filed` flare. Republished windows carry
  // stable content-derived keys, so they don't remount and can't re-flash;
  // reopening re-snapshots, which is what makes read history stay quiet.
  //
  // State with a lazy initializer, not a ref: the snapshot is READ during
  // render to pick each entry's class, and refs are off-limits there (the
  // React Compiler is in this build -- `react-hooks/refs` fails the lint).
  // The initializer runs once at mount, which is exactly "what was on the
  // wire when the room opened"; the setter is deliberately unused.
  const [openedWith] = useState(() => new Set(entries.map((e) => e.key)));

  // Chat order: the panel stays newest-first, but a conversation reads top
  // to bottom -- so the thread reverses and stays pinned to the newest line
  // at the bottom. Scrolling up to reread unpins; returning to the bottom
  // re-pins (checked per scroll, with slack for rounding).
  const thread = [...entries].reverse();
  const paneRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);
  useEffect(() => {
    const pane = paneRef.current;
    if (pane && pinnedRef.current) pane.scrollTop = pane.scrollHeight;
  }, [entries]);
  const onScroll = () => {
    const pane = paneRef.current;
    if (!pane) return;
    pinnedRef.current =
      pane.scrollHeight - pane.scrollTop - pane.clientHeight < 48;
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Field journal broadcast view"
      className="fixed inset-0 z-50 flex flex-col bg-bg"
    >
      {/* Full width (issue #107): max-w-[1500px] is the station view's
          measure, itself the main dashboard's wrapper (#60) -- the app's two
          full-screen overlays now agree on how wide the page is, and the
          room it buys goes to the prints. The masthead stays put; the thread
          scrolls inside. */}
      <div className="mx-auto flex h-full w-full max-w-[1500px] flex-col px-4 sm:px-6">
        <div className="flex items-center justify-between gap-3 border-b border-line pb-3 pt-5">
          <h2
            className="text-2xl text-ink"
            style={{ fontFamily: "var(--font-display)" }}
          >
            Field Journal{" "}
            <span className="stamp ml-2 align-middle text-[10px] text-inkfaint">
              broadcast view
            </span>
          </h2>
          <div className="flex items-center gap-3">
            {!busUp ? (
              <span className="stamp text-xs text-inkfaint">bus quiet</span>
            ) : narrators.length === 0 ? (
              <span className="stamp text-xs text-inkfaint">
                no narrator hired
              </span>
            ) : (
              <PresenceChips narrators={narrators} />
            )}
            <button
              type="button"
              onClick={onToggleSpeaking}
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
            <button
              type="button"
              onClick={onClose}
              autoFocus
              aria-label="Close the broadcast view"
              className="rounded-sm border border-line p-1.5 text-inkdim transition-colors hover:border-linebright hover:text-squirrel"
            >
              <CloseIcon />
            </button>
          </div>
        </div>

        <div
          ref={paneRef}
          onScroll={onScroll}
          className="scrollpane flex-1 overflow-y-auto py-6"
        >
          {thread.length > 0 ? (
            <ol className="flex flex-col gap-6">
              {thread.map((e, i) => {
                const reply = e.event_kind === "colleague_mention";
                const newest = i === thread.length - 1;
                // Same voice treatment as the panel: the narrator's accent
                // on rail + name, full strength on the newest line.
                const voice = voiceColor(e.narrator);
                return (
                  <li
                    key={e.key}
                    // History slides in quietly; only what landed while the
                    // room was open flares (issue #107 -- see openedWith).
                    className={`${
                      openedWith.has(e.key) ? "journal-in" : "journal-filed"
                    } border-l-2 pl-4 ${reply ? "ml-8 sm:ml-16" : ""}`}
                    style={{
                      borderLeftColor: newest
                        ? voice
                        : `color-mix(in srgb, ${voice} 45%, transparent)`,
                    }}
                  >
                    <div className="flex items-baseline gap-2 text-sm">
                      {reply && (
                        <span aria-hidden className="text-inkfaint">
                          ↳
                        </span>
                      )}
                      <span className="stamp" style={{ color: voice }}>
                        {e.narrator}
                      </span>
                      <span className="text-inkfaint">{eventClock(e.ts)}</span>
                      {reply && (
                        <span className="stamp text-[11px] text-inkfaint">
                          follow-up
                        </span>
                      )}
                    </div>
                    {/* The reading room's print (issue #102): the panel's
                        magazine wrap writ large -- flow-root contains the
                        float, so a short line still holds the print's full
                        height. Reserving the box is also what keeps the
                        pinned scroll above honest. */}
                    <div className="mt-1 flow-root">
                      {e.frame_id && (
                        <FrameThumb
                          frameId={e.frame_id}
                          narrator={e.narrator}
                          size="broadcast"
                          onOpen={() => onOpenStill(e.key)}
                        />
                      )}
                      <p
                        className="text-xl leading-relaxed text-ink"
                        style={{ fontFamily: "var(--font-display)" }}
                      >
                        {e.text}
                      </p>
                    </div>
                  </li>
                );
              })}
            </ol>
          ) : (
            <p className="text-sm leading-relaxed text-inkfaint">
              {!busUp
                ? "the event bus isn't reachable — the broker lives on pearl"
                : anyoneOn
                  ? "nothing filed yet — the driveway is between stories"
                  : "the bus is up but nobody's reporting"}
            </p>
          )}
        </div>
      </div>
    </div>
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

// Empty-state row with the same box metrics as SpeciesRow (the invisible
// text-xl spacer locks the line height), so a panel flipping between
// "quiet" and one species never changes height and shifts the layout.
function QuietRow({ label }: { label: string }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-sm px-3 py-2">
      <span className="text-sm text-inkfaint">{label}</span>
      <span aria-hidden className="invisible text-xl font-bold tabular-nums">
        0
      </span>
    </div>
  );
}

function SpeciesRow({ name, n }: { name: string; n: number }) {
  const color = speciesColor(name);
  // Zero is a real reading, not an error: the row keeps its slot (fixed panel
  // geometry, issue #16) and goes quiet -- dim chip, faint count -- then
  // lights back up when the species returns.
  const quiet = n === 0;
  return (
    <li className="flex items-center justify-between gap-3 rounded-sm bg-panel2 px-3 py-2">
      <span
        className={`flex min-w-0 items-center gap-2.5 transition-opacity duration-500 ${quiet ? "opacity-40" : ""}`}
      >
        <span
          className="inline-block h-3 w-3 shrink-0 border-2"
          style={{ borderColor: color }}
          title={`box color on the stream`}
        />
        <span className="truncate text-sm">{name}</span>
      </span>
      <span
        className={`text-xl font-bold tabular-nums transition-colors duration-500 ${quiet ? "text-inkfaint" : ""}`}
        style={quiet ? undefined : { color }}
      >
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
          python -m uvicorn merle_daemon:app --host 0.0.0.0 --port 8000
          --timeout-graceful-shutdown 3
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

/* --- Condition icons (issue #78): the sky at a glance -----------------------
   Eight hand-drawn glyphs in the house line-work style, sized to ride beside
   the temperature readout. Structure strokes inherit currentColor so the
   stale and off-air states dim them along with the text; the subject of each
   sky (sun disc, rain, lightning) wears its accent only while the report is
   fresh. Clouds fill with the panel color so an overlap occludes what sits
   behind it instead of tangling line-work. */

const CONDITION_LABEL: Record<ConditionIconKey, string> = {
  sunny: "sunny",
  "mostly-sunny": "mostly sunny",
  "partly-cloudy": "partly cloudy",
  cloudy: "cloudy",
  stormy: "stormy",
  raining: "raining",
  snowing: "snowing",
  windy: "super windy",
};

// One cumulus, drawn once; every cloudy sky places it by transform.
const CLOUD_PATH = "M35 38H18a14 14 0 1 1 13.42-18h3.58a9 9 0 1 1 0 18Z";

/** Eight rays around a sun disc -- shared by the three sunny-ish skies. */
function SunRays({
  cx,
  cy,
  from,
  to,
}: {
  cx: number;
  cy: number;
  from: number;
  to: number;
}) {
  return (
    <g>
      {[0, 45, 90, 135, 180, 225, 270, 315].map((a) => (
        <line
          key={a}
          x1={cx}
          y1={cy - to}
          x2={cx}
          y2={cy - from}
          transform={`rotate(${a} ${cx} ${cy})`}
        />
      ))}
    </g>
  );
}

function ConditionGlyph({
  icon,
  size,
  live,
}: {
  icon: ConditionIconKey;
  size: number;
  live: boolean;
}) {
  const sun = live ? "var(--turkey)" : "currentColor";
  // Rain draws in the weather ink (issue #113), not the live-lamp green it
  // used to borrow. Snow keeps currentColor -- it is already the ink the
  // glyph's structure wears, and white IS snow's colour.
  const rain = live ? "var(--rain)" : "currentColor";
  const common = {
    viewBox: "0 0 48 48",
    width: size,
    height: size,
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 2.4,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };
  switch (icon) {
    case "sunny":
      return (
        <svg {...common} stroke={sun}>
          <circle cx="24" cy="24" r="9.5" />
          <SunRays cx={24} cy={24} from={14.5} to={19} />
        </svg>
      );
    case "mostly-sunny":
      return (
        <svg {...common}>
          <g stroke={sun}>
            <circle cx="19" cy="19" r="8" />
            <SunRays cx={19} cy={19} from={11.5} to={15.5} />
          </g>
          <g transform="translate(16 20) scale(0.58)">
            <path d={CLOUD_PATH} fill="var(--panel)" strokeWidth={2.4 / 0.58} />
          </g>
        </svg>
      );
    case "partly-cloudy":
      return (
        <svg {...common}>
          <g stroke={sun}>
            <circle cx="17" cy="15" r="7" />
            <SunRays cx={17} cy={15} from={10} to={13.5} />
          </g>
          <g transform="translate(4.5 12) scale(0.76)">
            <path d={CLOUD_PATH} fill="var(--panel)" strokeWidth={2.4 / 0.76} />
          </g>
        </svg>
      );
    case "cloudy":
      return (
        <svg {...common}>
          <g transform="translate(17.5 3) scale(0.5)" strokeOpacity={0.55}>
            <path d={CLOUD_PATH} strokeWidth={2.4 / 0.5} />
          </g>
          <g transform="translate(0 5) scale(0.92)">
            <path d={CLOUD_PATH} fill="var(--panel)" strokeWidth={2.4 / 0.92} />
          </g>
        </svg>
      );
    case "stormy":
      return (
        <svg {...common}>
          <g transform="translate(3.5 -5) scale(0.85)">
            <path d={CLOUD_PATH} fill="var(--panel)" strokeWidth={2.4 / 0.85} />
          </g>
          <path
            d="M26 25 L18.5 37 H23.5 L21.5 46 L31.5 33.5 H26 L29 25 Z"
            fill={sun}
            stroke="none"
          />
        </svg>
      );
    case "raining":
      return (
        <svg {...common}>
          <g transform="translate(3.5 -3.5) scale(0.85)">
            <path d={CLOUD_PATH} fill="var(--panel)" strokeWidth={2.4 / 0.85} />
          </g>
          <g stroke={rain}>
            <line x1="15.5" y1="33" x2="13" y2="41" />
            <line x1="23.5" y1="35" x2="21" y2="43" />
            <line x1="31.5" y1="33" x2="29" y2="41" />
          </g>
        </svg>
      );
    case "snowing":
      return (
        <svg {...common}>
          <g transform="translate(3.5 -3.5) scale(0.85)">
            <path d={CLOUD_PATH} fill="var(--panel)" strokeWidth={2.4 / 0.85} />
          </g>
          {/* three six-armed flakes: three crossing lines each */}
          <g strokeWidth={1.7}>
            {(
              [
                [15, 37],
                [24, 41],
                [33, 37],
              ] as const
            ).map(([x, y]) =>
              [0, 60, 120].map((a) => (
                <line
                  key={`${x}-${a}`}
                  x1={x}
                  y1={y - 3.4}
                  x2={x}
                  y2={y + 3.4}
                  transform={`rotate(${a} ${x} ${y})`}
                />
              )),
            )}
          </g>
        </svg>
      );
    case "windy":
      return (
        <svg {...common}>
          <path d="M19.6 8.8A4 4 0 1 1 22 16H4" />
          <path d="M35 16a5 5 0 1 1 4 8H4" />
          <path d="M25.6 39.2A4 4 0 1 0 28 32H4" />
        </svg>
      );
  }
}

/** The icon's slot beside a temperature readout. Fixed-size whether or not a
 * sky is known (house rule #1): no report means an empty reservation, never
 * a collapsed one. */
function ConditionBadge({
  icon,
  size,
  live,
}: {
  icon: ConditionIconKey | null;
  size: number;
  live: boolean;
}) {
  return (
    <span
      className={`flex shrink-0 items-center justify-center ${live ? "text-ink" : "text-inkfaint"}`}
      style={{ width: size, height: size }}
      role={icon ? "img" : undefined}
      aria-label={icon ? CONDITION_LABEL[icon] : undefined}
    >
      {icon && <ConditionGlyph icon={icon} size={size} live={live} />}
    </span>
  );
}

/* --- Weather Post (issue #25): conditions at the seed pile ------------------ */

// Chart geometry. The viewBox is stretched to the panel width
// (preserveAspectRatio="none"), so strokes carry vector-effect to stay crisp.
const WX_W = 320;
const WX_H = 112;
// "now" sits where the trailing 24h meets the leading 48h: 1/3 across.
const WX_NOW_X = (PAST_S / (PAST_S + FUTURE_S)) * WX_W;
// Time-axis ticks every 12h across the fixed window (issue #40) -- pure
// geometry, so computed once at module scope.
const WX_TICKS = timeTicks();
const WEATHER_TICK_MS = 60_000;

/** hh:mm from epoch seconds, viewer-local. Only ever rendered after bus data
 * arrives (client-side), so it can't cause a hydration mismatch. */
function clock(ts: number | null): string {
  if (ts === null) return "—";
  const d = new Date(ts * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** "fri 14:05" -- weekday + clock, viewer-local. The chart spans three days,
 * so the hover readout names the day, not just the hour. */
function dayClock(ts: number): string {
  const day = new Date(ts * 1000)
    .toLocaleDateString(undefined, { weekday: "short" })
    .toLowerCase();
  return `${day} ${clock(ts)}`;
}

/** "—" for a missing reading, the integer otherwise. Shared by the panel and
 * the station view (issue #51) -- a hole in the data is an em-dash, never
 * NaN°. */
const wxRound = (v: number | null) => (v === null ? "—" : Math.round(v));

/** Fixed decimals or the em-dash -- rain and pressure read in hundredths. */
const wxFixed = (v: number | null, digits: number) =>
  v === null ? "—" : v.toFixed(digits);

/** The barometer's tendency as a compass-adjacent glyph. Null trend (trail
 * too short) renders as empty -- the slot is reserved by the row itself. */
const TREND_GLYPH = { rising: "↗", falling: "↘", steady: "→" } as const;

function WeatherPost() {
  const [current, setCurrent] = useState<CurrentWeather | null>(null);
  const [history, setHistory] = useState<WeatherPoint[]>([]);
  const [forecast, setForecast] = useState<WeatherPoint[]>([]);
  // Willard's on-air segment (issue #45): retained LLM narration, absent
  // entirely when the weather service runs without an Ollama (the topic is
  // simply never published).
  const [report, setReport] = useState<WeatherReport | null>(null);
  // Presence (issue #31): retained "online"/"offline" from weather/status.
  // null = no retained status on the broker (pre-#31 service), which falls
  // back to judging Willard purely by report freshness.
  const [status, setStatus] = useState<WeatherStatus | null>(null);
  const [busUp, setBusUp] = useState(false);
  // "now" lives in state, never Date.now() in render (the house hydration
  // rule) -- a slow tick marches the chart window and staleness check forward
  // between reports.
  const [now, setNow] = useState<number | null>(null);

  useEffect(() => {
    const tick = () => setNow(Math.floor(Date.now() / 1000));
    // Deferred a microtask, the StationRecords trick: no synchronous setState
    // path from the effect body, zero visible cost.
    queueMicrotask(tick);
    const id = setInterval(tick, WEATHER_TICK_MS);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    // Its own bus client, same shape as FieldJournal's: each panel stays
    // self-contained (Mosquitto shrugs at a second WebSocket) rather than
    // threading a shared client through two unrelated components. All four
    // weather topics are RETAINED, so the broker replays the latest report,
    // forecast, 48h window, and presence the moment we subscribe -- weather
    // has no HTTP.
    const url = busUrl(
      window.location.hostname,
      process.env.NEXT_PUBLIC_MERLE_MQTT_WS,
    );
    const client = mqtt.connect(url, { reconnectPeriod: 3000 });
    client.on("connect", () => {
      setBusUp(true);
      client.subscribe([
        WEATHER_CURRENT_TOPIC,
        WEATHER_FORECAST_TOPIC,
        WEATHER_HISTORY_TOPIC,
        WEATHER_REPORT_TOPIC,
        WEATHER_STATUS_TOPIC,
      ]);
    });
    client.on("close", () => setBusUp(false));
    // Mandatory: an unhandled mqtt.js "error" throws and wedges its reconnect
    // loop (see FieldJournal, which also surfaces the reason for both panels).
    client.on("error", () => {});
    client.on("message", (topic, payload) => {
      const text = payload.toString();
      if (topic === WEATHER_CURRENT_TOPIC) {
        const report = parseCurrent(text);
        if (report) {
          setCurrent(report);
          setNow(Math.floor(Date.now() / 1000));
        }
      } else if (topic === WEATHER_FORECAST_TOPIC) {
        setForecast(parsePoints(text) ?? []);
      } else if (topic === WEATHER_HISTORY_TOPIC) {
        setHistory(parsePoints(text) ?? []);
      } else if (topic === WEATHER_REPORT_TOPIC) {
        setReport(parseReport(text));
      } else if (topic === WEATHER_STATUS_TOPIC) {
        setStatus(parseStatus(text));
      }
    });
    return () => {
      client.end(true);
    };
  }, []);

  // Presence beats freshness: a retained "offline" (graceful stop or the
  // Last Will after a crash) is announced the moment it happens, while
  // staleness needs 3 missed polls to accrue. No retained status at all
  // (pre-#31 service) leaves the freshness judgement in charge.
  const offline = status === "offline";
  // A report older than 3 missed polls is presented as stale, never as now.
  const stale =
    current !== null && now !== null && current.ts < now - STALE_AFTER_S;
  const reporting = current !== null && !stale && !offline;
  // The segment ages on its own clock (broadcasts are ~30 min apart, polls
  // 10): past REPORT_STALE_S it's history, not news, and the between-
  // broadcasts state takes over.
  const onAir =
    report !== null && now !== null && now - report.ts <= REPORT_STALE_S;

  // Hover scrub (issue #40): pointer x as a fraction of the chart width, null
  // when the pointer is elsewhere. Everything else is derived per render.
  const [hoverFrac, setHoverFrac] = useState<number | null>(null);

  // The station view (issue #51): a full-screen overlay off the masthead.
  const [stationView, setStationView] = useState(false);

  // The panel's window is FIXED at 24h/48h and deliberately not pannable
  // (issue #106): six days in ~400px would be a smear, so its legibility
  // ceiling is a decision, not an oversight. The station view is where you
  // drag through history.
  const ts0 = (now ?? 0) - PAST_S;
  const ts1 = (now ?? 0) + FUTURE_S;
  const trend: Trend =
    now !== null
      ? trendSeries(history, forecast, now, ts0, ts1)
      : { observed: [], coming: [], bridged: false };
  const allPts = [...trend.observed, ...trend.coming];
  const range = tempRange(allPts);
  const windMax = windCeil(allPts);
  const hasChart = now !== null && range !== null && allPts.length > 1;

  // The readout snaps to the nearest real report/forecast point -- the
  // crosshair sits at that point's time, not at the raw pointer.
  const hovered =
    hasChart && hoverFrac !== null
      ? nearestPoint(allPts, ts0 + hoverFrac * (ts1 - ts0))
      : null;
  const hoveredFrac = hovered ? (hovered.ts - ts0) / (ts1 - ts0) : 0;
  const nights = hasChart
    ? nightBands(current?.sunrise ?? null, current?.sunset ?? null, ts0, ts1)
    : [];
  // The barometer's tendency, judged from the observed trail (issue #51).
  const baroTrend = now !== null ? pressureTrend(history, now) : null;

  const round = wxRound;

  return (
    <section className="panel flex flex-col rounded-sm border border-line bg-panel">
      {/* The masthead bills the reporter, Field Journal style -- conditions
          themselves live down in the data block with the temperature. The
          expand control opens the station view (issue #51). */}
      <PanelLabel
        title="Weather Post"
        right={
          <span className="flex items-center gap-2.5">
            {!busUp ? (
              <span className="stamp text-xs text-inkfaint">bus quiet</span>
            ) : offline ? (
              <span className="flex items-center gap-1.5 text-xs">
                <span className="inline-block h-2 w-2 rounded-full bg-inkfaint" />
                <span className="stamp text-inkfaint">
                  willard · on coffee break
                </span>
              </span>
            ) : current === null ? (
              <span className="flex items-center gap-1.5 text-xs">
                <span className="inline-block h-2 w-2 rounded-full bg-inkfaint" />
                <span className="stamp text-inkfaint">
                  willard · off the air
                </span>
              </span>
            ) : stale ? (
              <span className="flex items-center gap-1.5 text-xs">
                <span className="breathe inline-block h-2 w-2 rounded-full bg-turkey" />
                <span className="stamp text-turkey">
                  willard · stale report {clock(current.ts)}
                </span>
              </span>
            ) : (
              <span className="flex items-center gap-1.5 text-xs">
                <span className="lamp inline-block h-2 w-2 rounded-full bg-led text-led" />
                <span className="stamp text-led">willard with the weather</span>
              </span>
            )}
            <button
              type="button"
              onClick={() => setStationView(true)}
              aria-label="Open the station view"
              className="text-inkfaint transition-colors hover:text-squirrel"
            >
              <ExpandIcon />
            </button>
          </span>
        }
      />
      {/* `relative` so the off-air message can overlay the (dimmed) data
          skeleton instead of sharing its space -- same veil idea as the
          Live Watch feed. */}
      <div className="relative flex-1 px-4 pb-4">
        <div
          className={
            current === null || offline
              ? "opacity-30 transition-opacity"
              : "transition-opacity"
          }
        >
        {/* Current conditions -- a fixed-height block whether or not a report
            is in, so the panel never shifts as data arrives (house rule #1).
            The right column grew with the station (issue #51): five fixed
            rows, each rendered with em-dash placeholders before data.
            Top-aligned (issue #54): items-end left a dead void between the
            masthead and the temperature once the right column stretched the
            block -- the headline belongs up under the masthead, sized to
            fill the height the telemetry rows set. */}
        <div className="flex min-h-[92px] items-start justify-between gap-3">
          {/* The sky's icon (issue #78) rides left of the number, centered on
              the temperature + description block. */}
          <div className="flex items-center gap-3">
            <ConditionBadge
              icon={conditionIcon(current)}
              size={44}
              live={reporting}
            />
            <div>
              <div className="flex items-baseline gap-2.5">
                <span
                  className={`text-5xl font-bold tabular-nums ${reporting ? "text-ink" : "text-inkfaint"}`}
                >
                  {round(current?.temp_f ?? null)}°
                </span>
                <span className="text-base text-inkdim">
                  feels {round(current?.feels_like_f ?? null)}°
                </span>
              </div>
              {/* Conditions ride with the temperature -- the sky gets headline
                  billing next to the number (issue #54), not caption type.
                  min-h reserves the line before the first report, so nothing
                  shifts when it lands. */}
              <div
                className={`mt-0.5 min-h-[24px] text-base ${reporting ? "text-ink" : "text-inkfaint"}`}
              >
                {current?.description ?? ""}
              </div>
            </div>
          </div>
          <div className="flex flex-col items-end gap-0.5 text-[11px] text-inkdim">
            <span>
              wind {round(current?.wind_mph ?? null)} mph{" "}
              {compass(current?.wind_deg ?? null)}
              {current?.wind_gust_mph != null &&
                ` · gusts ${Math.round(current.wind_gust_mph)}`}
            </span>
            <span>
              humidity {round(current?.humidity_pct ?? null)}% · dew{" "}
              {round(current?.dew_point_f ?? null)}°
            </span>
            <span>
              baro {wxFixed(current?.pressure_rel_inhg ?? null, 2)} in
              {baroTrend && ` ${TREND_GLYPH[baroTrend]}`}
              {current?.uv_index != null &&
                current.uv_index >= 1 &&
                ` · uv ${Math.round(current.uv_index)}`}
            </span>
            <span>
              rain {wxFixed(current?.rain_day_in ?? null, 2)} in today
              {current?.raining === 1 &&
                ` · ${wxFixed(current?.rain_rate_inhr ?? null, 2)}/hr`}
            </span>
            <span className="stamp text-[10px] text-inkfaint">
              sun {clock(current?.sunrise ?? null)} –{" "}
              {clock(current?.sunset ?? null)}
            </span>
          </div>
        </div>

        {/* The gateway's indoor instruments: one quiet strip, deliberately
            below the outdoor stage (issue #51) -- the station's other room,
            never competing with the sky. Fixed single row, always rendered. */}
        <div className="mt-2 flex items-baseline justify-between border-t border-line pt-1.5 text-[11px]">
          <span className="stamp text-[10px] text-inkfaint">
            indoors · at the gateway
          </span>
          <span className="tabular-nums text-inkdim">
            {round(current?.indoor_temp_f ?? null)}° ·{" "}
            {round(current?.indoor_humidity_pct ?? null)}% rh
          </span>
        </div>

        {/* The trend: observed temps trail left of "now" (solid), the forecast
            extends right (dashed). Wind rides its own scale underneath. */}
        {/* The scale itself now lives on the chart edges (issue #40); this
            row is just the series key. */}
        <div className="mt-2 text-[10px] text-inkfaint">
          <span>
            <span className="text-squirrel">—</span> temp °F ·{" "}
            <span className="text-inkdim">—</span> wind mph
          </span>
        </div>
        {/* `relative` roots the hover overlay; pan-y leaves vertical page
            scroll alone on touch while horizontal drags scrub the chart. */}
        <div
          className="relative mt-1 h-28 w-full"
          style={{ touchAction: "pan-y" }}
          onPointerMove={(e) => {
            const r = e.currentTarget.getBoundingClientRect();
            if (r.width > 0)
              setHoverFrac(
                Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
              );
          }}
          onPointerLeave={() => setHoverFrac(null)}
          onPointerCancel={() => setHoverFrac(null)}
        >
          {hasChart && (
            <svg
              viewBox={`0 0 ${WX_W} ${WX_H}`}
              preserveAspectRatio="none"
              className="h-full w-full"
              role="img"
              aria-label="Observed and forecast temperature and wind"
            >
              {/* night first: shading sits behind everything else */}
              {nights.map((b) => (
                <rect
                  key={b.start}
                  x={((b.start - ts0) / (ts1 - ts0)) * WX_W}
                  y={0}
                  width={((b.end - b.start) / (ts1 - ts0)) * WX_W}
                  height={WX_H}
                  fill="black"
                  opacity="0.25"
                />
              ))}
              {WX_TICKS.map((t) => (
                <line
                  key={t.offsetS}
                  x1={t.frac * WX_W}
                  y1={0}
                  x2={t.frac * WX_W}
                  y2={WX_H}
                  stroke="var(--line)"
                  vectorEffect="non-scaling-stroke"
                />
              ))}
              <line
                x1={WX_NOW_X}
                y1={0}
                x2={WX_NOW_X}
                y2={WX_H}
                stroke="var(--line-bright)"
                strokeDasharray="2 4"
                vectorEffect="non-scaling-stroke"
              />
              {/* wind first so temperature reads on top */}
              <path
                d={linePath(trend.observed, (p) => p.wind_mph, ts0, ts1, 0, windMax, WX_W, WX_H)}
                fill="none"
                stroke="var(--ink-dim)"
                strokeWidth="1"
                opacity="0.8"
                vectorEffect="non-scaling-stroke"
              />
              <path
                d={linePath(trend.coming, (p) => p.wind_mph, ts0, ts1, 0, windMax, WX_W, WX_H)}
                fill="none"
                stroke="var(--ink-dim)"
                strokeWidth="1"
                strokeDasharray="3 3"
                opacity="0.5"
                vectorEffect="non-scaling-stroke"
              />
              <path
                d={linePath(trend.observed, (p) => p.temp_f, ts0, ts1, range.min, range.max, WX_W, WX_H)}
                fill="none"
                stroke="var(--squirrel)"
                strokeWidth="1.8"
                vectorEffect="non-scaling-stroke"
              />
              <path
                d={linePath(trend.coming, (p) => p.temp_f, ts0, ts1, range.min, range.max, WX_W, WX_H)}
                fill="none"
                stroke="var(--squirrel)"
                strokeWidth="1.4"
                strokeDasharray="4 3"
                opacity="0.65"
                vectorEffect="non-scaling-stroke"
              />
              {hovered && (
                <line
                  x1={hoveredFrac * WX_W}
                  y1={0}
                  x2={hoveredFrac * WX_W}
                  y2={WX_H}
                  stroke="var(--ink-dim)"
                  strokeDasharray="1 3"
                  vectorEffect="non-scaling-stroke"
                />
              )}
            </svg>
          )}
          {/* Scale whispers on the chart edges, tinted to their series so they
              self-identify. Overlaid, not laid out -- zero footprint change,
              and the empty skeleton keeps the exact same box. */}
          {hasChart && range !== null && (
            <>
              <span className="pointer-events-none absolute left-1 top-0.5 text-[9px] tabular-nums text-squirrel opacity-60">
                {range.max}°
              </span>
              <span className="pointer-events-none absolute bottom-0.5 left-1 text-[9px] tabular-nums text-squirrel opacity-60">
                {range.min}°
              </span>
              <span className="pointer-events-none absolute right-1 top-0.5 text-[9px] tabular-nums text-inkfaint">
                {windMax} mph
              </span>
            </>
          )}
          {/* Hover readout: dots mark the snapped point on each line, the chip
              reads its conditions. HTML overlay (never SVG text): the viewBox
              is stretched, and an overlay can't shift layout (house rule #1).
              The chip rides the roomier side of the crosshair. */}
          {hovered && range !== null && now !== null && (
            <>
              {hovered.temp_f !== null && (
                <span
                  className="pointer-events-none absolute h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full border border-squirrel bg-panel"
                  style={{
                    left: `${hoveredFrac * 100}%`,
                    top: `${(1 - (hovered.temp_f - range.min) / (range.max - range.min)) * 100}%`,
                  }}
                />
              )}
              {hovered.wind_mph !== null && (
                <span
                  className="pointer-events-none absolute h-1.5 w-1.5 -translate-x-1/2 -translate-y-1/2 rounded-full border border-inkdim bg-panel"
                  style={{
                    left: `${hoveredFrac * 100}%`,
                    top: `${(1 - hovered.wind_mph / windMax) * 100}%`,
                  }}
                />
              )}
              <div
                className="pointer-events-none absolute top-1 z-10 whitespace-nowrap rounded-sm border border-linebright bg-panel2 px-2 py-1"
                style={
                  hoveredFrac < 0.5
                    ? { left: `calc(${hoveredFrac * 100}% + 10px)` }
                    : { right: `calc(${(1 - hoveredFrac) * 100}% + 10px)` }
                }
              >
                <div className="stamp text-[10px] text-inkfaint">
                  {dayClock(hovered.ts)} ·{" "}
                  {hovered.ts <= now ? "observed" : "forecast"}
                </div>
                <div className="text-[11px] tabular-nums text-ink">
                  {round(hovered.temp_f)}°
                  {hovered.condition && (
                    <span className="text-inkdim">
                      {" "}
                      · {hovered.condition.toLowerCase()}
                    </span>
                  )}
                </div>
                <div className="text-[10px] tabular-nums text-inkdim">
                  wind {round(hovered.wind_mph)} mph
                  {hovered.wind_gust_mph !== null &&
                    ` · gusts ${Math.round(hovered.wind_gust_mph)}`}
                </div>
              </div>
            </>
          )}
        </div>
        <div className="relative mt-0.5 h-4 text-[9px] text-inkfaint">
          <span className="absolute left-0">−24h</span>
          {WX_TICKS.map((t) => (
            <span
              key={t.offsetS}
              className="absolute -translate-x-1/2 tabular-nums"
              style={{ left: `${t.frac * 100}%` }}
            >
              {t.offsetS > 0 ? `+${t.offsetS / 3600}h` : `−${-t.offsetS / 3600}h`}
            </span>
          ))}
          <span
            className="stamp absolute -translate-x-1/2 text-inkdim"
            style={{ left: `${(WX_NOW_X / WX_W) * 100}%` }}
          >
            now
          </span>
          <span className="absolute right-0">+48h</span>
        </div>

        {/* Willard's on-air segment (issue #45): the LLM narration, retained
            on weather/report, in the display face -- the Field Journal
            convention (voice = display, telemetry = mono). Inside the
            dimmable block, so it fades with the rest when he's off duty.
            min-h reserves the slot before (or between) broadcasts, so a
            segment landing never shifts the chart above (house rule #1). */}
        <div className="mt-3 min-h-[110px] border-t border-line pt-2">
          {onAir && report !== null ? (
            <>
              <div className="flex gap-2 text-[11px]">
                <span className="stamp text-inkdim">willard, on the air</span>
                <span className="text-inkfaint">{clock(report.ts)}</span>
              </div>
              <p
                className="journal-in mt-1 whitespace-pre-line text-[15px] leading-snug text-ink"
                style={{ fontFamily: "var(--font-display)" }}
              >
                {report.text}
              </p>
            </>
          ) : (
            <p className="py-2 text-sm leading-relaxed text-inkfaint">
              willard is between broadcasts — the forecast desk is quiet
            </p>
          )}
        </div>
        </div>

        {/* Off-air: the message floats over the dimmed skeleton instead of
            wedging into the chart slot next to live-looking numerals. Three
            distinct states, like the Live Watch veils: bus unreachable, the
            service announced offline (with when he last checked in -- the
            chart's "now" line stops meaning anything once he's off duty),
            and no retained report at all. */}
        {(current === null || offline) && (
          <div className="absolute inset-0 flex items-center justify-center px-6">
            <p className="text-center text-sm leading-relaxed text-inkfaint">
              {!busUp ? (
                "waiting on the bus — the weather rides it"
              ) : offline ? (
                <>
                  willard is on coffee break — the weather post is stopped.
                  {current !== null && now !== null && (
                    <span className="stamp mt-1 block text-xs text-inkdim">
                      last checked in {clock(current.ts)} ·{" "}
                      {ageText(current.ts, now)}
                    </span>
                  )}
                  <code className="mx-auto mt-1 block w-fit rounded-sm bg-panel2 px-2 py-1 text-xs text-inkdim">
                    sudo systemctl start willard-weather
                  </code>
                </>
              ) : (
                <>
                  no weather report yet. put Willard on duty (he lives on
                  pearl):
                  <code className="mx-auto mt-1 block w-fit rounded-sm bg-panel2 px-2 py-1 text-xs text-inkdim">
                    sudo systemctl start willard-weather
                  </code>
                </>
              )}
            </p>
          </div>
        )}
      </div>

      {/* The station view (issue #51): the same bus data, writ large. */}
      {stationView && (
        <WeatherStationView
          current={current}
          history={history}
          forecast={forecast}
          report={report}
          now={now}
          reporting={reporting}
          offline={offline}
          onAir={onAir}
          baroTrend={baroTrend}
          onClose={() => setStationView(false)}
        />
      )}
    </section>
  );
}

/* --- The Station View (issue #51): the Weather Post, writ large ------------- */

// Large-chart geometry. Same stretched-viewBox rules as the panel chart
// (strokes carry vector-effect), but its own window (issue #60): 24h back +
// the full 5-day forecast forward, "now" at the 1/6 mark. Gridlines are the
// viewer's local midnights (dayTicks), not 12h offsets -- at 144h, days are
// the honest unit. The strips underneath share ts0/ts1 with the main chart,
// so every timestamp lines up vertically across all four.
const WXL_W = 960;
const WXL_H = 240;
const WXL_STRIP_H = 72;
// How much archive to ask for at a time when panning past what we hold
// (issue #106). A week of 5-minute rows is ~2000 points -- a chunk big enough
// that a station outage doesn't read as the end of the record, and small
// enough to land before the viewer notices. History is immutable, so each
// chunk is fetched exactly once.
const ARCHIVE_CHUNK_S = 7 * 86400;
// A press that travels less than this is a tap, not a drag: the crosshair
// placement a touchscreen can't express as hover.
const TAP_SLOP_PX = 4;
// OpenWeather's forecast step: each point's rain_rate_inhr (issue #56) is the
// average over the 3 hours ENDING at its ts ("volume for last 3 hours"), so
// its ghost bar spans that window on the strip.
const WX_FORECAST_STEP_S = 3 * 3600;

/** The circled i riding after a WxStat label (issue #63) -- drawn like
 * CloseIcon, sized to sit inside the stamp line. */
function InfoIcon() {
  return (
    <svg
      viewBox="0 0 12 12"
      width="11"
      height="11"
      aria-hidden="true"
      className="shrink-0"
    >
      <circle
        cx="6"
        cy="6"
        r="5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1"
      />
      <line
        x1="6"
        y1="5.3"
        x2="6"
        y2="8.6"
        stroke="currentColor"
        strokeWidth="1.3"
      />
      <circle cx="6" cy="3.3" r="0.8" fill="currentColor" />
    </svg>
  );
}

/** The wind cell's compass rose (issue #63): a ring with cardinal ticks and
 * a needle rotated to the bearing the wind comes FROM (matching the letter
 * beside it -- a "W" wind points the needle west). No bearing draws the
 * ring and ticks alone, reserving the same footprint (house rule #1). */
function CompassGlyph({ deg }: { deg: number | null }) {
  return (
    <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
      <circle
        cx="8"
        cy="8"
        r="7"
        fill="none"
        stroke="var(--line-bright)"
        strokeWidth="1"
      />
      {[0, 90, 180, 270].map((t) => (
        <line
          key={t}
          x1="8"
          y1="1"
          x2="8"
          y2="2.6"
          stroke="var(--ink-faint)"
          strokeWidth="1"
          transform={`rotate(${t} 8 8)`}
        />
      ))}
      {deg !== null && (
        <path
          d="M8 2.8 L10.1 10.2 L8 8.7 L5.9 10.2 Z"
          fill="var(--squirrel)"
          transform={`rotate(${deg} 8 8)`}
        />
      )}
    </svg>
  );
}

/** One labeled reading in the hero grid: stamped label, instrument-sized
 * value, the unit riding quietly after it, `aside` riding to their right
 * (the wind cell's compass). `info` (issue #63) appends a quiet circled-i
 * whose hover/focus reveals a field-manual note in the readout chip's
 * dress -- pure CSS, an absolute overlay (house rule #1: revealing it can
 * never shift the grid), focusable so keyboards and touch taps get it
 * too. */
function WxStat({
  label,
  value,
  unit,
  sub,
  info,
  aside,
}: {
  label: string;
  value: string | number;
  unit?: string;
  sub?: React.ReactNode;
  info?: string;
  aside?: React.ReactNode;
}) {
  return (
    <div className="border-l border-line pl-3">
      <div className="stamp flex items-center gap-1.5 text-[10px] text-inkfaint">
        {label}
        {info && (
          <span
            tabIndex={0}
            aria-label={`about ${label}`}
            className="group relative -my-1 inline-flex cursor-help rounded-sm py-1 text-inkfaint/70 outline-none transition-colors hover:text-inkdim focus-visible:text-inkdim"
          >
            <InfoIcon />
            <span
              role="tooltip"
              className="pointer-events-none invisible absolute left-1/2 top-full z-20 mt-1.5 w-60 -translate-x-1/2 rounded-sm border border-linebright bg-panel2 px-2.5 py-2 text-[11px] font-normal normal-case leading-relaxed tracking-normal text-inkdim opacity-0 transition-opacity duration-150 group-hover:visible group-hover:opacity-100 group-focus-within:visible group-focus-within:opacity-100"
            >
              {info}
            </span>
          </span>
        )}
      </div>
      <div className="flex items-center gap-2 text-xl tabular-nums text-ink">
        <span>
          {value}
          {unit && <span className="ml-1 text-xs text-inkdim">{unit}</span>}
        </span>
        {aside}
      </div>
      {/* min-h reserves the sub-line so the grid rows stay ranked even when
          a reading has nothing extra to say (house rule #1). */}
      <div className="min-h-[16px] text-[11px] text-inkdim">{sub}</div>
    </div>
  );
}

/** Segment meter for the WH90's battery (0-5) and radio signal (0-4) -- gear
 * gauge, not percentage bar. Unknown level draws all sockets empty. */
function SegMeter({
  n,
  of,
  tone = "var(--led)",
}: {
  n: number | null;
  of: number;
  tone?: string;
}) {
  return (
    <span className="inline-flex items-center gap-0.5">
      {Array.from({ length: of }, (_, i) => (
        <span
          key={i}
          className="h-1.5 w-2.5 rounded-[1px]"
          style={
            n !== null && i < n
              ? { backgroundColor: tone }
              : { backgroundColor: "var(--panel-2)", border: "1px solid var(--line)" }
          }
        />
      ))}
    </span>
  );
}

/** One instrument strip under the main chart: its own svg sharing the time
 * axis, the now-divider, and faint tick rules for alignment. The series itself
 * comes in as svg children.
 *
 * The label sits in the forecast half because the station's instruments mostly
 * leave the future blank -- but that is no longer a reason to expect an EMPTY
 * corner, and it was the reason for a long time. Issue #56 noted the rain strip
 * as the one exception whose ghosts "stay well below the label's corner"; #65
 * then rescaled those ghosts to CHANCE on a fixed 0-100% strip, where a
 * confident Friday is a full-height bar growing straight into it. The label
 * stops relying on an empty corner and brings its own scrim instead (#113). */
function WxStrip({
  label,
  scale,
  ticks,
  nowFrac,
  children,
}: {
  label: string;
  scale: string;
  /** gridline positions as window fractions -- the midnights of dayTicks,
   *  computed once by the station view so all four charts agree */
  ticks: number[];
  /** the seam's position in the window, or null once the viewer has panned
   *  far enough back that "now" isn't on the chart at all (issue #106) */
  nowFrac: number | null;
  children: React.ReactNode;
}) {
  return (
    <div className="relative mt-1 h-[72px] w-full border-t border-line/60">
      <svg
        viewBox={`0 0 ${WXL_W} ${WXL_STRIP_H}`}
        preserveAspectRatio="none"
        className="h-full w-full"
        aria-hidden="true"
      >
        {ticks.map((frac) => (
          <line
            key={frac}
            x1={frac * WXL_W}
            y1={0}
            x2={frac * WXL_W}
            y2={WXL_STRIP_H}
            stroke="var(--line)"
            opacity="0.5"
            vectorEffect="non-scaling-stroke"
          />
        ))}
        {nowFrac !== null && (
          <line
            x1={nowFrac * WXL_W}
            y1={0}
            x2={nowFrac * WXL_W}
            y2={WXL_STRIP_H}
            stroke="var(--line-bright)"
            strokeDasharray="2 4"
            vectorEffect="non-scaling-stroke"
          />
        )}
        {children}
      </svg>
      {/* The heading and the scale whisper both sit OVER the data now, not
          beside it (issue #113). The corner used to be reliably empty -- #56
          put the label there precisely because the station leaves the future
          blank -- but #65 rescaled the forecast ghosts to chance on a fixed
          0-100% strip, so a confident Friday is a full-height bar growing
          straight into this corner. The premise is gone, so the label stops
          relying on it: it wears the panel as a scrim and reads at station-view
          size instead of whispering at 10px in the palest ink on the board.
          Both stay absolute overlays, so none of this can shift the chart. */}
      <span className="stamp pointer-events-none absolute right-1 top-1 rounded-sm bg-panel/80 px-1 py-px text-[11px] text-inkdim">
        {label}
      </span>
      <span className="pointer-events-none absolute left-1 top-0.5 rounded-sm bg-panel/80 px-1 py-px text-[11px] tabular-nums text-inkdim">
        {scale}
      </span>
    </div>
  );
}

/** The station chart's calendar axis (#60): weekday labels centered over
 * their day's slice between midnight gridlines, endpoints and the now stamp
 * anchoring the window. Narrow partial days at the window's edges and
 * anything shadowing the now stamp stay quiet -- the hover chip names every
 * point anyway. Rendered twice since issue #65's follow-up: once above the
 * chart stack and once below, so neither end of a tall chart leaves the
 * reader guessing which day they're over. */
function WxTimeAxis({
  days,
  nowFrac,
  leftLabel,
  rightLabel,
  className,
}: {
  days: DayTick[];
  /** null once "now" is off the chart -- a window panned into the past has
   *  no seam to stamp, and the corners say how far back it sits (#106) */
  nowFrac: number | null;
  leftLabel: string;
  rightLabel: string;
  className: string;
}) {
  return (
    <div className={`relative h-4 text-[10px] text-inkfaint ${className}`}>
      <span className="absolute left-0">{leftLabel}</span>
      {days.map((t, i) => {
        const end = days[i + 1]?.frac ?? 1;
        const center = (t.frac + end) / 2;
        if (end - t.frac < 0.05) return null;
        if (nowFrac !== null && Math.abs(center - nowFrac) < 0.04) return null;
        if (center > 0.96) return null;
        return (
          <span
            key={t.ts}
            className="stamp absolute -translate-x-1/2"
            style={{ left: `${center * 100}%` }}
          >
            {t.label}
          </span>
        );
      })}
      {nowFrac !== null && (
        <span
          className="stamp absolute -translate-x-1/2 text-inkdim"
          style={{ left: `${nowFrac * 100}%` }}
        >
          now
        </span>
      )}
      <span className="absolute right-0">{rightLabel}</span>
    </div>
  );
}

function CloseIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      aria-hidden="true"
    >
      <path d="M6 6l12 12M18 6L6 18" />
    </svg>
  );
}

function WeatherStationView({
  current,
  history,
  forecast,
  report,
  now,
  reporting,
  offline,
  onAir,
  baroTrend,
  onClose,
}: {
  current: CurrentWeather | null;
  history: WeatherPoint[];
  forecast: WeatherPoint[];
  report: WeatherReport | null;
  now: number | null;
  reporting: boolean;
  offline: boolean;
  onAir: boolean;
  baroTrend: "rising" | "falling" | "steady" | null;
  onClose: () => void;
}) {
  // The Live Watch CSS-overlay contract: Escape closes, body scroll locks
  // while the view is up. No requestFullscreen here -- this is a page of
  // instruments, not a video, and it should scroll on small screens.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [onClose]);

  // Hover scrub, the panel chart's contract: pointer x as a fraction of the
  // chart stack's width; the crosshair spans main chart and strips alike.
  const [hoverFrac, setHoverFrac] = useState<number | null>(null);

  // --- The pannable window (issue #106) -----------------------------------
  // null means LIVE: the window tracks `now` and sits exactly where it always
  // has (24h back, 120h ahead, "now" at the 1/6 mark). Once panned it holds
  // an ABSOLUTE right edge instead -- a window the viewer dragged to last
  // Tuesday must stay on last Tuesday, not creep rightward as `now` ticks
  // forward underneath it. It doubles as the snap-back control's state: home
  // is null, not a timestamp to recompute.
  const [windowEnd, setWindowEnd] = useState<number | null>(null);
  // Whatever the archive has handed over so far, additive to the retained bus
  // window and never a replacement for it (#105 is a second source, not the
  // trail's source). History is immutable once past, so a range fetched once
  // is never refetched -- this only ever grows.
  const [archived, setArchived] = useState<WeatherPoint[]>([]);
  // The wall: set when the archive says there is nothing older, or when it
  // can't be reached at all. Both mean the same thing to a viewer -- the
  // record stops here -- and both must stop us asking, or a drag pinned at
  // the wall would refetch forever.
  const [exhausted, setExhausted] = useState(false);
  const drag = useRef<{ x: number; ts1: number; moved: boolean } | null>(null);
  const dragging = useRef(false);
  const inFlight = useRef(0);
  // The floor we last asked below. A ref, not state, and this is the whole
  // reason it exists: a chunk resolving does not re-render synchronously, so
  // a pointermove landing between the fetch settling and React catching up
  // still sees the previous render's `oldestKnown` and would ask for the same
  // week a second time. inFlight can't catch that -- it's already back to 0.
  const askedBelow = useRef<number | null>(null);

  const live = windowEnd === null;
  const newest = (now ?? 0) + STATION_FUTURE_S;
  const ts1 = windowEnd ?? newest;
  const ts0 = ts1 - STATION_SPAN_S;

  // The trail is the bus window plus the archive, deduped: the two overlap by
  // design (the archive holds the same rows the window does).
  const trail = mergePoints(history, archived);
  const oldestKnown = trail.length ? trail[0].ts : ts0;

  const trend: Trend =
    now !== null
      ? trendSeries(trail, forecast, now, ts0, ts1)
      : { observed: [], coming: [], bridged: false };
  const allPts = [...trend.observed, ...trend.coming];
  const nights = nightBands(
    current?.sunrise ?? null,
    current?.sunset ?? null,
    ts0,
    ts1,
  );
  // Local midnights, the window's gridlines and axis labels alike (#60),
  // now over whatever window the viewer dragged to rather than `now`'s.
  const days = now !== null ? dayTicks(ts0, ts1) : [];
  const dayFracs = days.map((d) => d.frac);
  // Where "now" falls in the window -- the 1/6 mark while live, off the left
  // edge entirely once panned back a few days. Null means the seam isn't in
  // view and nothing should draw it.
  const nowFrac =
    now !== null && now >= ts0 && now <= ts1 ? (now - ts0) / (ts1 - ts0) : null;

  // --- The axes settle when the gesture does (issue #106) -----------------
  // Every scale here is computed from the points in view, so a drag would
  // rescale them on every frame and the chart would breathe -- the
  // no-layout-shift rule violated inside the chart's own frame. They freeze
  // at whatever is on screen when a drag starts and settle once when the
  // gesture is over AND any fetch it kicked off has landed: one controlled
  // rescale when the viewer is done moving, never a jitter under the finger
  // and never a jump the moment a fetch returns.
  const liveAxes = {
    range: tempRange(allPts),
    windMax: windCeil(allPts),
    rainMax: seriesCeil(trend.observed, (p) => p.rain_rate_inhr, 0.25, 0.25),
    snowMax: seriesCeil(trend.coming, (p) => p.snow_3h_in, 1, 0.5),
    solarMax: seriesCeil(trend.observed, (p) => p.solar_wm2, 200, 100),
    uvMax: seriesCeil(trend.observed, (p) => p.uv_index, 4, 2),
    presRange: pressureRange(trend.observed),
  };
  const [frozenAxes, setFrozenAxes] = useState<typeof liveAxes | null>(null);
  const { range, windMax, rainMax, snowMax, solarMax, uvMax, presRange } =
    frozenAxes ?? liveAxes;
  const hasChart = now !== null && range !== null && allPts.length > 1;

  // The forecast's peaks and valleys (issue #113): what a viewer actually wants
  // to know -- how hot Friday gets, how cold tonight -- without hovering point
  // to point. Turning points, not per-calendar-day min/max, so a valley that
  // spans midnight gets ONE label; see tempMarks. Forecast only: the observed
  // trail is 5-minute data where every passing cloud is a turning point -- and
  // `bridged` keeps the seam stitch from being the one cloud that gets through.
  const marks = hasChart ? tempMarks(trend.coming, trend.bridged) : [];

  // --- Panning ------------------------------------------------------------
  // The first drag interaction in the codebase, so it sets the pattern:
  // pointer events throughout (one path for mouse and touch, as the hover
  // scrub already does), setPointerCapture so a drag that leaves the chart
  // doesn't strand mid-gesture, and touch-action: pan-y on the surface so a
  // horizontal drag pans while a vertical one still scrolls the overlay.
  const settleAxes = () => {
    if (inFlight.current === 0 && !dragging.current) setFrozenAxes(null);
  };

  /** Ask the archive for the chunk older than everything we hold. Keyed off
   * what the viewer ASKED for, never the clamped result: the clamp pins ts0
   * at the wall, so waiting for the clamped window to cross it would mean the
   * request never fires at all. Nothing here fetches while the window sits
   * inside the retained trail -- the first day of panning is already on the
   * wire and costs no network. */
  const askArchive = (wantTs0: number) => {
    if (exhausted || inFlight.current > 0) return;
    if (!trail.length || wantTs0 >= oldestKnown) return;
    // Already asked below this floor: the answer is in flight or already
    // merged, and history doesn't change once past. A range is fetched once.
    if (askedBelow.current !== null && oldestKnown >= askedBelow.current) return;
    askedBelow.current = oldestKnown;
    inFlight.current += 1;
    fetchArchive(oldestKnown - ARCHIVE_CHUNK_S, oldestKnown - 1)
      .then((pts) => {
        // Nothing older exists: this is the archive's first reading, the end
        // of the record rather than a gap in it. Stop here and stop asking.
        if (pts.length === 0) setExhausted(true);
        else setArchived((a) => mergePoints(a, pts));
      })
      // Route unreachable or unset MERLE_WEATHER_DB: the chart is exactly the
      // chart it was before the archive existed, and panning stops at the
      // retained window's edge. Never a thrown error at a viewer.
      .catch(() => setExhausted(true))
      .finally(() => {
        inFlight.current -= 1;
        settleAxes();
      });
  };

  const panTo = (wantTs1: number) => {
    const c = clampWindow(
      wantTs1 - STATION_SPAN_S,
      wantTs1,
      oldestKnown,
      newest,
    );
    // Dragged back to the right edge: return to LIVE rather than freezing an
    // absolute end that `now` would immediately outrun.
    setWindowEnd(c.ts1 >= newest ? null : c.ts1);
    askArchive(wantTs1 - STATION_SPAN_S);
  };

  const fracAt = (e: React.PointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    return r.width > 0
      ? Math.min(1, Math.max(0, (e.clientX - r.left) / r.width))
      : null;
  };

  // The barometer's tendency treatment, extended to the rest of the desk
  // (issue #67). Temperature and humidity mostly ride the sun -- honest,
  // just diurnal; a moving dew point is the interesting one, it means the
  // air itself is changing. Null (short trail) renders as reserved blank.
  const tempTrend =
    now !== null
      ? seriesTrend(history, now, (p) => p.temp_f, TEMP_TREND_EPS_F)
      : null;
  const dewTrend =
    now !== null
      ? seriesTrend(history, now, (p) => p.dew_point_f, DEW_TREND_EPS_F)
      : null;
  const humidityTrend =
    now !== null
      ? seriesTrend(history, now, (p) => p.humidity_pct, HUMIDITY_TREND_EPS_PCT)
      : null;

  // The strips chart the observed trail -- the forecast carries none of
  // these series, and an honest chart leaves the future blank. Two
  // exceptions: the rain strip's future half charts the CHANCE of
  // precipitation (issue #65 -- pop is what a viewer plans around; the
  // forecast's volumes read as slivers on the piezo's scale), and the snow
  // strip is forecast-only (the piezo is snow-blind, so its observed half
  // is honestly blank forever). The rain ceiling is observed-only again --
  // the future half rides its own fixed 0-100% scale. Their ceilings ride
  // the settling rule with every other axis (issue #106).
  const observed = trend.observed;
  // The snow strip goes seasonal (issue #69, owner's call): hidden April
  // through October rather than sitting dead for seven months -- but a
  // forecast actually carrying snow shows it in any month. The valve only
  // ever ADDS the row; the gate never hides real data.
  const showSnow =
    now !== null &&
    (snowSeason(now) ||
      trend.coming.some((p) => (p.snow_3h_in ?? 0) > 0));

  const hovered =
    hasChart && hoverFrac !== null
      ? nearestPoint(allPts, ts0 + hoverFrac * (ts1 - ts0))
      : null;
  const hoveredFrac = hovered ? (hovered.ts - ts0) / (ts1 - ts0) : 0;
  // The station-series line of the readout: what the hovered point actually
  // measured (observed side), or -- the issue #56 exception -- what the
  // forecast expects to fall, plus its chance (and snow, #65).
  const hoveredExtras = hovered
    ? [
        // >= half a display digit, so a drizzle that rounds to "0.00/hr"
        // (or "0.0 in") stays quiet instead of printing a zero
        hovered.rain_rate_inhr !== null && hovered.rain_rate_inhr >= 0.005
          ? `rain ${hovered.rain_rate_inhr.toFixed(2)}/hr`
          : null,
        hovered.snow_3h_in !== null && hovered.snow_3h_in >= 0.05
          ? `snow ${hovered.snow_3h_in.toFixed(1)} in`
          : null,
        now !== null &&
        hovered.ts > now &&
        hovered.pop !== null &&
        hovered.pop >= 0.05
          ? `chance ${Math.round(hovered.pop * 100)}%`
          : null,
        hovered.pressure_rel_inhg !== null
          ? `baro ${hovered.pressure_rel_inhg.toFixed(2)}`
          : null,
        hovered.solar_wm2 !== null
          ? `solar ${Math.round(hovered.solar_wm2)}`
          : null,
        hovered.uv_index !== null && hovered.uv_index >= 1
          ? `uv ${Math.round(hovered.uv_index)}`
          : null,
      ].filter(Boolean)
    : [];

  const battery = current?.station_battery ?? null;
  const batteryTone =
    battery !== null && battery <= 1
      ? "var(--chipmunk)"
      : battery === 2
        ? "var(--turkey)"
        : "var(--led)";

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Weather station view"
      className="scrollpane fixed inset-0 z-50 overflow-y-auto bg-bg"
    >
      {/* max-w matches the main dashboard's wrapper (issue #60) -- the right
          rail is fixed 320px, so the extra room all goes to the chart. */}
      <div className="mx-auto w-full max-w-[1500px] px-4 pb-10 pt-5 sm:px-6">
        {/* Masthead: the same billing as the panel, room to breathe. */}
        <div className="flex items-center justify-between gap-3 border-b border-line pb-3">
          <h2
            className="text-2xl text-ink"
            style={{ fontFamily: "var(--font-display)" }}
          >
            Weather Post{" "}
            <span className="stamp ml-2 align-middle text-[10px] text-inkfaint">
              station view
            </span>
          </h2>
          <div className="flex items-center gap-4">
            {offline ? (
              <span className="flex items-center gap-1.5 text-xs">
                <span className="inline-block h-2 w-2 rounded-full bg-inkfaint" />
                <span className="stamp text-inkfaint">on coffee break</span>
              </span>
            ) : reporting ? (
              <span className="flex items-center gap-1.5 text-xs">
                <span className="lamp inline-block h-2 w-2 rounded-full bg-led text-led" />
                <span className="stamp text-led">on duty</span>
              </span>
            ) : (
              <span className="flex items-center gap-1.5 text-xs">
                <span className="breathe inline-block h-2 w-2 rounded-full bg-turkey" />
                <span className="stamp text-turkey">no fresh report</span>
              </span>
            )}
            <button
              type="button"
              onClick={onClose}
              autoFocus
              aria-label="Close the station view"
              className="rounded-sm border border-line p-1.5 text-inkdim transition-colors hover:border-linebright hover:text-squirrel"
            >
              <CloseIcon />
            </button>
          </div>
        </div>

        <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
          {/* --- The sky: hero conditions + the big chart ------------------ */}
          <main className="min-w-0">
            <section className="panel rounded-sm border border-line bg-panel px-4 pb-4 pt-3">
              {/* Top-aligned like the panel (issue #54): items-end left a
                  dead void above the temperature once the WxStat grid grew
                  taller than the headline. */}
              <div className="flex flex-wrap items-start gap-x-8 gap-y-4">
                {/* The sky's icon (issue #78) takes the left column; feels-like
                    and the tendency stack beside the number to make the room. */}
                <div className="flex items-center gap-4">
                  <ConditionBadge
                    icon={conditionIcon(current)}
                    size={58}
                    live={reporting}
                  />
                  <div>
                    <div className="flex items-center gap-3">
                      <span
                        className={`text-6xl font-bold tabular-nums ${reporting ? "text-ink" : "text-inkfaint"}`}
                      >
                        {wxRound(current?.temp_f ?? null)}°
                      </span>
                      <span className="flex flex-col text-base leading-snug text-inkdim">
                        <span>feels {wxRound(current?.feels_like_f ?? null)}°</span>
                        {/* the tendency (issue #67) gets its own line under the
                            feels-like; min-h reserves it before the trail is
                            long enough to have an opinion */}
                        <span className="min-h-[24px]">
                          {tempTrend &&
                            `${TREND_GLYPH[tempTrend]} ${tempTrend}`}
                        </span>
                      </span>
                    </div>
                    {/* The sky gets headline billing next to the number, the
                        panel's #54 treatment at station-view scale. */}
                    <div
                      className={`mt-0.5 min-h-[28px] text-lg ${reporting ? "text-ink" : "text-inkfaint"}`}
                    >
                      {current?.description ?? ""}
                    </div>
                    <div className="stamp mt-1 text-[10px] text-inkfaint">
                      sun {clock(current?.sunrise ?? null)} –{" "}
                      {clock(current?.sunset ?? null)}
                      {current !== null && now !== null && (
                        <>
                          {" "}
                          · read{" "}
                          {/* min-w reserves "just now" (8ch at this size and
                              tracking) -- this stamp is the left column's
                              widest line, so the age flipping to "1m ago" and
                              back would walk the whole stat grid sideways
                              (house rule #1) */}
                          <span className="inline-block min-w-[6.3em]">
                            {ageText(current.ts, now)}
                          </span>
                        </>
                      )}
                    </div>
                  </div>
                </div>
                <div className="grid flex-1 grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3">
                  <WxStat
                    label="dew point"
                    value={wxRound(current?.dew_point_f ?? null)}
                    unit="°F"
                    sub={dewTrend && `${TREND_GLYPH[dewTrend]} ${dewTrend}`}
                    info="The temperature the air would have to cool to for
                      its moisture to bead out as dew, figured from the
                      station's temperature and humidity. The closer it runs
                      to the actual temperature, the heavier the air feels —
                      50s is comfortable, 60s is sticky, 70s is a swamp."
                  />
                  <WxStat
                    label="humidity"
                    value={wxRound(current?.humidity_pct ?? null)}
                    unit="%"
                    sub={
                      humidityTrend &&
                      `${TREND_GLYPH[humidityTrend]} ${humidityTrend}`
                    }
                    info="How much water vapor the air is carrying, as a
                      percentage of all it could hold at this temperature —
                      read by the WH90's hygrometer over the driveway. Warm
                      air holds more, so the same percentage sits heavier on
                      a hot afternoon than a cool morning."
                  />
                  <WxStat
                    label="wind"
                    value={wxRound(current?.wind_mph ?? null)}
                    unit="mph"
                    aside={
                      <span className="flex items-center gap-1.5 text-xs text-inkdim">
                        <CompassGlyph deg={current?.wind_deg ?? null} />
                        {/* min-w reserves the widest label ("NW") so a
                            bearing arriving can't nudge the row */}
                        <span className="min-w-[2ch]">
                          {compass(current?.wind_deg ?? null) || "—"}
                        </span>
                      </span>
                    }
                    sub={[
                      current?.wind_gust_mph != null
                        ? `gusts ${Math.round(current.wind_gust_mph)}`
                        : null,
                      current?.wind_max_daily_gust_mph != null
                        ? `max ${Math.round(current.wind_max_daily_gust_mph)}`
                        : null,
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  />
                  <WxStat
                    label="barometer"
                    value={wxFixed(current?.pressure_rel_inhg ?? null, 2)}
                    unit="in"
                    sub={baroTrend && `${TREND_GLYPH[baroTrend]} ${baroTrend}`}
                    info="The weight of the air, in inches of mercury —
                      measured at the gateway and corrected to sea level so
                      it reads like the forecasts do. The arrow matters more
                      than the number: steadily falling usually means weather
                      moving in, rising means it's clearing out."
                  />
                  <WxStat
                    label="uv index"
                    value={wxRound(current?.uv_index ?? null)}
                    sub={
                      current?.solar_wm2 != null &&
                      `solar ${Math.round(current.solar_wm2)} W/m²`
                    }
                    info="The strength of the sun's ultraviolet at the
                      station, on the standard 0–11 scale from the WH90's
                      light sensor. 0–2 is low, 3–5 moderate, 6–7 high, 8 and
                      up means even the turkeys should find shade. The solar
                      wattage below is the same sunshine as raw power."
                  />
                  <WxStat
                    label="rain today"
                    value={wxFixed(current?.rain_day_in ?? null, 2)}
                    unit="in"
                    sub={
                      current?.raining === 1 ? (
                        <span className="flex items-center gap-1.5 text-led">
                          <span className="lamp inline-block h-1.5 w-1.5 rounded-full bg-led text-led" />
                          falling ·{" "}
                          {wxFixed(current?.rain_rate_inhr ?? null, 2)}/hr
                        </span>
                      ) : (
                        "dry"
                      )
                    }
                  />
                </div>
              </div>
              {/* The seldom-read dials, one quiet line: the piezo's ledgers
                  and the agronomist's vapor number. */}
              <div className="mt-3 border-t border-line pt-2 text-[11px] text-inkfaint">
                rain event {wxFixed(current?.rain_event_in ?? null, 2)} in ·
                week {wxFixed(current?.rain_week_in ?? null, 2)} · month{" "}
                {wxFixed(current?.rain_month_in ?? null, 2)} · year{" "}
                {wxFixed(current?.rain_year_in ?? null, 2)} in · vpd{" "}
                {wxFixed(current?.vpd_inhg ?? null, 3)} in
              </div>
            </section>

            {/* --- The trend, four instruments tall ------------------------ */}
            <section className="panel mt-4 rounded-sm border border-line bg-panel px-4 pb-4 pt-3">
              <div className="flex items-center justify-between gap-3 text-[10px] text-inkfaint">
                <span>
                  <span className="text-squirrel">—</span> temp °F ·{" "}
                  <span className="text-inkdim">—</span> wind mph · observed
                  solid, forecast dashed
                </span>
                {/* Home. Always rendered and merely disabled while the window
                    is live, never appearing on pan -- a control that pops into
                    existence would shove this line's legend sideways (house
                    rule #1). The masthead's chrome, the stepper's disabled
                    idiom: no new vocabulary for an old job. */}
                <button
                  type="button"
                  onClick={() => setWindowEnd(null)}
                  disabled={live}
                  aria-label="Return the chart to now"
                  className="stamp shrink-0 rounded-sm border border-line px-2 py-0.5 text-inkdim transition-colors hover:border-linebright hover:text-squirrel disabled:pointer-events-none disabled:opacity-40"
                >
                  now
                </button>
              </div>
              <WxTimeAxis
                days={days}
                nowFrac={nowFrac}
                leftLabel={windowEdgeLabel(ts0 - (now ?? 0))}
                rightLabel={windowEdgeLabel(ts1 - (now ?? 0))}
                className="mt-1.5"
              />
              <div
                className={`relative mt-1 ${hasChart ? "cursor-grab active:cursor-grabbing" : ""}`}
                // pan-y: a horizontal drag is ours, a vertical one still
                // scrolls the overlay underneath (the browser fires
                // pointercancel when it claims the gesture, which ends the
                // drag cleanly).
                style={{ touchAction: "pan-y" }}
                onPointerDown={(e) => {
                  if (!hasChart || now === null) return;
                  e.currentTarget.setPointerCapture(e.pointerId);
                  drag.current = { x: e.clientX, ts1, moved: false };
                  dragging.current = true;
                  setFrozenAxes(frozenAxes ?? liveAxes);
                  // A finger on the glass isn't hovering; the crosshair waits
                  // to see whether this becomes a tap or a drag.
                  if (e.pointerType !== "mouse") setHoverFrac(null);
                }}
                onPointerMove={(e) => {
                  const d = drag.current;
                  if (d) {
                    const r = e.currentTarget.getBoundingClientRect();
                    if (r.width <= 0) return;
                    const dx = e.clientX - d.x;
                    if (!d.moved && Math.abs(dx) > TAP_SLOP_PX) d.moved = true;
                    if (d.moved) {
                      setHoverFrac(null);
                      // Drag right and the chart follows your hand, which
                      // means walking backwards in time.
                      panTo(d.ts1 - (dx / r.width) * STATION_SPAN_S);
                    }
                    return;
                  }
                  // Only a mouse can hover. Touch scrubbing is the tap below.
                  if (e.pointerType === "mouse") setHoverFrac(fracAt(e));
                }}
                onPointerUp={(e) => {
                  const d = drag.current;
                  drag.current = null;
                  dragging.current = false;
                  settleAxes();
                  if (!d) return;
                  // A press that never travelled is a tap: place the
                  // crosshair. A mouse release just restores the hover it had.
                  if (!d.moved || e.pointerType === "mouse")
                    setHoverFrac(fracAt(e));
                }}
                onPointerLeave={() => {
                  if (!drag.current) setHoverFrac(null);
                }}
                onPointerCancel={() => {
                  drag.current = null;
                  dragging.current = false;
                  settleAxes();
                  setHoverFrac(null);
                }}
              >
                {/* main chart: temperature + wind, the panel chart writ tall */}
                <div className="relative h-72 w-full sm:h-96">
                  {hasChart && (
                    <svg
                      viewBox={`0 0 ${WXL_W} ${WXL_H}`}
                      preserveAspectRatio="none"
                      className="h-full w-full"
                      role="img"
                      aria-label="Observed and forecast temperature and wind"
                    >
                      {nights.map((b) => (
                        <rect
                          key={b.start}
                          x={((b.start - ts0) / (ts1 - ts0)) * WXL_W}
                          y={0}
                          width={((b.end - b.start) / (ts1 - ts0)) * WXL_W}
                          height={WXL_H}
                          fill="black"
                          opacity="0.25"
                        />
                      ))}
                      {days.map((t) => (
                        <line
                          key={t.ts}
                          x1={t.frac * WXL_W}
                          y1={0}
                          x2={t.frac * WXL_W}
                          y2={WXL_H}
                          stroke="var(--line)"
                          vectorEffect="non-scaling-stroke"
                        />
                      ))}
                      {nowFrac !== null && (
                        <line
                          x1={nowFrac * WXL_W}
                          y1={0}
                          x2={nowFrac * WXL_W}
                          y2={WXL_H}
                          stroke="var(--line-bright)"
                          strokeDasharray="2 4"
                          vectorEffect="non-scaling-stroke"
                        />
                      )}
                      <path
                        d={linePath(trend.observed, (p) => p.wind_mph, ts0, ts1, 0, windMax, WXL_W, WXL_H)}
                        fill="none"
                        stroke="var(--ink-dim)"
                        strokeWidth="1"
                        opacity="0.8"
                        vectorEffect="non-scaling-stroke"
                      />
                      <path
                        d={linePath(trend.coming, (p) => p.wind_mph, ts0, ts1, 0, windMax, WXL_W, WXL_H)}
                        fill="none"
                        stroke="var(--ink-dim)"
                        strokeWidth="1"
                        strokeDasharray="3 3"
                        opacity="0.5"
                        vectorEffect="non-scaling-stroke"
                      />
                      <path
                        d={linePath(trend.observed, (p) => p.temp_f, ts0, ts1, range.min, range.max, WXL_W, WXL_H)}
                        fill="none"
                        stroke="var(--squirrel)"
                        strokeWidth="1.8"
                        vectorEffect="non-scaling-stroke"
                      />
                      <path
                        d={linePath(trend.coming, (p) => p.temp_f, ts0, ts1, range.min, range.max, WXL_W, WXL_H)}
                        fill="none"
                        stroke="var(--squirrel)"
                        strokeWidth="1.4"
                        strokeDasharray="4 3"
                        opacity="0.65"
                        vectorEffect="non-scaling-stroke"
                      />
                    </svg>
                  )}
                  {hasChart && range !== null && (
                    <>
                      <span className="pointer-events-none absolute left-1 top-0.5 text-[10px] tabular-nums text-squirrel opacity-60">
                        {range.max}°
                      </span>
                      <span className="pointer-events-none absolute bottom-0.5 left-1 text-[10px] tabular-nums text-squirrel opacity-60">
                        {range.min}°
                      </span>
                      <span className="pointer-events-none absolute right-1 top-0.5 text-[10px] tabular-nums text-inkfaint">
                        {windMax} mph
                      </span>
                    </>
                  )}
                  {/* Each forecast peak and valley wears its temperature
                      (issue #113), positioned where it actually happens. HTML
                      overlays for the same reason as the snapped dots below:
                      the viewBox is stretched, so svg text would squash. They
                      read the BLENDED temps (#71 -- trend.coming is already
                      calibrated), or a label would disagree with the line it
                      sits on. Ambient, not the readout: the hover chip stays
                      the detailed answer, so these dim out of its way. */}
                  {hasChart && range !== null &&
                    marks.map((m) => {
                      const frac = (m.ts - ts0) / (ts1 - ts0);
                      if (frac < 0.02 || frac > 0.98) return null;
                      const y =
                        (1 - (m.temp_f - range.min) / (range.max - range.min)) *
                        100;
                      return (
                        <span
                          key={`${m.kind}-${m.ts}`}
                          className={`stamp pointer-events-none absolute -translate-x-1/2 text-[10px] tabular-nums text-squirrel transition-opacity ${
                            hovered ? "opacity-40" : "opacity-80"
                          }`}
                          style={{
                            left: `${frac * 100}%`,
                            top: `${y}%`,
                            // above a peak, below a valley -- the label sits
                            // off the line, never on it
                            marginTop: m.kind === "high" ? "-1.15rem" : "0.3rem",
                          }}
                        >
                          {wxRound(m.temp_f)}°
                        </span>
                      );
                    })}
                  {/* snapped dots, the panel chart's HTML-overlay trick (the
                      viewBox is stretched; svg circles would squash). */}
                  {hovered && range !== null && (
                    <>
                      {hovered.temp_f !== null && (
                        <span
                          className="pointer-events-none absolute h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full border border-squirrel bg-panel"
                          style={{
                            left: `${hoveredFrac * 100}%`,
                            top: `${(1 - (hovered.temp_f - range.min) / (range.max - range.min)) * 100}%`,
                          }}
                        />
                      )}
                      {hovered.wind_mph !== null && (
                        <span
                          className="pointer-events-none absolute h-1.5 w-1.5 -translate-x-1/2 -translate-y-1/2 rounded-full border border-inkdim bg-panel"
                          style={{
                            left: `${hoveredFrac * 100}%`,
                            top: `${(1 - hovered.wind_mph / windMax) * 100}%`,
                          }}
                        />
                      )}
                    </>
                  )}
                </div>

                {/* the station's own instruments, one strip each, sharing
                    the time axis -- rain falls, snow waits its season, the
                    barometer wanders, the sun arcs, and they all line up
                    under the temperature */}
                {hasChart && (
                  <>
                    <WxStrip
                      label="rain · fell in/hr · forecast chance %"
                      scale={`${rainMax}`}
                      ticks={dayFracs}
                      nowFrac={nowFrac}
                    >
                      {/* bar width follows the window: 5-min points sit 0.56
                          viewBox units apart across 144h, so wider bars would
                          stack opacity into false intensity */}
                      {observed
                        .filter(
                          (p) =>
                            p.rain_rate_inhr !== null && p.rain_rate_inhr > 0,
                        )
                        .map((p) => (
                          <rect
                            key={p.ts}
                            x={((p.ts - ts0) / (ts1 - ts0)) * WXL_W - 0.35}
                            y={
                              WXL_STRIP_H -
                              (Math.min(p.rain_rate_inhr!, rainMax) / rainMax) *
                                WXL_STRIP_H
                            }
                            width={0.7}
                            height={
                              (Math.min(p.rain_rate_inhr!, rainMax) / rainMax) *
                              WXL_STRIP_H
                            }
                            style={{
                              fill: precipFill(
                                "var(--rain)",
                                precipShade(
                                  p.rain_rate_inhr!,
                                  rainMax,
                                  RAIN_SHADE_FLOOR,
                                ),
                              ),
                            }}
                          />
                        ))}
                      {/* the forecast half changes units at the now line
                          (issue #65): ghost bars are the CHANCE of
                          precipitation, full strip = 100% -- the forecast's
                          volumes read as slivers on the piezo's in/hr scale,
                          but a 20% Thursday deserves a 20%-tall bar. Volumes
                          stay one hover away in the chip. Each bar spans the
                          3h window ending at its point, clipped at the now
                          line (the observed trail owns the past). */}
                      {now !== null &&
                        trend.coming
                          .filter(
                            (p) => p.ts > now && p.pop !== null && p.pop > 0,
                          )
                          .map((p) => {
                            const t0 = Math.max(
                              p.ts - WX_FORECAST_STEP_S,
                              now,
                            );
                            const w =
                              ((p.ts - t0) / (ts1 - ts0)) * WXL_W - 1.4;
                            if (w <= 0) return null;
                            const h = Math.min(p.pop!, 1) * WXL_STRIP_H;
                            return (
                              <rect
                                key={p.ts}
                                x={((t0 - ts0) / (ts1 - ts0)) * WXL_W + 0.7}
                                y={WXL_STRIP_H - h}
                                width={w}
                                height={h}
                                // Shade = the chance, same as the height (#113):
                                // a 90% Friday is a tall VIVID slab, a 20%
                                // Tuesday a short ghost. Ceilinged below the
                                // observed trail's full voice -- a forecast,
                                // however certain, never shouts as loud as a
                                // measurement.
                                style={{
                                  fill: precipFill(
                                    "var(--rain)",
                                    precipShade(
                                      p.pop!,
                                      1,
                                      RAIN_SHADE_FLOOR,
                                      FORECAST_SHADE_CEIL,
                                    ),
                                  ),
                                }}
                              />
                            );
                          })}
                    </WxStrip>
                    {/* snow, forecast-only (issue #65) and seasonal (issue
                        #69): the piezo cannot see snow, so the observed half
                        is honestly blank forever, and April through October
                        the whole row stands down -- unless the forecast
                        actually carries snow, which overrides the calendar
                        in any month. Snow-white ink, never the rain's blue.
                        Its ramp reaches FULL white (issue #113) where rain's
                        forecast is ceilinged: rain's ghosts share a strip with
                        a measured trail they mustn't out-shout, and snow's
                        observed half is blank forever, so there is nothing
                        here to shout over -- heavy snow gets the whole voice. */}
                    {showSnow && (
                    <WxStrip
                      label="snow · forecast in per 3h"
                      scale={`${snowMax}`}
                      ticks={dayFracs}
                      nowFrac={nowFrac}
                    >
                      {now !== null &&
                        trend.coming
                          .filter(
                            (p) =>
                              p.ts > now &&
                              p.snow_3h_in !== null &&
                              p.snow_3h_in > 0,
                          )
                          .map((p) => {
                            const t0 = Math.max(
                              p.ts - WX_FORECAST_STEP_S,
                              now,
                            );
                            const w =
                              ((p.ts - t0) / (ts1 - ts0)) * WXL_W - 1.4;
                            if (w <= 0) return null;
                            const h =
                              (Math.min(p.snow_3h_in!, snowMax) / snowMax) *
                              WXL_STRIP_H;
                            return (
                              <rect
                                key={p.ts}
                                x={((t0 - ts0) / (ts1 - ts0)) * WXL_W + 0.7}
                                y={WXL_STRIP_H - h}
                                width={w}
                                height={h}
                                style={{
                                  fill: precipFill(
                                    "var(--ink)",
                                    precipShade(
                                      p.snow_3h_in!,
                                      snowMax,
                                      SNOW_SHADE_FLOOR,
                                    ),
                                  ),
                                }}
                              />
                            );
                          })}
                    </WxStrip>
                    )}
                    <WxStrip
                      label="barometer · in"
                      scale={
                        presRange ? `${presRange.max}–${presRange.min}` : ""
                      }
                      ticks={dayFracs}
                      nowFrac={nowFrac}
                    >
                      {presRange && (
                        <path
                          d={linePath(observed, (p) => p.pressure_rel_inhg, ts0, ts1, presRange.min, presRange.max, WXL_W, WXL_STRIP_H)}
                          fill="none"
                          stroke="var(--ink)"
                          strokeWidth="1.2"
                          opacity="0.55"
                          vectorEffect="non-scaling-stroke"
                        />
                      )}
                    </WxStrip>
                    <WxStrip
                      label="solar w/m² · uv dashed"
                      scale={`${solarMax}`}
                      ticks={dayFracs}
                      nowFrac={nowFrac}
                    >
                      <path
                        d={linePath(observed, (p) => p.solar_wm2, ts0, ts1, 0, solarMax, WXL_W, WXL_STRIP_H)}
                        fill="none"
                        stroke="var(--turkey)"
                        strokeWidth="1.2"
                        opacity="0.8"
                        vectorEffect="non-scaling-stroke"
                      />
                      <path
                        d={linePath(observed, (p) => p.uv_index, ts0, ts1, 0, uvMax, WXL_W, WXL_STRIP_H)}
                        fill="none"
                        stroke="var(--turkey)"
                        strokeWidth="1"
                        strokeDasharray="2 3"
                        opacity="0.5"
                        vectorEffect="non-scaling-stroke"
                      />
                    </WxStrip>
                  </>
                )}

                {/* one crosshair through the whole instrument stack */}
                {hovered && (
                  <span
                    className="pointer-events-none absolute inset-y-0 w-px bg-inkdim/40"
                    style={{ left: `${hoveredFrac * 100}%` }}
                  />
                )}
                {hovered && now !== null && (
                  <div
                    className="pointer-events-none absolute top-1 z-10 whitespace-nowrap rounded-sm border border-linebright bg-panel2 px-2 py-1"
                    style={
                      hoveredFrac < 0.5
                        ? { left: `calc(${hoveredFrac * 100}% + 10px)` }
                        : { right: `calc(${(1 - hoveredFrac) * 100}% + 10px)` }
                    }
                  >
                    <div className="stamp text-[10px] text-inkfaint">
                      {dayClock(hovered.ts)} ·{" "}
                      {hovered.ts <= now ? "observed" : "forecast"}
                    </div>
                    <div className="text-[11px] tabular-nums text-ink">
                      {wxRound(hovered.temp_f)}°
                      {hovered.condition && (
                        <span className="text-inkdim">
                          {" "}
                          · {hovered.condition.toLowerCase()}
                        </span>
                      )}
                    </div>
                    <div className="text-[10px] tabular-nums text-inkdim">
                      wind {wxRound(hovered.wind_mph)} mph
                      {hovered.wind_gust_mph !== null &&
                        ` · gusts ${Math.round(hovered.wind_gust_mph)}`}
                    </div>
                    {hoveredExtras.length > 0 && (
                      <div className="text-[10px] tabular-nums text-inkdim">
                        {hoveredExtras.join(" · ")}
                      </div>
                    )}
                  </div>
                )}
              </div>
              <WxTimeAxis
                days={days}
                nowFrac={nowFrac}
                leftLabel={windowEdgeLabel(ts0 - (now ?? 0))}
                rightLabel={windowEdgeLabel(ts1 - (now ?? 0))}
                className="mt-0.5"
              />
            </section>
          </main>

          {/* --- The desk and the hardware: Willard + the station itself --- */}
          <aside className="flex min-w-0 flex-col gap-4">
            <section className="panel rounded-sm border border-line bg-panel">
              <div className="flex items-baseline gap-2 px-4 pt-3 text-[11px]">
                <span className="stamp text-inkdim">willard, on the air</span>
                {onAir && report !== null && (
                  <span className="text-inkfaint">{clock(report.ts)}</span>
                )}
              </div>
              {onAir && report !== null ? (
                <p
                  className="px-4 pb-4 pt-2 text-[17px] leading-snug text-ink"
                  style={{
                    fontFamily: "var(--font-display)",
                    whiteSpace: "pre-line",
                  }}
                >
                  {report.text}
                </p>
              ) : (
                <p className="px-4 pb-4 pt-2 text-sm leading-relaxed text-inkfaint">
                  willard is between broadcasts — the forecast desk is quiet
                </p>
              )}
            </section>

            <section className="panel rounded-sm border border-line bg-panel">
              <div className="px-4 pt-3">
                <span className="stamp text-[11px] text-inkdim">
                  the station itself
                </span>
              </div>
              <dl className="flex flex-col gap-2.5 px-4 pb-4 pt-3 text-[12px]">
                <div className="flex items-baseline justify-between gap-3">
                  <dt className="stamp text-[10px] text-inkfaint">
                    indoors · at the gateway
                  </dt>
                  <dd className="tabular-nums text-ink">
                    {wxRound(current?.indoor_temp_f ?? null)}° ·{" "}
                    {wxRound(current?.indoor_humidity_pct ?? null)}% rh
                  </dd>
                </div>
                <div className="flex items-baseline justify-between gap-3">
                  <dt className="stamp text-[10px] text-inkfaint">
                    barometer · absolute
                  </dt>
                  <dd className="tabular-nums text-inkdim">
                    {wxFixed(current?.pressure_abs_inhg ?? null, 2)} in
                  </dd>
                </div>
                <div className="flex items-center justify-between gap-3 border-t border-line pt-2.5">
                  <dt className="stamp text-[10px] text-inkfaint">
                    wh90 battery
                  </dt>
                  {/* meter last so its right edge lands flush with the radio
                      signal's below it -- the two green meters read as one
                      column (issue #103) */}
                  <dd className="flex items-center gap-2 tabular-nums text-inkdim">
                    {current?.station_voltage != null &&
                      `${current.station_voltage.toFixed(2)}v`}
                    <SegMeter n={battery} of={5} tone={batteryTone} />
                  </dd>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <dt className="stamp text-[10px] text-inkfaint">
                    radio signal
                  </dt>
                  <dd className="flex items-center gap-2 text-inkdim">
                    <SegMeter n={current?.station_signal ?? null} of={4} />
                  </dd>
                </div>
                <div className="flex items-baseline justify-between gap-3 border-t border-line pt-2.5">
                  <dt className="stamp text-[10px] text-inkfaint">
                    last report
                  </dt>
                  <dd className="tabular-nums text-inkdim">
                    {current !== null && now !== null
                      ? `${clock(current.ts)} · ${ageText(current.ts, now)}`
                      : "—"}
                  </dd>
                </div>
              </dl>
            </section>
          </aside>
        </div>
      </div>
    </div>
  );
}
