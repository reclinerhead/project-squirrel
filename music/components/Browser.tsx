"use client";

// Infinite-scroll list for the browse pages (issue #118). The server renders
// the first window and hands it here; this component appends each next window
// as the sentinel comes into view, asking /api/{kind} for "the next N after
// what I have".
//
// Three things that are load-bearing rather than decorative:
//
//  - **The client never holds the catalog.** It holds what's been scrolled to.
//    That's the whole reason for the round trip -- at 27k tracks, slicing a
//    client-side copy is the failure this design exists to avoid.
//  - **`nextOffset === null` is the only end signal.** Inferring "done" from a
//    short page breaks the moment the catalog's last window is exactly full.
//  - **A real button, not just a sentinel.** Auto-load-on-scroll is invisible
//    to keyboard users and to anyone whose scroll never reaches the sentinel;
//    the button is the honest control and the observer is the convenience.
//    Both call the same loader, and it is re-entrancy guarded -- the observer
//    fires more than once per intersection.
//
// The page keys this component on the active filter/sort, so changing either
// remounts it with fresh server data instead of appending onto a stale list.

import { useCallback, useEffect, useRef, useState } from "react";
import { AlbumCard, ArtistCard } from "./cards";
import type { Album, Artist } from "@/lib/types";

type Kind = "albums" | "artists";

export function Browser({
  kind,
  initialItems,
  initialNextOffset,
  startOffset,
  total,
  query,
}: {
  kind: Kind;
  initialItems: (Album | Artist)[];
  initialNextOffset: number | null;
  /** Where the server's first window began. Non-zero means a letter jump, so
   * reaching the end is "end of the list", NOT "you've seen all N" -- the
   * entries before the jump were never loaded and the footer must not claim
   * otherwise. */
  startOffset: number;
  total: number;
  /** genre/sort, forwarded verbatim to the feed so the window matches what
   * the server rendered. */
  query: { genre?: string; sort: string };
}) {
  const [items, setItems] = useState(initialItems);
  const [nextOffset, setNextOffset] = useState(initialNextOffset);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const busy = useRef(false);
  const sentinel = useRef<HTMLDivElement>(null);

  const loadMore = useCallback(async () => {
    if (busy.current || nextOffset === null) return;
    busy.current = true;
    setLoading(true);
    setFailed(false);
    try {
      const p = new URLSearchParams({ sort: query.sort, offset: String(nextOffset) });
      if (query.genre) p.set("genre", query.genre);
      const res = await fetch(`/api/${kind}?${p}`);
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      setItems((prev) => [...prev, ...data.items]);
      setNextOffset(data.nextOffset);
    } catch {
      // Degrade, never break: the list keeps what it has and the button
      // stays clickable. Nothing here is worth a thrown boundary.
      setFailed(true);
    } finally {
      setLoading(false);
      busy.current = false;
    }
  }, [kind, nextOffset, query.genre, query.sort]);

  useEffect(() => {
    const el = sentinel.current;
    if (!el || nextOffset === null) return;
    // rootMargin: start fetching a screen early so the grid is usually
    // already filled by the time the bottom arrives.
    const io = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting) loadMore();
      },
      { rootMargin: "600px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [loadMore, nextOffset]);

  const grid =
    kind === "albums"
      ? "grid grid-cols-2 gap-4 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5"
      : "grid grid-cols-3 gap-4 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6";

  return (
    <div className="space-y-4">
      {items.length === 0 ? (
        <section className="panel rounded-sm border border-line bg-panel px-4 py-6 text-sm text-inkdim">
          {kind === "albums" ? "Nothing on this shelf." : "Nobody on this shelf."}
        </section>
      ) : (
        <div className={grid}>
          {kind === "albums"
            ? (items as Album[]).map((al) => <AlbumCard key={al.id} album={al} />)
            : (items as Artist[]).map((a) => <ArtistCard key={a.id} artist={a} />)}
        </div>
      )}

      {/* The status line holds its box whether or not there's more to get,
          so the grid never jumps as windows land. */}
      <div ref={sentinel} className="flex h-12 items-center justify-center gap-3">
        {nextOffset !== null ? (
          <>
            <button
              type="button"
              onClick={loadMore}
              disabled={loading}
              className="stamp rounded-sm border border-line px-4 py-1.5 text-[10px] text-inkdim transition-colors hover:border-linebright hover:text-ink disabled:opacity-50"
            >
              {loading ? "Loading…" : failed ? "Retry" : "Load more"}
            </button>
            <span className="stamp text-[10px] text-inkfaint">
              {items.length.toLocaleString()} loaded · {total.toLocaleString()} {kind}
            </span>
          </>
        ) : (
          <span className="stamp text-[10px] text-inkfaint">
            {startOffset > 0
              ? `end of the list · ${total.toLocaleString()} ${kind} in all`
              : `${total.toLocaleString()} ${kind} · that’s all of them`}
          </span>
        )}
      </div>
    </div>
  );
}
