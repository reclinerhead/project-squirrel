// Hand-drawn icon set (issue #116) -- stroke SVGs on currentColor, no icon
// library. Small on purpose: the bar's glyphs should read like transport
// controls on a deck, not like a UI kit.

type IconProps = { className?: string };

function I({ className, children }: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className ?? "h-4 w-4"}
      aria-hidden
    >
      {children}
    </svg>
  );
}

export function PlayIcon(p: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className={p.className ?? "h-4 w-4"} aria-hidden>
      <path d="M8 5.5v13l11-6.5z" />
    </svg>
  );
}

export function PauseIcon(p: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className={p.className ?? "h-4 w-4"} aria-hidden>
      <rect x="6.5" y="5" width="3.6" height="14" rx="0.8" />
      <rect x="13.9" y="5" width="3.6" height="14" rx="0.8" />
    </svg>
  );
}

export function PrevIcon(p: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className={p.className ?? "h-4 w-4"} aria-hidden>
      <path d="M17.5 5.5v13L8.5 12z" />
      <rect x="5.5" y="5.5" width="2" height="13" rx="0.6" />
    </svg>
  );
}

export function NextIcon(p: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className={p.className ?? "h-4 w-4"} aria-hidden>
      <path d="M6.5 5.5v13l9-6.5z" />
      <rect x="16.5" y="5.5" width="2" height="13" rx="0.6" />
    </svg>
  );
}

export function ShuffleIcon(p: IconProps) {
  return (
    <I {...p}>
      <path d="M3 7h3.5c4 0 6.5 10 10.5 10H21" />
      <path d="M3 17h3.5c1.6 0 2.9-1.6 4.1-3.4M21 7h-4c-1.6 0-2.9 1.6-4.1 3.4" />
      <path d="M18.5 4.5L21 7l-2.5 2.5M18.5 14.5L21 17l-2.5 2.5" />
    </I>
  );
}

export function RepeatIcon(p: IconProps) {
  return (
    <I {...p}>
      <path d="M4 12V9a3 3 0 013-3h13" />
      <path d="M20 12v3a3 3 0 01-3 3H4" />
      <path d="M17.5 3.5L20 6l-2.5 2.5M6.5 15.5L4 18l2.5 2.5" />
    </I>
  );
}

export function QueueIcon(p: IconProps) {
  return (
    <I {...p}>
      <path d="M4 6h16M4 12h16M4 18h9" />
      <path d="M17.5 15.5v5M15 18h5" />
    </I>
  );
}

export function OutputIcon(p: IconProps) {
  return (
    <I {...p}>
      <rect x="6" y="3.5" width="12" height="17" rx="1.5" />
      <circle cx="12" cy="14.5" r="3.2" />
      <circle cx="12" cy="7.5" r="1.2" />
    </I>
  );
}

export function VolumeIcon(p: IconProps) {
  return (
    <I {...p}>
      <path d="M4 9.5v5h3.5L12 19V5L7.5 9.5z" fill="currentColor" stroke="none" />
      <path d="M15.5 9a4.2 4.2 0 010 6M18 6.5a8 8 0 010 11" />
    </I>
  );
}

export function CheckIcon(p: IconProps) {
  return (
    <I {...p}>
      <path d="M4.5 12.5l5 5 10-11" />
    </I>
  );
}

export function XIcon(p: IconProps) {
  return (
    <I {...p}>
      <path d="M6 6l12 12M18 6L6 18" />
    </I>
  );
}

export function SearchIcon(p: IconProps) {
  return (
    <I {...p}>
      <circle cx="10.5" cy="10.5" r="6" />
      <path d="M15.5 15.5L21 21" />
    </I>
  );
}

export function ArrowLeftIcon(p: IconProps) {
  return (
    <I {...p}>
      <path d="M14.5 5.5L8 12l6.5 6.5" />
    </I>
  );
}

export function ArrowRightIcon(p: IconProps) {
  return (
    <I {...p}>
      <path d="M9.5 5.5L16 12l-6.5 6.5" />
    </I>
  );
}

export function RadioIcon(p: IconProps) {
  // A broadcast dot with radiating arcs -- "play me stuff like this" as a
  // transmitter, not a tower (the bar's glyph grammar is gear, not scenery).
  return (
    <I {...p}>
      <circle cx="12" cy="12" r="1.7" fill="currentColor" stroke="none" />
      <path d="M8.5 15.5a5 5 0 010-7M15.5 8.5a5 5 0 010 7" />
      <path d="M5.8 18.2a9 9 0 010-12.4M18.2 5.8a9 9 0 010 12.4" />
    </I>
  );
}

export function ThumbIcon({ className, down }: IconProps & { down?: boolean }) {
  // One path, flipped for down -- the two thumbs must be exact mirrors or
  // the split control looks lopsided at 14px.
  return (
    <svg
      viewBox="0 0 24 24"
      className={`${className ?? "h-4 w-4"} ${down ? "rotate-180" : ""}`}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M7 11l3.8-6.8a2 2 0 011.9 2.1L12.2 10H18a2 2 0 012 2.2l-.9 6A2 2 0 0117.1 20H9.5a2.5 2.5 0 01-2.5-2.5z" />
      <path d="M7 11H4.5v9H7z" />
    </svg>
  );
}
