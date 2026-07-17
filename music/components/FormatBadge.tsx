// The format pill (issue #157) -- the quality badge's nerdy sibling. Quality
// states fidelity (16-bit 44.1 kHz); this states what the file IS (ALAC ·
// M4A). Border matches the quality pill's lossless tier and the buttons
// below (#159 -- the "quietest pill" darker border just read as
// inconsistent); no tier colors, gold stays the quality badge's alone.
// Like QualityBadge, text size belongs to the caller.

export function FormatBadge({ label, className }: { label: string | null; className?: string }) {
  if (!label) return null;
  return (
    <span
      className={`stamp inline-flex items-center whitespace-nowrap rounded-full border border-linebright px-2 py-0.5 text-inkdim ${className ?? ""}`}
    >
      {label}
    </span>
  );
}
