"use client";

// Sound-output popover (issue #116), fed by discovery (issue #129): the rows
// are whatever outputs the daemon offers, fetched from /api/player/state when
// the popover opens -- the discovered renderers, plus "This browser" (issue
// #149), which the daemon lists as available exactly when its FLAC cache is
// configured (without it the ALAC majority couldn't play, and a 62%-broken
// output wearing a checkmark would be a lie). An unreachable daemon renders
// one disabled row saying so: the popover reserves its space either way
// (rule #1), and silence with no explanation is how mysteries get reported
// as bugs.

import { useEffect, useState } from "react";
import { usePlayer } from "./PlayerProvider";
import { CheckIcon, OutputIcon } from "./icons";

type OutputRow = { id: string; name: string; kind: string; available: boolean };

export function OutputPicker({ onClose }: { onClose: () => void }) {
  const { outputId, setOutputId } = usePlayer();
  const [outputs, setOutputs] = useState<OutputRow[] | null>(null);

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const res = await fetch("/api/player/state", { cache: "no-store" });
        if (!res.ok) throw new Error();
        const s = (await res.json()) as { outputs?: OutputRow[] };
        if (live) setOutputs(s.outputs ?? []);
      } catch {
        if (live) setOutputs([]);
      }
    })();
    return () => {
      live = false;
    };
  }, []);

  const rows: OutputRow[] =
    outputs === null
      ? [{ id: "loading", name: "Looking for outputs…", kind: "dlna", available: false }]
      : outputs.length === 0
        ? [{ id: "none", name: "No outputs reachable", kind: "dlna", available: false }]
        : outputs;

  return (
    <>
      {/* click-away backdrop, under the popover */}
      <button type="button" aria-label="Close sound output" className="fixed inset-0 z-40 cursor-default" onClick={onClose} />
      <div className="absolute bottom-full right-0 z-50 mb-3 w-72 rounded-sm border border-line bg-panel shadow-[0_12px_40px_rgba(0,0,0,0.5)]">
        <div className="stamp px-4 pb-1 pt-3 text-[10px] text-inkfaint">Sound output</div>
        <ul className="pb-2">
          {rows.map((o) => {
            const active = o.id === outputId;
            return (
              <li key={o.id}>
                <button
                  type="button"
                  disabled={!o.available}
                  onClick={() => {
                    setOutputId(o.id);
                    onClose();
                  }}
                  className="flex w-full items-center gap-3 px-4 py-2.5 text-left text-sm text-inkdim transition-colors enabled:hover:bg-panel2 enabled:hover:text-ink disabled:opacity-50"
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
