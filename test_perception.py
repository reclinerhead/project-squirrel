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
    tm = TrackMemory(coast_frames=1, prune_frames=2)
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
