# Tests for perception.py -- the shared tracker bookkeeping. Pure logic, fed
# synthetic detection tuples, so no camera or model is needed. This is the code
# both live.py and the daemon's RTSP source rely on, so it's the piece most
# worth locking down.

import numpy as np

import perception
from perception import TrackMemory


def det(tid, name="squirrel", conf=0.6, box=(10, 10, 50, 50)):
    return (tid, name, conf, box)


def test_live_track_on_match():
    tm = TrackMemory()
    live, coasting = tm.ingest([det(1)])
    assert [tid for tid, _ in live] == [1]
    assert coasting == []


def test_track_coasts_then_prunes():
    tm = TrackMemory(coast_frames=3, prune_frames=6)
    tm.ingest([det(1)])                       # frame 1: live
    for _ in range(3):                        # frames 2-4: within coast window
        live, coasting = tm.ingest([])
    assert [tid for tid, _ in coasting] == [1]
    assert live == []
    for _ in range(4):                        # push age past prune_frames
        live, coasting = tm.ingest([])
    assert coasting == []                     # dropped from the coasting list
    assert 1 not in tm.tracks                 # and forgotten entirely


def test_coasting_track_returns_to_live_on_rematch():
    tm = TrackMemory(coast_frames=5)
    tm.ingest([det(1)])
    tm.ingest([])                             # miss once -> coasting
    live, coasting = tm.ingest([det(1)])      # matched again -> live
    assert [tid for tid, _ in live] == [1]
    assert coasting == []


def test_class_vote_is_majority_over_life():
    tm = TrackMemory()
    tm.ingest([det(1, "chipmunk")])
    tm.ingest([det(1, "squirrel")])
    tm.ingest([det(1, "squirrel")])           # squirrel now leads 2-1
    live, _ = tm.ingest([det(1, "squirrel")])
    assert perception.voted(live[0][1]) == "squirrel"


def test_seen_accumulates_and_survives_prune():
    tm = TrackMemory(coast_frames=1, prune_frames=2, census_after=1)
    tm.ingest([det(1, "squirrel")])
    tm.ingest([det(2, "chipmunk")])
    for _ in range(5):                        # let track 1 prune away
        tm.ingest([det(2, "chipmunk")])
    assert 1 not in tm.tracks                 # pruned from active memory
    assert tm.seen[1] == "squirrel"           # but remembered in the census
    assert tm.seen[2] == "chipmunk"


def test_xyxy_and_conf_updated_each_frame():
    tm = TrackMemory()
    tm.ingest([det(1, box=(0, 0, 10, 10), conf=0.3)])
    live, _ = tm.ingest([det(1, box=(5, 5, 25, 25), conf=0.8)])
    t = live[0][1]
    assert t["xyxy"] == [5, 5, 25, 25]
    assert t["conf"] == 0.8


# --- identity stitching (issue #22) -------------------------------------------
# The failure being fixed: a stationary feeding squirrel flickers out of
# detection past ByteTrack's buffer, gets a NEW id on re-acquisition, and one
# animal becomes several "visitors" (and its coasting ghost pads the crowd).

def test_iou():
    assert perception.iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    assert perception.iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0
    assert abs(perception.iou((0, 0, 10, 10), (5, 0, 15, 10)) - 1 / 3) < 1e-9


def test_reminted_id_adopts_the_lost_identity():
    tm = TrackMemory(census_after=1)
    tm.ingest([det(1, box=(10, 10, 50, 50))])
    for _ in range(20):                            # lost past coast, within prune
        tm.ingest([])
    live, coasting = tm.ingest([det(7, box=(12, 10, 52, 50))])   # re-mint, barely moved
    assert [tid for tid, _ in live] == [1]         # same squirrel, same identity
    assert coasting == []                          # no ghost twin left behind
    assert tm.seen == {1: "squirrel"}              # census: ONE visitor


def test_stitch_folds_votes_and_freshens_the_box():
    tm = TrackMemory()
    tm.ingest([det(1, box=(10, 10, 50, 50))])
    tm.ingest([])
    live, _ = tm.ingest([det(7, box=(14, 10, 54, 50), conf=0.9)])
    t = live[0][1]
    assert t["votes"]["squirrel"] == 2             # both detections on one record
    assert t["xyxy"] == [14, 10, 54, 50]           # tracking the animal, not the past
    assert t["conf"] == 0.9


def test_stitch_never_crosses_species():
    tm = TrackMemory()
    tm.ingest([det(1, "squirrel", box=(10, 10, 50, 50))])
    tm.ingest([])
    live, _ = tm.ingest([det(2, "turkey", box=(10, 10, 50, 50))])
    assert [tid for tid, _ in live] == [2]         # perfect overlap, wrong animal


def test_stitch_requires_solid_overlap():
    tm = TrackMemory()
    tm.ingest([det(1, box=(10, 10, 50, 50))])
    tm.ingest([])
    live, _ = tm.ingest([det(2, box=(200, 200, 240, 240))])
    assert [tid for tid, _ in live] == [2]         # elsewhere = genuinely new


def test_live_neighbor_is_never_a_stitch_target():
    # Two real squirrels shoulder to shoulder (overlapping, but below the
    # duplicate-box threshold): the one matched THIS frame must not absorb
    # the newcomer.
    tm = TrackMemory(census_after=1)
    tm.ingest([det(1, box=(10, 10, 50, 50))])
    live, _ = tm.ingest([det(1, box=(10, 10, 50, 50)),
                         det(2, box=(35, 10, 75, 50))])
    assert sorted(tid for tid, _ in live) == [1, 2]
    assert tm.seen == {1: "squirrel", 2: "squirrel"}


def test_one_lost_track_stitches_at_most_one_newcomer():
    # Both newcomers overlap the lost track past STITCH_IOU, but not each
    # other past DEDUPE_IOU -- only the better match adopts the identity.
    tm = TrackMemory()
    tm.ingest([det(1, box=(10, 10, 50, 50))])
    tm.ingest([])
    live, _ = tm.ingest([det(7, box=(11, 10, 51, 50)),
                         det(8, box=(25, 10, 65, 50))])
    assert sorted(tid for tid, _ in live) == [1, 8]   # 7 adopted; 8 is its own animal


def test_stitch_window_closes_when_the_track_prunes():
    tm = TrackMemory(coast_frames=1, prune_frames=3)
    tm.ingest([det(1, box=(10, 10, 50, 50))])
    for _ in range(5):                             # pruned away entirely
        tm.ingest([])
    live, _ = tm.ingest([det(2, box=(10, 10, 50, 50))])
    assert [tid for tid, _ in live] == [2]


def test_alias_persists_for_the_reminted_ids_lifetime():
    tm = TrackMemory(census_after=1)
    tm.ingest([det(1, box=(10, 10, 50, 50))])
    tm.ingest([])
    tm.ingest([det(7, box=(10, 10, 50, 50))])      # stitch happens here
    live, _ = tm.ingest([det(7, box=(11, 10, 51, 50))])   # ByteTrack keeps saying 7
    assert [tid for tid, _ in live] == [1]
    assert tm.seen == {1: "squirrel"}


def test_aliases_chain_flatten_to_the_original_identity():
    tm = TrackMemory()
    tm.ingest([det(1, box=(10, 10, 50, 50))])
    tm.ingest([])
    tm.ingest([det(7, box=(10, 10, 50, 50))])      # 7 -> 1
    for _ in range(5):
        tm.ingest([])
    live, _ = tm.ingest([det(9, box=(10, 10, 50, 50))])   # re-mint again
    assert [tid for tid, _ in live] == [1]
    assert tm.aliases[9] == 1                      # maps to the CANONICAL id, not 7


# --- duplicate-box dedupe + census tenure (issue #24) --------------------------
# The model is NMS-free: it can emit two boxes on one animal, and each
# duplicate reaching ByteTrack minted a parallel track riding the same
# squirrel. And any tracker occasionally coughs up one-blink junk tracks --
# neither may count as a visitor.

def test_dedupe_collapses_duplicates_keeping_highest_conf():
    dets = [det(1, conf=0.6, box=(10, 10, 50, 50)),
            det(2, conf=0.9, box=(11, 10, 51, 50))]     # same animal, two boxes
    kept = perception.dedupe_detections(dets)
    assert [d[0] for d in kept] == [2]                  # the confident one wins


def test_dedupe_leaves_distinct_animals_alone():
    dets = [det(1, box=(10, 10, 50, 50)),
            det(2, box=(100, 10, 140, 50))]
    assert len(perception.dedupe_detections(dets)) == 2


def test_dedupe_is_class_agnostic():
    # A duplicate box sometimes carries the other class -- still one animal.
    dets = [det(1, "squirrel", conf=0.9, box=(10, 10, 50, 50)),
            det(2, "turkey", conf=0.6, box=(11, 10, 51, 50))]
    kept = perception.dedupe_detections(dets)
    assert [d[0] for d in kept] == [1]


def test_duplicate_box_never_mints_a_parallel_track():
    tm = TrackMemory(census_after=1)
    tm.ingest([det(1, box=(10, 10, 50, 50))])
    # Same frame, same animal, second box with a fresh ByteTrack id:
    live, _ = tm.ingest([det(1, conf=0.6, box=(10, 10, 50, 50)),
                         det(88, conf=0.9, box=(11, 10, 51, 50))])
    assert [tid for tid, _ in live] == [1]              # one animal, one track
    assert tm.seen == {1: "squirrel"}


def test_dedupe_composes_with_stitching():
    # When the KEPT duplicate carries the new id, track 1 goes unmatched this
    # frame -- which makes it exactly what the stitch layer looks for. The new
    # id folds straight back onto it.
    tm = TrackMemory(census_after=1)
    tm.ingest([det(1, box=(10, 10, 50, 50))])
    live, _ = tm.ingest([det(1, conf=0.6, box=(10, 10, 50, 50)),
                         det(88, conf=0.9, box=(10, 10, 50, 50))])
    assert [tid for tid, _ in live] == [1]
    assert tm.aliases.get(88) == 1


def test_one_blink_track_never_reaches_the_census():
    tm = TrackMemory(census_after=3)
    tm.ingest([det(9)])                                 # one frame of junk
    for _ in range(10):
        tm.ingest([])
    assert tm.seen == {}


def test_track_counts_as_visitor_once_tenured():
    tm = TrackMemory(census_after=3)
    tm.ingest([det(1)])
    tm.ingest([det(1)])
    assert tm.seen == {}                                # not yet
    tm.ingest([det(1)])
    assert tm.seen == {1: "squirrel"}                   # third matched frame


def test_stitched_frames_accumulate_toward_tenure():
    # A re-mint mid-visit must not reset the visitor clock: frames fold onto
    # the canonical track across the stitch.
    tm = TrackMemory(census_after=3)
    tm.ingest([det(1, box=(10, 10, 50, 50))])
    tm.ingest([det(1, box=(10, 10, 50, 50))])
    tm.ingest([])                                       # flicker
    tm.ingest([det(7, box=(11, 10, 51, 50))])           # re-mint -> stitched
    assert tm.seen == {1: "squirrel"}                   # 2 + 1 frames = tenured


def test_class_colors_are_stable_and_cover_names():
    colors = perception.class_colors({0: "chipmunk", 1: "squirrel", 2: "turkey"})
    assert set(colors) == {"chipmunk", "squirrel", "turkey"}
    assert colors == perception.class_colors({0: "chipmunk", 1: "squirrel", 2: "turkey"})


def test_class_colors_pin_to_name_not_position():
    # The regression that dropping chipmunk (index 0) would have caused: a
    # species must keep its color no matter its index or how many classes there
    # are, or the stream and the frontend accents drift apart.
    three = perception.class_colors({0: "chipmunk", 1: "squirrel", 2: "turkey"})
    two = perception.class_colors({0: "squirrel", 1: "turkey"})
    assert two["squirrel"] == three["squirrel"]   # squirrel stays orange, not red
    assert two["turkey"] == three["turkey"]


def test_class_colors_falls_back_for_unknown_species():
    colors = perception.class_colors({0: "raccoon"})
    assert colors["raccoon"] in perception.PALETTE


def test_draw_tracks_runs_without_error():
    # Not asserting pixels -- just that the drawing path is valid for both a live
    # (colored) and a coasting (grey) item on a real frame buffer.
    frame = np.zeros((240, 320, 3), np.uint8)
    colors = perception.class_colors({0: "squirrel"})
    items = [(1, "squirrel", (10, 10, 60, 60), True),
             (2, "squirrel", (100, 100, 150, 150), False)]
    out = perception.draw_tracks(frame, items, colors, scale=0.5)
    assert out.shape == frame.shape
    assert out.any()                          # something was drawn
