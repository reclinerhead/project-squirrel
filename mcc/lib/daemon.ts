// Client for the Merle daemon. All requests go through the /daemon/* proxy
// route (app/daemon/[...path]/route.ts), so the browser stays same-origin and
// the daemon needs no CORS config.

export type Track = {
  track_id: number;
  species: string;
  conf: number;
  box: number[];
  coasting: boolean;
};

export type MerleEvent = {
  ts: string;
  kind: string;
  details: Record<string, unknown> | null;
};

export type DaemonState = {
  session_id: string;
  running: boolean;
  recording: boolean;
  crowd_threshold: number;
  /** The model's class roster, in class-id order. Optional so a dashboard
   * newer than its daemon degrades to present-species-only rows. */
  species?: string[];
  /** Which feed the daemon's eyes are on, and what it could switch to
   * (issue #236). Optional/null/[] on older daemons and in the synthetic
   * world -- the dashboard's cue to render no source toggle at all. */
  source?: string | null;
  sources?: string[];
  live: {
    counts: Record<string, number>;
    tracks: Track[];
    fps: number;
    signal: boolean;
  };
  totals: Record<string, number>;
  recent_events: MerleEvent[];
};

export const STREAM_URL = "/daemon/stream";
export const SNAPSHOT_URL = "/daemon/snapshot";

export async function fetchState(): Promise<DaemonState> {
  const res = await fetch("/daemon/state", { cache: "no-store" });
  if (!res.ok) throw new Error(`daemon /state -> ${res.status}`);
  return res.json();
}

export async function sendControl(
  action: string,
  value?: number,
): Promise<void> {
  const res = await fetch("/daemon/control", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value === undefined ? { action } : { action, value }),
  });
  if (!res.ok) throw new Error(`daemon /control -> ${res.status}`);
}

/** Point the daemon's eyes at another advertised source (issue #236). The
 * daemon swaps between perception loop passes; /state reports the new source
 * once the swap lands (or keeps the old one if the target wouldn't open). */
export async function setSource(source: string): Promise<void> {
  const res = await fetch("/daemon/control", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "set_source", source }),
  });
  if (!res.ok) throw new Error(`daemon /control -> ${res.status}`);
}

// --- Pure formatting helpers (unit-tested in daemon.test.ts) ----------------

/** "2026-07-05T14:31:02" -> "14:31:02" (already local time -- the daemon stamps
 * with the same clock as this machine; it all runs on one box). */
export function eventClock(ts: string): string {
  const t = ts.split("T")[1];
  return t ? t.slice(0, 8) : ts;
}

/** Visit lengths for the event log: seconds under 2 minutes, then minutes,
 * then hours -- species-level visits can run long. */
export function visitLength(secs: number): string {
  if (secs < 120) return `${Math.round(secs)}s`;
  if (secs < 5400) return `${Math.round(secs / 60)}m`;
  return `${(secs / 3600).toFixed(1)}h`;
}

/** One event-log line per event kind. Unknown kinds fall back to the raw
 * kind so new daemon event types show up without a frontend change.
 * arrival/departure are SPECIES-level (count rides in details); duration only
 * exists when the last one left. */
export function eventLine(e: MerleEvent): string {
  if (e.kind === "arrival") {
    const species = (e.details?.species as string) ?? "critter";
    const count = (e.details?.count as number) ?? 1;
    return count > 1 ? `${species} arrived (${count} now)` : `${species} arrived`;
  }
  if (e.kind === "departure") {
    const species = (e.details?.species as string) ?? "critter";
    const count = (e.details?.count as number) ?? 0;
    if (count > 0) return `${species} left (${count} still here)`;
    const secs = e.details?.duration_s as number | undefined;
    return secs !== undefined
      ? `${species} left after ${visitLength(secs)}`
      : `${species} left`;
  }
  if (e.kind === "crowd_snapshot") {
    const total = (e.details?.total as number) ?? "?";
    const counts = (e.details?.counts as Record<string, number>) ?? {};
    const mix = Object.entries(counts)
      .sort()
      .map(([name, n]) => `${n} ${name}`)
      .join(", ");
    return mix ? `crowd of ${total} — ${mix}` : `crowd of ${total}`;
  }
  if (e.kind === "hard_frame_saved") {
    const boxes = e.details?.boxes as number | undefined;
    return boxes !== undefined
      ? `hard frame banked (${boxes} boxes pre-labeled)`
      : "hard frame banked";
  }
  return e.kind.replaceAll("_", " ");
}

/** Species sorted for display: most-counted first, ties alphabetical. */
export function sortedCounts(
  counts: Record<string, number>,
): [string, number][] {
  return Object.entries(counts).sort(
    ([an, ac], [bn, bc]) => bc - ac || an.localeCompare(bn),
  );
}

/** Fixed display slots for the rail panels: one entry per roster species in
 * roster order, zero-filled, so rows never appear/disappear/reorder and the
 * panels keep a stable height (issue #16). Species counted but missing from
 * the roster (older daemon, future classes) are appended alphabetically --
 * nothing is ever hidden. */
export function rosterCounts(
  roster: string[],
  counts: Record<string, number>,
): [string, number][] {
  const extras = Object.keys(counts)
    .filter((name) => !roster.includes(name))
    .sort();
  return [...roster, ...extras].map((name) => [name, counts[name] ?? 0]);
}
