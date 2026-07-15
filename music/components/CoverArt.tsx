// Deterministic generated cover art (issue #116). The fixture library has no
// real artwork; instead of gray squares (which would let layout bugs hide),
// every album renders a stable two-hue SVG chosen by lib/cover.ts. Real art
// replaces this component's internals, not its callers.

import { coverParams } from "@/lib/cover";

export function CoverArt({
  id,
  title,
  className,
}: {
  id: string;
  title: string;
  className?: string;
}) {
  const { hue1, hue2, pattern } = coverParams(id);
  const bg0 = `hsl(${hue1} 38% 16%)`;
  const bg1 = `hsl(${hue1} 45% 26%)`;
  const fg = `hsl(${hue2} 55% 58%)`;
  const fgSoft = `hsl(${hue2} 45% 45%)`;
  const gid = `cov-${id}`;

  return (
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
  );
}
