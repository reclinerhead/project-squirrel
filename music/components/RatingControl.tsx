"use client";

// The split four-level thumb control (issue #116). Click sets +/-1, a second
// click on the same thumb escalates to +/-2 (drawn as a doubled thumb), a
// third clears; the opposite thumb replaces. The transition math lives in
// lib/rating.ts under an exhaustive table test -- this component only renders
// state and forwards clicks.
//
// Layout-shift discipline: the control always occupies its full box. Unrated
// rows show it faint (brightened on row hover by the parent's group class),
// never collapsed or unmounted.

import { ThumbIcon } from "./icons";
import type { Rating } from "@/lib/types";
import type { ThumbClick } from "@/lib/rating";

function Thumb({
  down,
  level,
  onClick,
}: {
  down: boolean;
  level: 0 | 1 | 2;
  onClick: () => void;
}) {
  const dir = down ? "down" : "up";
  const label =
    level === 0 ? `thumb ${dir}` : level === 1 ? `thumb ${dir} (set) -- click for strong` : `strong ${dir} -- click to clear`;
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      aria-label={label}
      aria-pressed={level > 0}
      title={label}
      className={`relative rounded-sm p-1 transition-colors ${
        level > 0 ? "text-ink" : "text-inkfaint hover:text-inkdim"
      }`}
    >
      <ThumbIcon down={down} className={`h-4 w-4 ${level > 0 ? "fill-current" : ""}`} />
      {/* strong = a second thumb peeking out behind the first */}
      <ThumbIcon
        down={down}
        className={`absolute left-2.5 top-0.5 h-4 w-4 transition-opacity ${
          level === 2 ? "opacity-70" : "opacity-0"
        }`}
      />
    </button>
  );
}

export function RatingControl({
  rating,
  onRate,
  className,
}: {
  rating: Rating;
  onRate: (click: ThumbClick) => void;
  className?: string;
}) {
  return (
    <span className={`inline-flex items-center gap-1 pr-1.5 ${className ?? ""}`}>
      <Thumb down level={rating === -2 ? 2 : rating === -1 ? 1 : 0} onClick={() => onRate("down")} />
      <Thumb down={false} level={rating === 2 ? 2 : rating === 1 ? 1 : 0} onClick={() => onRate("up")} />
    </span>
  );
}
