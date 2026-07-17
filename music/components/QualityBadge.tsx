// The quality pill (issue #116) -- TIDAL brags about hi-res; with a ~62% ALAC
// library, so can we. Gold (--hires) is reserved for the >16-bit tier; the
// label always carries the facts in text, so no meaning rides on hue alone.

import type { QualityBadge as Badge } from "@/lib/quality";

const TIER_CLASSES: Record<Badge["tier"], string> = {
  hires: "border-hires/50 bg-hires/10 text-hires",
  lossless: "border-linebright text-inkdim",
  lossy: "border-line text-inkfaint",
};

/** Text size is the CALLER's (issue #157): the album header's metadata row
 * runs a notch bigger than the player bar's brag slot, and a size baked in
 * here would fight the className append. The player bar's idle placeholder
 * mirrors whatever its call site passes -- keep them in step. */
export function QualityBadge({ badge, className }: { badge: Badge; className?: string }) {
  if (!badge.label) return null;
  return (
    <span
      className={`stamp inline-flex items-center whitespace-nowrap rounded-full border px-2 py-0.5 tabular-nums ${TIER_CLASSES[badge.tier]} ${className ?? ""}`}
    >
      {badge.label}
    </span>
  );
}
