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


# --- the necromancer pass (issue #74, Phase 2.4) --------------------------------
# The IoU stitch only resurrects an animal that came back WHERE it vanished.
# The crowd fixture showed under half of re-mints overlap their corpse -- the
# animal moved a body length or two while the tracker had lost it. A new id of
# the same species born within NECRO_REACH body-lengths of a VANISHED track
# (lost past coast, not yet pruned) is that track come back.

def test_moved_remint_is_raised_onto_its_corpse():
    # Dead box (100,100,180,180): 80px body, reach 160. The newcomer sits a
    # full body away -- zero overlap, well inside reach.
    tm = TrackMemory(census_after=1)
    tm.ingest([det(1, box=(100, 100, 180, 180))])
    for _ in range(20):                                # vanished: past coast (15)
        tm.ingest([])
    live, coasting = tm.ingest([det(7, box=(240, 100, 320, 180))])
    assert [tid for tid, _ in live] == [1]             # same squirrel, same identity
    assert coasting == []
    assert tm.seen == {1: "squirrel"}                  # census: ONE visitor
    assert tm.total_raised == 1
    assert tm.total_births == 1                        # no second birth


def test_necromancer_reach_is_bounded():
    # Same corpse, newcomer centered 240px away: past the 160px reach.
    tm = TrackMemory(census_after=1)
    tm.ingest([det(1, box=(100, 100, 180, 180))])
    for _ in range(20):
        tm.ingest([])
    live, _ = tm.ingest([det(7, box=(340, 100, 420, 180))])
    assert [tid for tid, _ in live] == [7]             # genuinely new animal
    assert tm.total_raised == 0


def test_necromancer_never_crosses_species():
    tm = TrackMemory(census_after=1)
    tm.ingest([det(1, "squirrel", box=(100, 100, 180, 180))])
    for _ in range(20):
        tm.ingest([])
    live, _ = tm.ingest([det(7, "turkey", box=(240, 100, 320, 180))])
    assert [tid for tid, _ in live] == [7]


def test_midhop_remint_is_raised_from_a_coasting_corpse():
    # The crowd fixture's biggest leak: a squirrel hops, ByteTrack fails the
    # association mid-hop, and the new id births a fraction of a second later
    # under a body length away -- old track still coasting, zero overlap.
    # The tight coasting tier catches exactly this.
    tm = TrackMemory(census_after=1)
    tm.ingest([det(1, box=(100, 100, 180, 180))])
    tm.ingest([])                                      # missed one frame: coasting
    live, coasting = tm.ingest([det(7, box=(160, 100, 240, 180))])   # 60px hop
    assert [tid for tid, _ in live] == [1]             # same squirrel mid-hop
    assert coasting == []
    assert tm.total_raised == 1


def test_coasting_reach_is_one_body_length():
    # Beyond one body length, a newcomer beside a coasting track is more
    # likely a REAL second animal arriving -- it must not merge. (A vanished
    # corpse gets the full NECRO_REACH; a coasting one only the tight tier.)
    tm = TrackMemory(census_after=1)
    tm.ingest([det(1, box=(100, 100, 180, 180))])
    tm.ingest([])                                      # missed one frame: coasting
    live, coasting = tm.ingest([det(7, box=(240, 100, 320, 180))])   # 140px away
    assert [tid for tid, _ in live] == [7]
    assert [tid for tid, _ in coasting] == [1]         # both animals accounted for
    assert tm.total_raised == 0


def test_necromancer_window_closes_at_prune():
    tm = TrackMemory(coast_frames=1, prune_frames=3, census_after=1)
    tm.ingest([det(1, box=(100, 100, 180, 180))])
    for _ in range(5):                                 # pruned away entirely
        tm.ingest([])
    live, _ = tm.ingest([det(7, box=(240, 100, 320, 180))])
    assert [tid for tid, _ in live] == [7]


def test_nearest_grave_wins():
    tm = TrackMemory(census_after=1)
    tm.ingest([det(1, box=(100, 100, 180, 180)),       # grave A
               det(2, box=(400, 100, 480, 180))])      # grave B
    for _ in range(20):
        tm.ingest([])
    # Newcomer at (330..410): center (370,140) -- 90px from B, 230px from A.
    live, _ = tm.ingest([det(7, box=(330, 100, 410, 180))])
    assert [tid for tid, _ in live] == [2]


def test_overlapping_corpse_beats_a_nearer_grave():
    # Stitch (IoU) runs first: an overlapping corpse is the stronger claim
    # even when some other grave is technically closer by center distance.
    tm = TrackMemory(census_after=1)
    tm.ingest([det(1, box=(100, 100, 180, 180)),
               det(2, box=(150, 100, 230, 180))])
    for _ in range(20):
        tm.ingest([])
    # Newcomer overlaps #1 heavily (IoU >= 0.4) but its center is nearer #2.
    live, _ = tm.ingest([det(7, box=(110, 100, 190, 180))])
    assert [tid for tid, _ in live] == [1]
    assert tm.total_stitches == 1
    assert tm.total_raised == 0


def test_raised_track_keeps_accumulating_tenure():
    # Same contract as the stitch: a resurrection mid-visit must not reset
    # the visitor clock.
    tm = TrackMemory(coast_frames=1, prune_frames=100, census_after=3)
    tm.ingest([det(1, box=(100, 100, 180, 180))])
    tm.ingest([det(1, box=(100, 100, 180, 180))])
    tm.ingest([])
    tm.ingest([])                                      # past coast: vanished
    tm.ingest([det(7, box=(240, 100, 320, 180))])      # raised, 3rd matched frame
    assert tm.seen == {1: "squirrel"}


def test_necromancer_logs_the_resurrection():
    lines = []
    tm = TrackMemory(census_after=1, log=lines.append)
    tm.ingest([det(1, box=(100, 100, 180, 180))])
    for _ in range(20):
        tm.ingest([])
    tm.ingest([det(7, box=(240, 100, 320, 180))])
    assert any("#7 raised onto #1" in line for line in lines)


# --- churn instrumentation (issue #74, Phase 0) --------------------------------
# Measure before building: mint/birth/stitch/death counting, per-track
# confidence stats, and the rolling metrics every Phase 2 change is graded
# against. All camera-free; the daemon and replay_fixture.py just read these.

def test_metrics_counts_mints_births_and_stitches():
    tm = TrackMemory(census_after=1)
    tm.ingest([det(1)])
    tm.ingest([])
    tm.ingest([det(7, box=(11, 10, 51, 50))])       # re-mint -> stitched
    m = tm.metrics()
    assert m["ids_minted"] == 2                     # ByteTrack minted two raw ids
    assert m["births"] == 1                         # ...but one canonical animal
    assert m["stitches"] == 1


def test_duplicate_box_still_counts_as_a_mint():
    # Dedupe drops the box before the bookkeeping, but ByteTrack DID mint the
    # id -- and tracker churn is what the metric measures, so it counts.
    tm = TrackMemory(census_after=1)
    tm.ingest([det(1)])
    tm.ingest([det(1, conf=0.6), det(88, conf=0.9, box=(11, 10, 51, 50))])
    m = tm.metrics()
    assert m["ids_minted"] == 2
    assert m["births"] == 1


def test_conf_stats_track_the_dips():
    tm = TrackMemory()
    tm.ingest([det(1, conf=0.3)])
    tm.ingest([det(1, conf=0.8)])
    live, _ = tm.ingest([det(1, conf=0.4)])
    t = live[0][1]
    assert t["conf_min"] == 0.3
    assert t["conf_max"] == 0.8
    assert abs(t["conf_sum"] / t["frames"] - 0.5) < 1e-9


def test_death_records_lifetime_and_tenure():
    tm = TrackMemory(coast_frames=1, prune_frames=2, census_after=2)
    tm.ingest([det(9)])                             # one matched frame of junk
    for _ in range(4):
        tm.ingest([])
    m = tm.metrics()
    assert m["deaths_window"] == 1
    assert m["never_confirmed_window"] == 1         # died below census tenure
    assert m["median_lifetime_frames"] == 1


def test_metrics_rates_and_fragmentation():
    tm = TrackMemory(census_after=1)
    for _ in range(60):                             # two steady animals, 4s at 15fps
        tm.ingest([det(1), det(2, box=(100, 10, 140, 50))])
    m = tm.metrics(fps=15.0)
    assert m["ids_per_minute"] == 30.0              # 2 mints in 1/15 of a minute
    assert m["mean_concurrency"] == 2.0
    assert m["fragmentation"] == 1.0                # one id per animal: no churn
    assert m["canonical_fragmentation"] == 1.0      # ...and one identity per animal
    assert m["median_lifetime_frames"] is None      # nobody has died


def test_fragmentation_is_none_on_an_empty_pavement():
    tm = TrackMemory()
    for _ in range(30):
        tm.ingest([])
    assert tm.metrics()["fragmentation"] is None    # never divide by ~zero


def test_metrics_window_forgets_old_mints_and_deaths():
    tm = TrackMemory(coast_frames=1, prune_frames=2, census_after=1,
                     metrics_window=10)
    tm.ingest([det(1)])                             # mint at frame 1...
    for _ in range(15):                             # ...then die and age out
        tm.ingest([])
    m = tm.metrics()
    assert m["ids_minted"] == 1                     # the all-time count survives
    assert m["ids_minted_window"] == 0              # the window has moved on
    assert m["births_window"] == 0
    assert m["deaths_window"] == 0


def test_lifecycle_log_narrates_births_stitches_and_deaths():
    lines = []
    tm = TrackMemory(coast_frames=1, prune_frames=2, census_after=2,
                     log=lines.append)
    tm.ingest([det(1, conf=0.62)])
    tm.ingest([])
    tm.ingest([det(7, box=(11, 10, 51, 50))])       # stitched back onto #1
    for _ in range(4):                              # prune it (2 matched frames = tenured)
        tm.ingest([])
    text = "\n".join(lines)
    assert "born #1 squirrel conf=0.62" in text
    assert "#7 stitched onto #1" in text
    assert "died #1 squirrel after 2 matched frames (tenured)" in text


def test_lifecycle_log_flags_never_confirmed_junk():
    lines = []
    tm = TrackMemory(coast_frames=1, prune_frames=2, census_after=5,
                     log=lines.append)
    tm.ingest([det(9)])
    for _ in range(4):
        tm.ingest([])
    assert any("never confirmed" in line for line in lines)


def test_silent_by_default():
    # No log hook -> no output machinery in the way of live.py or the tests.
    tm = TrackMemory()
    assert tm.log is None


# --- species-presence debounce (moved from the daemon, issue #74 Phase 0.5) ----
# The exact logic the Worker runs, now pure and replayable offline. The
# end-to-end paths (events reaching SQLite + the bus) stay covered in
# test_daemon.py; these lock the state machine itself.

def test_presence_announces_arrival_after_hold():
    p = perception.SpeciesPresence(arrive_after=2.0, depart_after=12.0)
    assert p.observe({"squirrel": 1}, now=0.0) == []
    assert p.observe({"squirrel": 1}, now=1.0) == []
    assert p.observe({"squirrel": 1}, now=2.5) == \
        [("arrival", {"species": "squirrel", "count": 1})]
    assert p.observe({"squirrel": 1}, now=3.0) == []   # announced once


def test_presence_wobble_resets_the_clock():
    p = perception.SpeciesPresence(arrive_after=2.0, depart_after=12.0)
    p.observe({"squirrel": 1}, now=0.0)
    p.observe({}, now=1.0)                             # dipped back before the hold
    assert p.observe({"squirrel": 1}, now=3.0) == []   # clock restarted here
    assert p.observe({"squirrel": 1}, now=5.5) == \
        [("arrival", {"species": "squirrel", "count": 1})]


def test_presence_departure_duration_only_when_last_one_leaves():
    p = perception.SpeciesPresence(arrive_after=1.0, depart_after=2.0)
    p.observe({"squirrel": 2}, now=0.0)
    assert p.observe({"squirrel": 2}, now=1.5) == \
        [("arrival", {"species": "squirrel", "count": 2})]
    p.observe({"squirrel": 1}, now=10.0)
    # One of two left: no duration (can't know which individual).
    assert p.observe({"squirrel": 1}, now=12.5) == \
        [("departure", {"species": "squirrel", "count": 1})]
    p.observe({}, now=20.0)
    events = p.observe({}, now=22.5)
    assert events[0][0] == "departure"
    assert events[0][1]["duration_s"] == 21.0          # since the arrival at 1.5


def test_presence_defaults_match_the_daemon():
    p = perception.SpeciesPresence()
    assert p.arrive_after == perception.ARRIVE_AFTER_S == 2.0
    assert p.depart_after == perception.DEPART_AFTER_S == 12.0


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
