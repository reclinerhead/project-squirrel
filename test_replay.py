# Tests for replay_events.py's pure timing math (the MQTT side is I/O,
# desk-tested against the real broker).

import replay_events


def _ev(ts, kind="arrival"):
    return {"ts": ts, "kind": kind, "details": None}


def test_first_event_fires_immediately():
    waits = replay_events.gaps([_ev("2026-07-06T10:00:00")], speed=1.0, max_gap=60)
    assert waits == [0.0]


def test_original_spacing_preserved():
    events = [_ev("2026-07-06T10:00:00"), _ev("2026-07-06T10:00:07")]
    assert replay_events.gaps(events, speed=1.0, max_gap=60) == [0.0, 7.0]


def test_speed_divides_the_waits():
    events = [_ev("2026-07-06T10:00:00"), _ev("2026-07-06T10:00:10")]
    assert replay_events.gaps(events, speed=4.0, max_gap=60) == [0.0, 2.5]


def test_long_silences_are_clamped():
    events = [_ev("2026-07-06T10:00:00"), _ev("2026-07-06T14:00:00")]
    assert replay_events.gaps(events, speed=1.0, max_gap=60) == [0.0, 60.0]


def test_out_of_order_timestamps_never_go_negative():
    events = [_ev("2026-07-06T10:00:10"), _ev("2026-07-06T10:00:00")]
    assert replay_events.gaps(events, speed=1.0, max_gap=60) == [0.0, 0.0]
