// Client for the daemon's /history endpoints (Station Records panels: daily
// census, hard-frame harvest, training rounds). Same proxy-route path as
// lib/daemon.ts. Fetched on demand + slow refresh -- never the 1s /state loop.

export type DayCensus = { date: string; counts: Record<string, number> };
export type HardFrameDay = { date: string; n: number };
export type TrainingRun = {
  run_name: string;
  map50: number | null;
  recall: number | null;
  map50_95: number | null;
  val_split: string | null;
  notes: string | null;
  metrics: Record<string, Record<string, number>> | null;
};
export type History = {
  census: DayCensus[];
  hard_frames: HardFrameDay[];
  training_runs: TrainingRun[];
};
export type DayHours = {
  date: string;
  hours: Record<string, Record<string, number>>;
};

export async function fetchHistory(days = 14): Promise<History> {
  const res = await fetch(`/daemon/history?days=${days}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`daemon /history -> ${res.status}`);
  return res.json();
}

export async function fetchDayHours(day: string): Promise<DayHours> {
  const res = await fetch(`/daemon/history/day?day=${day}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`daemon /history/day -> ${res.status}`);
  return res.json();
}

// --- Pure chart shaping (unit-tested in history.test.ts) ---------------------

/** Stack order for the census bars. Squirrel (the dominant class) anchors the
 * baseline; the order also keeps the palette's adjacent pairs CVD-separable
 * (squirrel|turkey and turkey|chipmunk both clear the validator; orange|red
 * never touch). Unknown species append after, in name order. */
export const SPECIES_ORDER = ["squirrel", "turkey", "chipmunk"];

export function speciesOrder(names: string[]): string[] {
  const known = SPECIES_ORDER.filter((s) => names.includes(s));
  const extra = names.filter((s) => !SPECIES_ORDER.includes(s)).sort();
  return [...known, ...extra];
}

/** Every species that appears anywhere in the window, in stack order --
 * drives both the legend and the segment order. */
export function speciesInWindow(census: DayCensus[]): string[] {
  const seen = new Set<string>();
  for (const d of census) for (const s of Object.keys(d.counts)) seen.add(s);
  return speciesOrder([...seen]);
}

export type Segment = { species: string; n: number };

/** One day's stacked segments, baseline-first, zero-count species omitted. */
export function stackDay(counts: Record<string, number>): Segment[] {
  return speciesOrder(Object.keys(counts))
    .map((species) => ({ species, n: counts[species] ?? 0 }))
    .filter((s) => s.n > 0);
}

export function dayTotal(counts: Record<string, number>): number {
  return Object.values(counts).reduce((a, b) => a + b, 0);
}

/** The tallest day in the window -- the shared y-scale for every bar. At least
 * 1 so an all-quiet window doesn't divide by zero. */
export function censusPeak(census: DayCensus[]): number {
  return Math.max(1, ...census.map((d) => dayTotal(d.counts)));
}

/** "2026-07-06" -> "Jul 6". Hand-rolled (no toLocaleDateString): identical on
 * server and client, so hydration never disagrees about a label. */
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export function dayLabel(date: string): string {
  const [, m, d] = date.split("-").map(Number);
  return `${MONTHS[(m ?? 1) - 1]} ${d ?? "?"}`;
}

/** Peak hourly total for a selected day's strip (>=1, same reason as above). */
export function hoursPeak(hours: DayHours["hours"]): number {
  return Math.max(1, ...Object.values(hours).map((h) => dayTotal(h)));
}

/** Training runs newest-first (train-18 before train-15) -- the table reads as
 * lineage, not leaderboard. Numeric suffix wins; anything unparseable sorts
 * last, alphabetically. */
export function runsNewestFirst(runs: TrainingRun[]): TrainingRun[] {
  const num = (name: string) => {
    const m = /(\d+)\s*$/.exec(name);
    return m ? Number(m[1]) : -1;
  };
  return [...runs].sort(
    (a, b) => num(b.run_name) - num(a.run_name) ||
      a.run_name.localeCompare(b.run_name),
  );
}
