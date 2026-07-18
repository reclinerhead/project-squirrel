"use client";

// Top chrome (issue #116): back/forward, the masthead, and the everything-
// search. Results render in an overlay panel anchored under the input --
// absolutely positioned, so a query appearing or vanishing never moves the
// page underneath (rule #1).
//
// Search became a debounced fetch of /api/search (issue #129) -- lib/api.ts
// is server-only now. Stale responses are dropped by sequence number rather
// than AbortController: results for "capi" arriving after "capital"'s must
// not win, and a counter is the whole of that logic.

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { SearchResults } from "@/lib/search";
import type { Album } from "@/lib/types";
import { formatDuration } from "@/lib/format";
import { usePlayer } from "./PlayerProvider";
import { CoverArt } from "./CoverArt";
import { ArrowLeftIcon, ArrowRightIcon, SearchIcon } from "./icons";

const NO_RESULTS: SearchResults = { artists: [], albums: [], tracks: [] };
const DEBOUNCE_MS = 150;

function GroupLabel({ children }: { children: React.ReactNode }) {
  return <div className="stamp px-4 pb-1 pt-3 text-[10px] text-inkfaint">{children}</div>;
}

export function Chrome() {
  const router = useRouter();
  const { playTracks } = usePlayer();
  const [q, setQ] = useState("");
  const [focused, setFocused] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const [results, setResults] = useState<SearchResults>(NO_RESULTS);
  const seqRef = useRef(0);

  useEffect(() => {
    const query = q.trim();
    if (query.length < 2) {
      setResults(NO_RESULTS);
      return;
    }
    const seq = ++seqRef.current;
    const id = setTimeout(async () => {
      try {
        const res = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
        if (!res.ok) return;
        const data = (await res.json()) as SearchResults;
        if (seq === seqRef.current) setResults(data); // stale answers lose
      } catch {
        // a failed search leaves the last results standing; typing continues
      }
    }, DEBOUNCE_MS);
    return () => clearTimeout(id);
  }, [q]);

  const hasResults = results.artists.length + results.albums.length + results.tracks.length > 0;
  const open = focused && q.trim().length >= 2;

  const close = () => {
    setQ("");
    inputRef.current?.blur();
    setFocused(false);
  };

  return (
    <header className="sticky top-0 z-20 border-b border-line bg-bg/85 backdrop-blur">
      <div className="mx-auto flex max-w-6xl items-center gap-3 px-4 py-3">
        <div className="flex items-center gap-1">
          <button
            type="button"
            aria-label="Back"
            onClick={() => router.back()}
            className="rounded-sm border border-line bg-panel p-1.5 text-inkdim transition-colors hover:text-ink"
          >
            <ArrowLeftIcon className="h-4 w-4" />
          </button>
          <button
            type="button"
            aria-label="Forward"
            onClick={() => router.forward()}
            className="rounded-sm border border-line bg-panel p-1.5 text-inkdim transition-colors hover:text-ink"
          >
            <ArrowRightIcon className="h-4 w-4" />
          </button>
        </div>

        <Link href="/" className="flex min-w-0 items-baseline gap-2">
          <span className="truncate text-xl text-ink" style={{ fontFamily: "var(--font-display)" }}>
            Music
          </span>
          <span className="stamp hidden rounded-full border border-line px-2 py-0.5 text-[9px] text-inkfaint sm:inline">
            the stacks, live
          </span>
        </Link>

        <div className="relative ml-auto w-full max-w-[420px]">
          <div className="flex items-center gap-2 rounded-sm border border-line bg-panel px-3 py-1.5 transition-colors focus-within:border-linebright">
            <SearchIcon className="h-4 w-4 shrink-0 text-inkfaint" />
            <input
              ref={inputRef}
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onFocus={() => setFocused(true)}
              onKeyDown={(e) => e.key === "Escape" && close()}
              placeholder="Search the library"
              aria-label="Search the library"
              className="w-full bg-transparent text-sm text-ink placeholder:text-inkfaint focus:outline-none"
            />
          </div>

          {open && (
            <>
              <button
                type="button"
                aria-label="Close search"
                className="fixed inset-0 z-30 cursor-default"
                onClick={close}
              />
              <div className="scrollpane absolute left-0 right-0 top-full z-40 mt-2 max-h-[60vh] overflow-y-auto rounded-sm border border-line bg-panel pb-2 shadow-[0_12px_40px_rgba(0,0,0,0.5)]">
                {!hasResults && <div className="px-4 py-3 text-sm text-inkfaint">Nothing in the stacks for “{q.trim()}”.</div>}

                {results.artists.length > 0 && <GroupLabel>Artists</GroupLabel>}
                {results.artists.map((a) => (
                  <Link
                    key={a.id}
                    href={`/artist/${a.id}`}
                    onClick={close}
                    className="flex items-center gap-3 px-4 py-2 text-sm text-inkdim transition-colors hover:bg-panel2 hover:text-ink"
                  >
                    <span className="relative block h-9 w-9 shrink-0 overflow-hidden rounded-full border border-line">
                      {a.albums[0] && (
                        <CoverArt
                          id={a.albums[0].id}
                          title={a.name}
                          artHash={a.artHash ?? a.albums[0].artHash}
                        />
                      )}
                    </span>
                    <span className="truncate">{a.name}</span>
                  </Link>
                ))}

                {results.albums.length > 0 && <GroupLabel>Albums</GroupLabel>}
                {results.albums.map((al) => (
                  <Link
                    key={al.id}
                    href={`/album/${al.id}`}
                    onClick={close}
                    className="flex items-center gap-3 px-4 py-2 text-sm text-inkdim transition-colors hover:bg-panel2 hover:text-ink"
                  >
                    <span className="relative block h-9 w-9 shrink-0 overflow-hidden rounded-sm border border-line">
                      <CoverArt id={al.id} title={al.title} artHash={al.artHash} />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate">{al.title}</span>
                      <span className="block truncate text-xs text-inkfaint">
                        {/* year 0 = unknown (issue #167): say nothing, not "0" */}
                        {al.artist}
                        {al.year ? ` · ${al.year}` : ""}
                      </span>
                    </span>
                  </Link>
                ))}

                {results.tracks.length > 0 && <GroupLabel>Tracks</GroupLabel>}
                {results.tracks.map((t) => (
                  <button
                    key={t.id}
                    type="button"
                    onClick={async () => {
                      close();
                      try {
                        const res = await fetch(`/api/album?id=${encodeURIComponent(t.albumId)}`);
                        if (!res.ok) return;
                        const album = (await res.json()) as Album;
                        const i = album.tracks.findIndex((x) => x.id === t.id);
                        playTracks(album.tracks, Math.max(i, 0), album.title);
                      } catch {
                        // no album, no play -- the overlay already closed
                      }
                    }}
                    className="flex w-full items-center gap-3 px-4 py-2 text-left text-sm text-inkdim transition-colors hover:bg-panel2 hover:text-ink"
                  >
                    <span className="relative block h-9 w-9 shrink-0 overflow-hidden rounded-sm border border-line">
                      <CoverArt id={t.albumId} title={t.album} artHash={t.artHash} />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate">{t.title}</span>
                      <span className="block truncate text-xs text-inkfaint">{t.artist}</span>
                    </span>
                    <span className="shrink-0 text-xs tabular-nums text-inkfaint">
                      {formatDuration(t.durationS)}
                    </span>
                  </button>
                ))}
              </div>
            </>
          )}
        </div>
      </div>
    </header>
  );
}
