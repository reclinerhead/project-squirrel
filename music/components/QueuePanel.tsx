"use client";

// The play-queue slide-over (issue #116): history, "playing from", next up.
// One mutation only -- removing an upcoming track. That's the user-driven
// edit epic #115 allows; agent-driven queue rewriting stays a non-goal, and
// reorder/clear wait for Phase 2's real queue ops.

import { usePlayer } from "./PlayerProvider";
import { CoverArt } from "./CoverArt";
import { EqGlyph } from "./EqGlyph";
import { XIcon } from "./icons";
import type { Track } from "@/lib/types";

function Row({
  track,
  dim,
  right,
  lead,
}: {
  track: Track;
  dim?: boolean;
  right?: React.ReactNode;
  lead?: React.ReactNode;
}) {
  return (
    <div className="group flex items-center gap-3 px-4 py-2">
      <div className="relative h-9 w-9 shrink-0 overflow-hidden rounded-sm border border-line">
        <CoverArt id={track.albumId} title={track.album} />
      </div>
      <div className="min-w-0 flex-1">
        <div className={`flex items-center gap-2 truncate text-sm ${dim ? "text-inkfaint" : "text-ink"}`}>
          {lead}
          <span className="truncate">{track.title}</span>
        </div>
        <div className="truncate text-xs text-inkfaint">{track.artist}</div>
      </div>
      {right}
    </div>
  );
}

export function QueuePanel({ onClose }: { onClose: () => void }) {
  const { view, playingFrom, isPlaying, removeUpNext } = usePlayer();

  return (
    <>
      <button type="button" aria-label="Close play queue" className="fixed inset-0 z-40 cursor-default" onClick={onClose} />
      <div className="fixed bottom-[92px] right-3 z-50 flex max-h-[70vh] w-[min(360px,calc(100vw-24px))] flex-col rounded-sm border border-line bg-panel shadow-[0_12px_40px_rgba(0,0,0,0.5)]">
        <div className="flex items-center justify-between px-4 pb-1 pt-3">
          <h2 className="text-lg text-ink" style={{ fontFamily: "var(--font-display)" }}>
            Play queue
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded-sm p-1 text-inkfaint transition-colors hover:text-ink"
          >
            <XIcon className="h-4 w-4" />
          </button>
        </div>

        <div className="scrollpane min-h-0 flex-1 overflow-y-auto pb-2">
          {view.history.length > 0 && (
            <>
              <div className="stamp px-4 pb-1 pt-2 text-[10px] text-inkfaint">History</div>
              {view.history.slice(-3).map((t) => (
                <Row key={t.id} track={t} dim />
              ))}
            </>
          )}

          {view.current && (
            <>
              <div className="stamp px-4 pb-1 pt-2 text-[10px] text-inkfaint">
                Playing from: <span className="text-inkdim">{playingFrom}</span>
              </div>
              <Row
                track={view.current}
                lead={<EqGlyph paused={!isPlaying} className="shrink-0 text-led" />}
              />
            </>
          )}

          <div className="stamp px-4 pb-1 pt-2 text-[10px] text-inkfaint">Next up</div>
          {view.upNext.length === 0 ? (
            <div className="px-4 py-2 text-sm text-inkfaint">End of the queue.</div>
          ) : (
            view.upNext.map((t, i) => (
              <Row
                key={`${t.id}-${i}`}
                track={t}
                right={
                  <button
                    type="button"
                    onClick={() => removeUpNext(i)}
                    aria-label={`Remove ${t.title} from queue`}
                    className="rounded-sm p-1 text-inkfaint opacity-0 transition-opacity hover:text-ink focus:opacity-100 group-hover:opacity-100"
                  >
                    <XIcon className="h-3.5 w-3.5" />
                  </button>
                }
              />
            ))
          )}
        </div>
      </div>
    </>
  );
}
