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
              // eslint-disable-next-line @next/next/no-img-element
              <img
                key={streamKey}
                src={STREAM_URL}
                alt="Live annotated driveway feed"
                className="block aspect-video w-full bg-black object-contain"
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
                tone={state?.running ? "dim" : "go"}
              >
                {state?.running ? "◼ stand down" : "▶ resume watch"}
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
