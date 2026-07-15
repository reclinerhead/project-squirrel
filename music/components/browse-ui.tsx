// Shared browse-page chrome (issue #118): sort toggles, the A-Z rail, and
// the pager. Everything is a <Link> -- sort, letter, and page state live in
// the URL, so back/forward and sharing behave with zero client state. The
// rail renders only letters that exist and only under A-Z sort (a letter
// jump is meaningless on a newest-first list).

import Link from "next/link";
import type { Page } from "@/lib/browse";
import type { BrowseSort } from "@/lib/api";

export function buildQuery(params: Record<string, string | number | undefined>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "") q.set(k, String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
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

export function LetterRail({
  base,
  letters,
  pageByLetter,
  extra,
}: {
  base: string;
  letters: string[];
  pageByLetter: Record<string, number>;
  extra?: Record<string, string | undefined>;
}) {
  return (
    <nav
      aria-label="Jump to letter"
      className="scrollpane -mx-1 flex gap-1 overflow-x-auto px-1 py-1"
    >
      {letters.map((l) => (
        <Link
          key={l}
          href={`${base}${buildQuery({ ...extra, sort: "az", page: pageByLetter[l] })}`}
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-sm text-xs tabular-nums text-inkfaint transition-colors hover:bg-panel2 hover:text-ink"
        >
          {l}
        </Link>
      ))}
    </nav>
  );
}

export function Pager({
  base,
  pageInfo,
  total,
  what,
  extra,
}: {
  base: string;
  pageInfo: Page;
  total: number;
  what: string;
  extra?: Record<string, string | undefined>;
}) {
  const { page, pages } = pageInfo;
  const link = (p: number, label: string, ok: boolean) =>
    ok ? (
      <Link
        href={`${base}${buildQuery({ ...extra, page: p })}`}
        className="stamp rounded-sm border border-line px-3 py-1 text-[10px] text-inkdim transition-colors hover:border-linebright hover:text-ink"
      >
        {label}
      </Link>
    ) : (
      <span className="stamp rounded-sm border border-line px-3 py-1 text-[10px] text-inkfaint opacity-40">
        {label}
      </span>
    );

  return (
    <div className="flex items-center justify-between gap-3 pt-2">
      <span className="stamp text-[10px] text-inkfaint">
        {total.toLocaleString()} {what} · page {page} of {pages}
      </span>
      <span className="flex items-center gap-2">
        {link(page - 1, "‹ Prev", page > 1)}
        {link(page + 1, "Next ›", page < pages)}
      </span>
    </div>
  );
}
