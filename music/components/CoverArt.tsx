"use client";

// Cover art (issues #116, #153). Real artwork when the catalog has it, the
// deterministic two-hue SVG when it doesn't -- and the SVG STAYS underneath
// as the loading state, so a slow image fades in over something intentional
// rather than popping over a gray hole. Exactly the swap the original banner
// promised: real art replaced this component's internals, not its callers
// (they still wrap it in a `relative` box; the img fills absolutely, so
// nothing shifts -- rule #1).
//
// "use client" is new with the img: the error fallback needs onError, and
// event handlers don't exist server-side. Server pages keep rendering this
// fine -- it's a leaf.
//
// The src is content-addressed (/api/art/<hash>/<size>, immutable-cached
// forever), so a changed cover is a changed URL and stale art is impossible.

import { useState } from "react";
import { coverParams } from "@/lib/cover";

export function CoverArt({
  id,
  title,
  artHash,
  size = "thumb",
  className,
}: {
  id: string;
  title: string;
  /** The catalog's art hash; null/absent renders the generated SVG alone. */
  artHash?: string | null;
  /** thumb (~160px) for grids/rows, large (~600px) for album/artist heroes. */
  size?: "thumb" | "large";
  className?: string;
}) {
  // A 404'd or corrupt image falls back to the SVG -- per instance, sticky
  // for this mount only, so one bad file can't hide anyone else's art.
  const [failed, setFailed] = useState(false);
  const { hue1, hue2, pattern } = coverParams(id);
  const bg0 = `hsl(${hue1} 38% 16%)`;
  const bg1 = `hsl(${hue1} 45% 26%)`;
  const fg = `hsl(${hue2} 55% 58%)`;
  const fgSoft = `hsl(${hue2} 45% 45%)`;
  const gid = `cov-${id}`;

  return (
    <>
      <svg
        viewBox="0 0 100 100"
        role="img"
        aria-label={`Cover art: ${title}`}
        className={className ?? "h-full w-full"}
        preserveAspectRatio="xMidYMid slice"
      >
        <defs>
          <linearGradient id={gid} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stopColor={bg1} />
            <stop offset="1" stopColor={bg0} />
          </linearGradient>
        </defs>
        <rect width="100" height="100" fill={`url(#${gid})`} />
        {pattern === 0 && (
          // rings: a record pressed off-center
          <g fill="none" stroke={fg} strokeWidth="1.6">
            {[8, 16, 24, 32, 40, 48].map((r, i) => (
              <circle key={r} cx="66" cy="60" r={r} opacity={0.75 - i * 0.1} stroke={i % 2 ? fgSoft : fg} />
            ))}
            <circle cx="66" cy="60" r="3" fill={fg} stroke="none" />
          </g>
        )}
        {pattern === 1 && (
          // beams: stage light through a doorway
          <g fill={fg}>
            <polygon points="0,100 34,0 46,0 12,100" opacity="0.8" />
            <polygon points="24,100 58,0 64,0 30,100" opacity="0.45" fill={fgSoft} />
            <polygon points="40,100 74,0 88,0 54,100" opacity="0.6" />
          </g>
        )}
        {pattern === 2 && (
          // blocks: crates against the wall
          <g fill={fg}>
            <rect x="10" y="52" width="36" height="38" opacity="0.75" />
            <rect x="52" y="24" width="38" height="66" opacity="0.4" fill={fgSoft} />
            <rect x="30" y="12" width="26" height="26" opacity="0.6" />
          </g>
        )}
        {pattern === 3 && (
          // waves: the signal itself
          <g fill="none" stroke={fg} strokeWidth="2.4" strokeLinecap="round">
            {[30, 50, 70].map((y, i) => (
              <path
                key={y}
                d={`M6 ${y} q 11 ${i % 2 ? -14 : 14} 22 0 t 22 0 t 22 0 t 22 0`}
                opacity={0.85 - i * 0.22}
                stroke={i === 1 ? fgSoft : fg}
              />
            ))}
          </g>
        )}
        {/* vignette keeps generated art quiet next to real ink */}
        <rect width="100" height="100" fill="black" opacity="0.18" />
      </svg>
      {artHash && !failed && (
        // eslint-disable-next-line @next/next/no-img-element -- LAN-local,
        // pre-sized at extraction; next/image's optimizer would be a second
        // resizer in front of files that already come in the right size.
        <img
          src={`/api/art/${artHash}/${size}`}
          alt=""
          loading="lazy"
          decoding="async"
          onError={() => setFailed(true)}
          className="absolute inset-0 h-full w-full object-cover opacity-0 transition-opacity duration-300 [&.loaded]:opacity-100"
          onLoad={(e) => e.currentTarget.classList.add("loaded")}
        />
      )}
    </>
  );
}
