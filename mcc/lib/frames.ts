// The event still-shot archive (issue #90). The daemon publishes each event's
// annotated frame to the bus; frame_archiver.py on pearl files it under
// MERLE_FRAMES_DIR; the /frames/[id] route serves it from there. These pure
// helpers keep the id sanitization and filename mapping testable -- and the
// filename shapes here must match what the archiver writes
// (<id>.jpg / <id>.thumb.jpg).

// Mirror of frame_archiver.py's SAFE_ID: ids are [A-Za-z0-9_-] only. No dots
// (kills ".."), no slashes or backslashes, nothing a filesystem could
// interpret. The daemon mints safe ids by construction; this guard exists
// because the route's id arrives from a URL, not from the daemon.
const FRAME_ID = /^[A-Za-z0-9_-]+$/;

/** The archived filename for a frame id, or null when the id is unsafe --
 * the route's path-traversal guard. thumb=true names the ~320px copy. */
export function frameFilename(id: string, thumb: boolean): string | null {
  if (!FRAME_ID.test(id)) return null;
  return thumb ? `${id}.thumb.jpg` : `${id}.jpg`;
}

/** The journal's URL for one entry's image. Bare /frames/<id> is the
 * full-size variant (what the upcoming full-screen journal view consumes);
 * ?thumb=1 the thumbnail the Field Journal renders. */
export function frameUrl(frameId: string, thumb = false): string {
  return thumb ? `/frames/${frameId}?thumb=1` : `/frames/${frameId}`;
}
