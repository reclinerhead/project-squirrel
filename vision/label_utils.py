# =============================================================================
# project-squirrel -- label_utils.py
#
# Shared helpers for writing YOLO label sidecars. Our trained model is NMS-free
# (an end-to-end YOLO head), so ultralytics' NMS `iou` threshold is inert: the
# model occasionally emits two boxes for a single animal and nothing upstream
# removes them. live.py hits the same thing from a second direction -- ByteTrack
# can fragment one animal into two track IDs. Both paths de-duplicate here before
# writing training labels, so the same subject isn't labeled twice.
# =============================================================================


def box_iou(a, b):
    """IoU of two (x1, y1, x2, y2) boxes, in any consistent unit (pixels or
    normalized)."""
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (area_a + area_b - inter)


def dedupe_boxes(boxes, iou_thresh=0.7):
    """Greedy duplicate suppression. `boxes` is a list of (x1, y1, x2, y2) in
    priority order (most-trusted first -- higher confidence, or a live track
    before its stale coasting twin). Returns the indices to KEEP: a box is
    dropped when it overlaps an already-kept box by >= iou_thresh.

    Class-agnostic on purpose. At this much overlap two boxes are the same
    physical animal regardless of predicted class, so this also collapses the
    case where the model labels one animal as two different species. The
    threshold is deliberately high (0.7): real, distinct animals -- even in a
    tight feeding cluster -- overlap far less, so they're never merged, while the
    observed duplicates sit at 0.95+ and are caught cleanly.
    """
    kept = []
    for i, b in enumerate(boxes):
        if all(box_iou(b, boxes[k]) < iou_thresh for k in kept):
            kept.append(i)
    return kept
