// Client for the Merle daemon. All requests go through the /daemon/* rewrite
// (see next.config.ts), so the browser stays same-origin and the daemon needs
// no CORS config.

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
  live: { counts: Record<string, number>; tracks: Track[]; fps: number };
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

// --- Pure formatting helpers (unit-tested in daemon.test.ts) ----------------

/** "2026-07-05T14:31:02" -> "14:31:02" (already local time -- the daemon stamps
 * with the same clock as this machine; it all runs on one box). */
export function eventClock(ts: string): string {
  const t = ts.split("T")[1];
  return t ? t.slice(0, 8) : ts;
}

/** One field-journal line per event kind. Unknown kinds fall back to the raw
 * kind so new daemon event types show up without a frontend change. */
export function eventLine(e: MerleEvent): string {
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
