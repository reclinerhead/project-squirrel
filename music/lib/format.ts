// --- Pure duration/label helpers (unit-tested in format.test.ts) ---

/** 227 -> "3:47". Hours only when needed: 4245 -> "1:10:45". */
export function formatDuration(totalS: number): string {
  const s = Math.max(0, Math.floor(totalS));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = h > 0 ? String(m).padStart(2, "0") : String(m);
  return `${h > 0 ? `${h}:` : ""}${mm}:${String(sec).padStart(2, "0")}`;
}

/** Album-length prose: 2729 -> "45 min", 5520 -> "1 hr 32 min". */
export function formatTotalDuration(totalS: number): string {
  const totalMin = Math.round(Math.max(0, totalS) / 60);
  if (totalMin < 60) return `${totalMin} min`;
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return m === 0 ? `${h} hr` : `${h} hr ${m} min`;
}

/** 48000 -> "48", 44100 -> "44.1" -- badge-facing kHz text. */
export function formatKhz(hz: number): string {
  const khz = hz / 1000;
  return Number.isInteger(khz) ? String(khz) : String(Math.round(khz * 10) / 10);
}
