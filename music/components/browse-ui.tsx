// Shared browse-page chrome (issue #118): genre pills, sort toggles, and the
// A-Z rail. All <Link>s -- genre, sort, and letter live in the URL, so
// back/forward and sharing work with no client state, and each is a fresh
// server render rather than a client refetch.
//
// The infinite list below them is client-side (Browser.tsx); these controls
// deliberately are not. Changing a filter should start a new list, not append
// to the old one.

import Link from "next/link";
import type { BrowseSort, RailEntry } from "@/lib/api";

export function buildQuery(params: Record<string, string | number | undefined>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "") q.set(k, String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}

export function GenrePills({
  base,
  genres,
  active,
  sort,
}: {
  base: string;
  genres: string[];
  active?: string;
  sort: BrowseSort;
}) {
  const pill = (on: boolean) =>
    `stamp shrink-0 whitespace-nowrap rounded-full border px-4 py-1.5 text-[10px] transition-colors ${
      on ? "border-linebright bg-panel2 text-ink" : "border-line text-inkdim hover:border-linebright hover:text-ink"
    }`;
  return (
    <nav aria-label="Genre filter" className="scrollpane -mx-1 flex gap-2 overflow-x-auto px-1 pb-1">
      <Link href={`${base}${buildQuery({ sort })}`} className={pill(!active)}>
        All
      </Link>
      {genres.map((g) => (
        <Link key={g} href={`${base}${buildQuery({ genre: g, sort })}`} className={pill(active === g)}>
          {g}
        </Link>
      ))}
    </nav>
  );
}

export function SortToggle({
  base,
  sort,
  extra,
}: {
  base: string;
  sort: BrowseSort;
  extra?: Record<string, string | undefined>;
}) {
  const opts: { key: BrowseSort; label: string }[] = [
    { key: "az", label: "A–Z" },
    { key: "new", label: "Newest" },
  ];
  return (
    <span className="flex items-center gap-1" role="group" aria-label="Sort">
      {opts.map((o) => (
        <Link
          key={o.key}
          href={`${base}${buildQuery({ ...extra, sort: o.key })}`}
          aria-current={sort === o.key ? "true" : undefined}
          className={`stamp rounded-full border px-3 py-1 text-[10px] transition-colors ${
            sort === o.key
              ? "border-linebright bg-panel2 text-ink"
              : "border-line text-inkfaint hover:text-inkdim"
          }`}
        >
          {o.label}
        </Link>
      ))}
    </span>
  );
}

/** A letter jump sets where the window STARTS (?letter=S), not which page it
 * lands on -- with infinite scroll there are no pages, and an offset in the
 * URL would rot the moment the catalog changes. The letter still means the
 * right thing after a re-index. */
export function LetterRail({
  base,
  rail,
  active,
  extra,
}: {
  base: string;
  rail: RailEntry[];
  active?: string;
  extra?: Record<string, string | undefined>;
}) {
  if (rail.length === 0) return null;
  return (
    <nav aria-label="Jump to letter" className="scrollpane -mx-1 flex gap-1 overflow-x-auto px-1 py-1">
      {rail.map((r) => (
        <Link
          key={r.letter}
          href={`${base}${buildQuery({ ...extra, sort: "az", letter: r.letter })}`}
          aria-current={active === r.letter ? "true" : undefined}
          className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-sm text-xs tabular-nums transition-colors hover:bg-panel2 hover:text-ink ${
            active === r.letter ? "bg-panel2 text-ink" : "text-inkfaint"
          }`}
        >
          {r.letter}
        </Link>
      ))}
    </nav>
  );
}
