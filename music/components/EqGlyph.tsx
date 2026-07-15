// The needle-drop glyph: three bars that dance while sound is (nominally)
// coming out and hold a low idle while paused. Wears currentColor, so the
// row that carries it decides the meaning -- in practice always --led,
// because green means LIVE (the one job it kept from the MCC).

export function EqGlyph({ paused, className }: { paused: boolean; className?: string }) {
  return (
    <span
      className={`inline-flex h-3.5 items-end gap-[2px] ${paused ? "eq-paused" : ""} ${className ?? ""}`}
      aria-hidden
    >
      <span className="eq-bar h-full w-[3px] rounded-[1px] bg-current" />
      <span className="eq-bar h-full w-[3px] rounded-[1px] bg-current" />
      <span className="eq-bar h-full w-[3px] rounded-[1px] bg-current" />
    </span>
  );
}
