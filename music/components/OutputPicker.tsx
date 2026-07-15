"use client";

// Sound-output popover (issue #116) -- the exact list Phase 2 confirmed:
// this browser, the Denon in the living room, the LG in the basement.
// Selection is client state until the playback daemon exists; the row shapes
// won't change when it does.

import { listOutputs } from "@/lib/api";
import { usePlayer } from "./PlayerProvider";
import { CheckIcon, OutputIcon } from "./icons";

export function OutputPicker({ onClose }: { onClose: () => void }) {
  const { outputId, setOutputId } = usePlayer();
  return (
    <>
      {/* click-away backdrop, under the popover */}
      <button type="button" aria-label="Close sound output" className="fixed inset-0 z-40 cursor-default" onClick={onClose} />
      <div className="absolute bottom-full right-0 z-50 mb-3 w-72 rounded-sm border border-line bg-panel shadow-[0_12px_40px_rgba(0,0,0,0.5)]">
        <div className="stamp px-4 pb-1 pt-3 text-[10px] text-inkfaint">Sound output</div>
        <ul className="pb-2">
          {listOutputs().map((o) => {
            const active = o.id === outputId;
            return (
              <li key={o.id}>
                <button
                  type="button"
                  onClick={() => {
                    setOutputId(o.id);
                    onClose();
                  }}
                  className="flex w-full items-center gap-3 px-4 py-2.5 text-left text-sm text-inkdim transition-colors hover:bg-panel2 hover:text-ink"
                >
                  <OutputIcon className="h-4 w-4 shrink-0" />
                  <span className="min-w-0 flex-1 truncate">{o.name}</span>
                  {/* the check reserves its box either way -- rule #1 */}
                  <CheckIcon className={`h-4 w-4 shrink-0 text-led ${active ? "" : "invisible"}`} />
                </button>
              </li>
            );
          })}
        </ul>
      </div>
    </>
  );
}
