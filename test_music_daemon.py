# =============================================================================
# project-squirrel -- test_music_daemon.py
#
# The playback daemon's pure half and its HTTP surface (issue #129): Range
# parsing (the silent off-by-one genre -- a wrong Content-Range survives
# `pnpm build` AND a manual listen), DIDL escaping (this library holds titles
# with & and <), the completed-vs-skipped rule that play_history's integrity
# rests on, and the traversal guard.
#
# Everything runs against a :memory: catalog, a temp file, and a fake
# renderer -- no Denon, no NAS, no network. The SOAP half and the SSDP half
# are I/O at the boundary, verified against the real AVR, not by CI.
#
# NOTE: this file must be on .github/workflows/tests.yml's pytest line by
# hand. CI enumerates test files and has no pytest.ini/testpaths fallback.
# =============================================================================

import pytest
from fastapi.testclient import TestClient

from jukebox import music_catalog as mc
from jukebox import music_daemon as md


# --- parse_range ----------------------------------------------------------------

def test_no_header_serves_the_whole_file():
    assert md.parse_range(None, 100) is None
    assert md.parse_range("", 100) is None


def test_simple_range_is_inclusive_both_ends():
    assert md.parse_range("bytes=0-7", 100) == (0, 7)


def test_open_ended_range_runs_to_eof():
    assert md.parse_range("bytes=40-", 100) == (40, 99)


def test_suffix_range_is_the_last_n_bytes():
    """Renderers really send this to read a trailing index; getting it wrong
    plays static."""
    assert md.parse_range("bytes=-10", 100) == (90, 99)


def test_suffix_larger_than_file_clamps_to_start():
    assert md.parse_range("bytes=-500", 100) == (0, 99)


def test_end_past_eof_is_clamped_not_rejected():
    assert md.parse_range("bytes=90-200", 100) == (90, 99)


def test_start_past_eof_is_unsatisfiable():
    with pytest.raises(ValueError):
        md.parse_range("bytes=100-", 100)


def test_zero_length_suffix_is_unsatisfiable():
    with pytest.raises(ValueError):
        md.parse_range("bytes=-0", 100)


def test_malformed_and_multirange_fall_back_to_the_whole_file():
    """RFC 7233 lets a server ignore Range rather than implement
    multipart/byteranges; a 200 is always a legal answer."""
    for h in ("bytes=1-2,5-6", "octets=0-7", "bytes=a-b", "bytes="):
        assert md.parse_range(h, 100) is None


def test_range_against_empty_file_is_unsatisfiable():
    with pytest.raises(ValueError):
        md.parse_range("bytes=0-7", 0)


# --- the wire shapes ------------------------------------------------------------

def test_content_types_cover_every_catalog_format():
    assert md.content_type_for("m4a") == "audio/mp4"
    assert md.content_type_for("mp4") == "audio/mp4"
    assert md.content_type_for("mp3") == "audio/mpeg"
    assert md.content_type_for("flac") == "audio/flac"
    assert md.content_type_for("wav") == "audio/wav"
    assert md.content_type_for("weird") == "application/octet-stream"


def test_didl_escapes_the_library_we_actually_have():
    """Titles with & and < exist in this library. Unescaped they truncate the
    metadata mid-tag and the renderer refuses the URI."""
    didl = md.didl_for("Mixtape <3 & Chill", "Simon & Garfunkel",
                       "http://x/stream/b:abc?x=1&y=2", "audio/mp4")
    assert "Mixtape &lt;3 &amp; Chill" in didl
    assert "Simon &amp; Garfunkel" in didl
    assert "x=1&amp;y=2" in didl
    assert "<3" not in didl
    assert 'http-get:*:audio/mp4:' in didl


def test_didl_survives_null_tags():
    didl = md.didl_for(None, None, "http://x/s/1", "audio/mpeg")
    assert "<dc:title>Unknown</dc:title>" in didl


def test_hms_round_trips():
    assert md.hms(193.4) == "0:03:13"
    assert md.parse_hms("0:03:13") == 193.0
    assert md.parse_hms("1:00:05") == 3605.0


def test_parse_hms_treats_renderer_notanswers_as_unknown():
    for t in ("NOT_IMPLEMENTED", "", None, "garbage"):
        assert md.parse_hms(t) is None


# --- completed vs skipped -------------------------------------------------------

def test_reaching_the_end_is_a_completion():
    assert md.outcome_for(193.0, 193.0) == mc.PLAY_COMPLETED


def test_the_last_poll_gap_does_not_steal_a_completion():
    """The watcher polls every 2s and the Denon zeroes RelTime on stop, so
    the recorded position is up to a poll short of the duration."""
    assert md.outcome_for(184.0, 193.0) == mc.PLAY_COMPLETED


def test_bailing_early_is_a_skip():
    assert md.outcome_for(12.0, 193.0) == mc.PLAY_SKIPPED


def test_ninety_percent_of_a_long_track_completes():
    assert md.outcome_for(540.0, 600.0) == mc.PLAY_COMPLETED
    assert md.outcome_for(500.0, 600.0) == mc.PLAY_SKIPPED


def test_unknown_duration_or_position_never_credits_a_listen():
    """Implicit feedback errs toward NOT crediting: a phantom completion
    poisons Phase 3's ranking; a lost one costs a replay."""
    assert md.outcome_for(100.0, None) == mc.PLAY_SKIPPED
    assert md.outcome_for(100.0, 0) == mc.PLAY_SKIPPED
    assert md.outcome_for(None, 193.0) == mc.PLAY_SKIPPED


# --- the app, end to end against fakes ------------------------------------------

AUDIO = bytes(range(256)) * 40  # 10,240 recognizable bytes


class FakeRenderer:
    def __init__(self):
        self.name = "Denon AVR-X4000"
        self.calls = []
        self.transport = "PLAYING"
        self.pos = (0.0, 193.0)

    def set_uri(self, url, didl):
        self.calls.append(("set_uri", url, didl))

    def play(self):
        self.calls.append(("play",))

    def pause(self):
        self.calls.append(("pause",))

    def stop(self):
        self.calls.append(("stop",))

    def seek(self, seconds):
        self.calls.append(("seek", seconds))

    def transport_state(self):
        return self.transport

    def position(self):
        return self.pos


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A live app over a :memory: catalog holding one real temp file. No
    cache on purpose (and the env cleared, so a developer's real
    MERLE_MUSIC_CACHE can't leak in): this rig is the Denon-only world, which
    must keep working on a bare checkout."""
    monkeypatch.delenv("MERLE_MUSIC_CACHE", raising=False)
    audio_file = tmp_path / "01 Safe And Sound.m4a"
    audio_file.write_bytes(AUDIO)
    conn = mc.connect(":memory:")
    mc.upsert_track(conn, {
        "id": "b:abc", "title": "Safe And Sound", "artist": "Capital Cities",
        "album": "In A Tidal Wave Of Mystery", "duration_s": 193.0,
        "format": "m4a", "indexed_at": 1000, "year": 2013, "genre": "Pop",
        "bpm": 118.0, "replaygain_db": -9.1, "dynamic_range_db": 5.0})
    mc.upsert_file(conn, {
        "path": str(audio_file), "track_id": "b:abc", "size": len(AUDIO),
        "mtime": 200, "audio_offset": 0, "audio_length": len(AUDIO),
        "seen_at": 1000})
    # A second track in a format the Denon table refuses. Its OWN path --
    # track_files' PK is the path, so sharing one would silently steal the
    # first track's location row (found the hard way).
    odd_file = tmp_path / "odd.aac"
    odd_file.write_bytes(AUDIO)
    mc.upsert_track(conn, {"id": "b:odd", "title": "Odd One",
                           "artist": "X", "album": "Y",
                           "duration_s": 100.0, "format": "aac",
                           "indexed_at": 1000, "year": 2012, "genre": "Pop",
                           "bpm": 121.0, "replaygain_db": -8.8,
                           "dynamic_range_db": 5.5})
    mc.upsert_file(conn, {"path": str(odd_file), "track_id": "b:odd",
                          "size": len(AUDIO), "mtime": 200,
                          "audio_offset": 0, "audio_length": len(AUDIO),
                          "seen_at": 1000})
    renderer = FakeRenderer()
    app = md.create_app(conn=conn, renderer=renderer,
                        stream_base="http://pearl.test:8090",
                        publisher_factory=lambda: object())
    with TestClient(app) as client:
        yield client, conn, renderer


def test_stream_serves_the_exact_bytes(rig):
    client, _, _ = rig
    r = client.get("/stream/b:abc")
    assert r.status_code == 200
    assert r.content == AUDIO
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-type"].startswith("audio/mp4")


def test_stream_answers_a_range_with_206_and_correct_content_range(rig):
    client, _, _ = rig
    r = client.get("/stream/b:abc", headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.content == AUDIO[100:200]
    assert r.headers["content-range"] == "bytes 100-199/%d" % len(AUDIO)
    assert r.headers["content-length"] == "100"


def test_stream_suffix_range_serves_the_tail(rig):
    client, _, _ = rig
    r = client.get("/stream/b:abc", headers={"Range": "bytes=-16"})
    assert r.status_code == 206
    assert r.content == AUDIO[-16:]


def test_stream_unsatisfiable_range_is_416_with_the_size(rig):
    client, _, _ = rig
    r = client.get("/stream/b:abc",
                   headers={"Range": "bytes=%d-" % (len(AUDIO) * 2)})
    assert r.status_code == 416
    assert r.headers["content-range"] == "bytes */%d" % len(AUDIO)


def test_stream_head_carries_headers_and_no_body(rig):
    client, _, _ = rig
    r = client.head("/stream/b:abc")
    assert r.status_code == 200
    assert r.headers["content-length"] == str(len(AUDIO))
    assert r.content == b""


def test_stream_rejects_a_traversal_shaped_id_before_the_catalog(rig):
    client, _, _ = rig
    # An encoded slash never reaches the endpoint (starlette's router refuses
    # the path outright), so the allowlist's job is everything else that IS a
    # single path segment but isn't an id: dots, spaces, tildes.
    assert client.get("/stream/..").status_code in (400, 404)
    assert client.get("/stream/b:abc%20def").status_code == 400
    assert client.get("/stream/~root").status_code == 400


def test_stream_unknown_id_is_a_404(rig):
    client, _, _ = rig
    assert client.get("/stream/b:nope").status_code == 404


def test_play_hands_the_renderer_our_stream_url(rig):
    client, _, renderer = rig
    r = client.post("/play", json={"track_id": "b:abc", "output": "denon"})
    assert r.status_code == 200
    kinds = [c[0] for c in renderer.calls]
    assert kinds == ["set_uri", "play"]
    _, url, didl = renderer.calls[0]
    assert url == "http://pearl.test:8090/stream/b:abc"
    assert "Capital Cities" in didl


def test_play_refuses_a_format_the_output_does_not_play(rig):
    client, _, renderer = rig
    r = client.post("/play", json={"track_id": "b:odd", "output": "denon"})
    assert r.status_code == 415
    assert renderer.calls == []


def test_play_refuses_an_unknown_output(rig):
    client, _, _ = rig
    r = client.post("/play", json={"track_id": "b:abc", "output": "toaster"})
    assert r.status_code == 422


def test_play_rejects_malformed_id_and_unknown_track(rig):
    client, _, _ = rig
    assert client.post("/play", json={"track_id": "../../x",
                                      "output": "denon"}).status_code == 400
    assert client.post("/play", json={"track_id": "b:nope",
                                      "output": "denon"}).status_code == 404


def test_stop_after_a_dozen_seconds_records_a_skip(rig):
    client, conn, renderer = rig
    renderer.pos = (12.0, 193.0)
    client.post("/play", json={"track_id": "b:abc", "output": "denon"})
    client.get("/state")  # a poll observes 12.0 while PLAYING
    client.post("/stop")
    rows = conn.execute("SELECT track_id, outcome, seconds, output "
                        "FROM play_history").fetchall()
    assert len(rows) == 1
    assert rows[0]["outcome"] == mc.PLAY_SKIPPED
    assert rows[0]["track_id"] == "b:abc"
    assert rows[0]["seconds"] == 12.0
    assert rows[0]["output"] == "denon"


def test_stop_near_the_end_records_a_completion(rig):
    client, conn, renderer = rig
    renderer.pos = (190.0, 193.0)
    client.post("/play", json={"track_id": "b:abc", "output": "denon"})
    client.get("/state")
    client.post("/stop")
    row = conn.execute("SELECT outcome FROM play_history").fetchone()
    assert row["outcome"] == mc.PLAY_COMPLETED


def test_playing_over_a_track_records_the_first_as_skipped(rig):
    client, conn, renderer = rig
    renderer.pos = (30.0, 193.0)
    client.post("/play", json={"track_id": "b:abc", "output": "denon"})
    client.get("/state")
    client.post("/play", json={"track_id": "b:abc", "output": "denon"})
    rows = conn.execute("SELECT outcome FROM play_history").fetchall()
    assert [r["outcome"] for r in rows] == [mc.PLAY_SKIPPED]


def test_pause_resume_and_seek_reach_the_renderer(rig):
    client, _, renderer = rig
    client.post("/play", json={"track_id": "b:abc", "output": "denon"})
    client.post("/pause")
    client.post("/play", json={})  # bare play = resume
    client.post("/seek", json={"seconds": 180})
    kinds = [c[0] for c in renderer.calls]
    assert kinds == ["set_uri", "play", "pause", "play", "seek"]
    assert renderer.calls[-1] == ("seek", 180.0)


def test_resume_with_nothing_loaded_is_a_409(rig):
    client, _, _ = rig
    assert client.post("/play", json={}).status_code == 409


def test_state_degrades_when_the_renderer_sulks(rig):
    client, _, renderer = rig
    client.post("/play", json={"track_id": "b:abc", "output": "denon"})

    def boom():
        raise OSError("unplugged")
    renderer.transport_state = boom
    r = client.get("/state")
    assert r.status_code == 200
    assert r.json()["transport"] == "UNREACHABLE"


def test_state_is_calm_with_nothing_playing(rig):
    client, _, _ = rig
    body = client.get("/state").json()
    assert body["track"] is None
    assert body["transport"] == "NO_MEDIA_PRESENT"
    assert body["outputs"][0]["id"] == "denon"


# --- POST /rate (issue #135) ---------------------------------------------------
#
# The thumbs are the one table this stack cannot rebuild, so the route's job is
# to be boring: take four values and a clear, refuse everything else, and never
# let a wire id near a query it shouldn't reach.

def test_rate_persists_a_thumb(rig):
    client, conn, _ = rig
    r = client.post("/rate", json={"track_id": "b:abc", "value": 2})
    assert r.status_code == 200
    assert r.json() == {"track_id": "b:abc", "value": 2}
    row = conn.execute("SELECT value, rated_at FROM ratings "
                       "WHERE track_id = 'b:abc'").fetchone()
    assert row["value"] == 2
    assert row["rated_at"] > 0  # the daemon's clock, not the client's


@pytest.mark.parametrize("value", [-2, -1, 1, 2])
def test_rate_round_trips_every_legal_value(rig, value):
    client, conn, _ = rig
    assert client.post("/rate",
                       json={"track_id": "b:abc", "value": value}).status_code == 200
    assert conn.execute("SELECT value FROM ratings WHERE track_id = 'b:abc'"
                        ).fetchone()["value"] == value


def test_re_rating_replaces_rather_than_appends(rig):
    client, conn, _ = rig
    client.post("/rate", json={"track_id": "b:abc", "value": 1})
    client.post("/rate", json={"track_id": "b:abc", "value": -2})
    rows = conn.execute("SELECT value FROM ratings "
                        "WHERE track_id = 'b:abc'").fetchall()
    assert [r["value"] for r in rows] == [-2]  # one row, the current opinion


def test_zero_clears_the_rating(rig):
    # The control's third click. A legal thing to send, an illegal thing to
    # store: an unrated track is the absence of a row, not a stored zero.
    client, conn, _ = rig
    client.post("/rate", json={"track_id": "b:abc", "value": 1})
    r = client.post("/rate", json={"track_id": "b:abc", "value": 0})
    assert r.status_code == 200
    assert conn.execute("SELECT COUNT(*) AS n FROM ratings").fetchone()["n"] == 0


def test_clearing_an_unrated_track_is_a_no_op(rig):
    client, conn, _ = rig
    assert client.post("/rate",
                       json={"track_id": "b:abc", "value": 0}).status_code == 200
    assert conn.execute("SELECT COUNT(*) AS n FROM ratings").fetchone()["n"] == 0


@pytest.mark.parametrize("value", [3, -3, 99, "up", None, 1.5, [1], True, False])
def test_rate_refuses_an_illegal_value_and_writes_nothing(rig, value):
    # True/False are in here deliberately: bool subclasses int, so `true`
    # satisfies `in RATING_VALUES` and would file itself as a thumbs-up.
    client, conn, _ = rig
    r = client.post("/rate", json={"track_id": "b:abc", "value": value})
    assert r.status_code == 400
    assert conn.execute("SELECT COUNT(*) AS n FROM ratings").fetchone()["n"] == 0


def test_rate_refuses_a_missing_value(rig):
    client, conn, _ = rig
    assert client.post("/rate", json={"track_id": "b:abc"}).status_code == 400
    assert conn.execute("SELECT COUNT(*) AS n FROM ratings").fetchone()["n"] == 0


def test_rate_rejects_a_malformed_id_before_the_catalog(rig):
    client, conn, _ = rig
    for bad in ["../../etc/passwd", "b abc", "b:abc;drop", "", "~/x"]:
        r = client.post("/rate", json={"track_id": bad, "value": 1})
        assert r.status_code == 400, bad
    assert conn.execute("SELECT COUNT(*) AS n FROM ratings").fetchone()["n"] == 0


def test_rate_rejects_a_non_string_id(rig):
    client, _, _ = rig
    assert client.post("/rate", json={"track_id": 7, "value": 1}).status_code == 400


def test_rate_404s_an_unknown_but_well_formed_id(rig):
    # A wrong URL, not a broken daemon -- /play's rule, and a rating for a
    # track we don't have is not a row worth inventing.
    client, conn, _ = rig
    assert client.post("/rate",
                       json={"track_id": "b:nope", "value": 1}).status_code == 404
    assert conn.execute("SELECT COUNT(*) AS n FROM ratings").fetchone()["n"] == 0


def test_rating_a_track_does_not_touch_playback(rig):
    # Rating is not a transport verb: it shares the proxy and the lock, and
    # must not so much as breathe on the renderer.
    client, _, renderer = rig
    client.post("/play", json={"track_id": "b:abc", "output": "denon"})
    before = list(renderer.calls)
    client.post("/rate", json={"track_id": "b:abc", "value": -2})
    assert renderer.calls == before
    assert client.get("/state").json()["track"]["id"] == "b:abc"


# --- POST /queue (issue #139) ----------------------------------------------------
#
# The route boundary only: the engine's arithmetic, anti-repetition, and
# determinism live in test_music_playlist.py. Here: seed validation, the 400s,
# and the promise that whatever comes back can actually stream.

def test_queue_from_a_track_seed_returns_streamable_tracks(rig):
    client, conn, _ = rig
    r = client.post("/queue", json={"seed": {"track_id": "b:abc"}})
    assert r.status_code == 200
    tracks = r.json()["tracks"]
    assert tracks, "two candidates minus the seed leaves one"
    for t in tracks:
        assert set(t) == set(md.QUEUE_TRACK_KEYS)  # the GUI's TrackRow shape
        assert mc.file_for_track(conn, t["id"]) is not None
    # radio from a song must not open by replaying it
    assert "b:abc" not in [t["id"] for t in tracks]


def test_queue_from_an_artist_seed_works(rig):
    client, _, _ = rig
    r = client.post("/queue", json={"seed": {"artist": "X"}})
    assert r.status_code == 200
    assert [t["id"] for t in r.json()["tracks"]]


def test_queue_for_an_unknown_artist_is_empty_not_an_error(rig):
    client, _, _ = rig
    r = client.post("/queue", json={"seed": {"artist": "Nobody At All"}})
    assert r.status_code == 200
    assert r.json()["tracks"] == []


def test_queue_respects_n_and_exclude(rig):
    client, _, _ = rig
    r = client.post("/queue", json={"seed": {"track_id": "b:abc"},
                                    "n": 1, "exclude": ["b:odd"]})
    assert r.status_code == 200
    assert r.json()["tracks"] == []  # the one candidate was excluded


def test_queue_400s_garbage(rig):
    client, _, _ = rig
    bad = [
        {},                                          # no seed at all
        {"seed": "b:abc"},                           # seed not an object
        {"seed": {}},                                # neither track nor artist
        {"seed": {"track_id": "../../etc/passwd"}},  # traversal-shaped id
        {"seed": {"track_id": 7}},                   # non-string id
        {"seed": {"artist": ""}},                    # blank artist
        {"seed": {"artist": "X"}, "n": 0},           # n out of range
        {"seed": {"artist": "X"}, "n": True},        # bool masquerading as int
        {"seed": {"artist": "X"}, "n": "lots"},      # n not an integer
        {"seed": {"artist": "X"}, "exclude": "b:abc"},      # not a list
        {"seed": {"artist": "X"}, "exclude": ["b abc"]},    # malformed id
    ]
    for body in bad:
        assert client.post("/queue", json=body).status_code == 400, body


def test_queue_404s_an_unknown_seed_track(rig):
    client, _, _ = rig
    r = client.post("/queue", json={"seed": {"track_id": "b:nope"}})
    assert r.status_code == 404


def test_queue_does_not_touch_playback(rig):
    """The do-not-change line verbatim: /queue generates lists; it does not
    start playback or hold queue state. The daemon stays one-track-at-a-time
    on the transport verbs."""
    client, _, renderer = rig
    client.post("/play", json={"track_id": "b:abc", "output": "denon"})
    before = list(renderer.calls)
    client.post("/queue", json={"seed": {"track_id": "b:abc"}})
    assert renderer.calls == before
    assert client.get("/state").json()["track"]["id"] == "b:abc"


def test_queue_excludes_a_track_played_moments_ago(rig):
    """Anti-repetition against REAL play_history: play b:odd (recorded by the
    watcher's bookkeeping via /stop), then seed a queue -- b:odd is inside
    the cooldown window and must be gone."""
    client, _, renderer = rig
    renderer.pos = (12.0, 193.0)
    client.post("/play", json={"track_id": "b:abc", "output": "denon"})
    client.get("/state")
    client.post("/stop")  # records the history row for b:abc
    r = client.post("/queue", json={"seed": {"artist": "X"}})
    assert r.status_code == 200
    assert "b:abc" not in [t["id"] for t in r.json()["tracks"]]


# --- the browser output (issue #149) ----------------------------------------------
#
# The other transport: the listener's <audio> element pulls ?output=browser,
# ALAC repacks to FLAC through the cache (the lossless rule), natively
# decodable formats stay raw, and history arrives by client report. The cache
# here is a fake -- the real ffmpeg is proven on pearl; these tests pin the
# POLICY: who transcodes, who streams untouched, who may never be asked.

from jukebox import music_cache as mcc


class FakeCache:
    """ensure() answers like the real thing: a finished file when one exists,
    otherwise an already-done Job whose part file holds fake FLAC bytes --
    which exercises the daemon's tailing branch end to end."""

    def __init__(self, root):
        self.root = root
        self.ensures = []

    def path_for(self, track_id):
        return os.path.join(self.root, mcc.cache_name(track_id))

    def lookup(self, track_id):
        p = self.path_for(track_id)
        return p if os.path.isfile(p) else None

    def ensure(self, track_id, src_path, background=False):
        self.ensures.append((track_id, background))
        hit = self.lookup(track_id)
        if hit:
            return "file", hit
        final = self.path_for(track_id)
        job = mcc.Job(final + ".part", final)
        with open(job.part_path, "wb") as f:
            f.write(b"FLACDATA")
        job.ok = True
        job.done.set()
        return "job", job


import os  # noqa: E402  (grouped with the fixture that needs it)


@pytest.fixture
def brig(tmp_path, monkeypatch):
    """The browser-era rig: an ALAC track, an AAC track, an mp3, and an
    unprobed m4a, plus an injected fake cache."""
    monkeypatch.delenv("MERLE_MUSIC_CACHE", raising=False)
    conn = mc.connect(":memory:")
    tracks = [
        ("b:alc", "m4a", "alac", "alac.m4a"),
        ("b:acc", "m4a", "aac", "aac.m4a"),
        ("b:mp3", "mp3", None, "x.mp3"),
        ("b:nul", "m4a", None, "unprobed.m4a"),
    ]
    for tid, fmt, codec, name in tracks:
        p = tmp_path / name
        p.write_bytes(AUDIO)
        mc.upsert_track(conn, {
            "id": tid, "title": tid, "artist": "A", "album": "B",
            "duration_s": 193.0, "format": fmt, "codec": codec,
            "indexed_at": 1000, "bpm": 120.0})
        mc.upsert_file(conn, {
            "path": str(p), "track_id": tid, "size": len(AUDIO),
            "mtime": 200, "audio_offset": 0, "audio_length": len(AUDIO),
            "seen_at": 1000})
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache = FakeCache(str(cache_dir))
    app = md.create_app(conn=conn, renderer=FakeRenderer(),
                        stream_base="http://pearl.test:8090",
                        publisher_factory=lambda: object(), cache=cache)
    with TestClient(app) as client:
        yield client, conn, cache


def test_browser_gets_aac_bytes_untouched(brig):
    """Already lossy: repacking loses nothing but inflates for no reason,
    and the cache must not even be consulted."""
    client, _, cache = brig
    r = client.get("/stream/b:acc", params={"output": "browser"})
    assert r.status_code == 200
    assert r.content == AUDIO
    assert r.headers["content-type"].startswith("audio/mp4")
    assert cache.ensures == []


def test_browser_gets_mp3_bytes_untouched(brig):
    client, _, cache = brig
    r = client.get("/stream/b:mp3", params={"output": "browser"})
    assert r.content == AUDIO
    assert cache.ensures == []


def test_browser_alac_cold_click_tails_the_transcode(brig):
    client, _, cache = brig
    r = client.get("/stream/b:alc", params={"output": "browser"})
    assert r.status_code == 200
    assert r.content == b"FLACDATA"
    assert r.headers["content-type"].startswith("audio/flac")
    assert "content-length" not in r.headers  # chunked: size unknowable
    assert cache.ensures == [("b:alc", False)]  # a listener waits: not gated


def test_browser_unprobed_m4a_takes_the_never_lossy_default(brig):
    client, _, cache = brig
    r = client.get("/stream/b:nul", params={"output": "browser"})
    assert r.status_code == 200
    assert r.content == b"FLACDATA"


def test_browser_alac_cache_hit_serves_ranges(brig):
    """The whole reason the cache is a file: a cached track seeks like one."""
    client, _, cache = brig
    with open(cache.path_for("b:alc"), "wb") as f:
        f.write(b"CACHEDFLACBYTES")
    r = client.get("/stream/b:alc", params={"output": "browser"},
                   headers={"Range": "bytes=6-9"})
    assert r.status_code == 206
    assert r.content == b"FLAC"
    assert r.headers["content-range"] == "bytes 6-9/15"
    assert r.headers["content-type"].startswith("audio/flac")


def test_browser_head_probe_never_costs_a_transcode(brig):
    client, _, cache = brig
    r = client.head("/stream/b:alc", params={"output": "browser"})
    assert r.status_code == 200
    assert cache.ensures == []


def test_denon_still_gets_alac_bytes_untouched(brig):
    """The do-not-change line: every URL the daemon ever handed the renderer
    means exactly what it did in 2a -- raw catalog bytes, no cache."""
    client, _, cache = brig
    r = client.get("/stream/b:alc")  # no ?output= -- the Denon's URL shape
    assert r.status_code == 200
    assert r.content == AUDIO
    assert r.headers["content-type"].startswith("audio/mp4")
    assert cache.ensures == []


def test_stream_refuses_an_unknown_output(brig):
    client, _, _ = brig
    r = client.get("/stream/b:alc", params={"output": "toaster"})
    assert r.status_code == 422


def test_play_refuses_the_browser_output(brig):
    """The browser's transport is the listener's tab; /play aims renderers."""
    client, _, _ = brig
    r = client.post("/play", json={"track_id": "b:alc", "output": "browser"})
    assert r.status_code == 422


def test_browser_alac_without_a_cache_is_a_503(rig):
    """The kill switch: MERLE_MUSIC_CACHE unset means no browser transcode.
    (rig, not brig -- the cacheless world.)"""
    client, _, _ = rig
    r = client.get("/stream/b:abc", params={"output": "browser"})
    assert r.status_code == 503


def test_state_offers_the_browser_exactly_when_the_cache_exists(rig, brig):
    ids = {o["id"]: o["available"]
           for o in rig[0].get("/state").json()["outputs"]}
    assert ids["browser"] is False
    ids = {o["id"]: o["available"]
           for o in brig[0].get("/state").json()["outputs"]}
    assert ids["browser"] is True


def test_state_carries_the_stream_base(brig):
    assert brig[0].get("/state").json()["stream_base"] == \
        "http://pearl.test:8090"


# --- POST /report -----------------------------------------------------------------

def test_report_near_the_end_is_a_completion(brig):
    client, conn, _ = brig
    r = client.post("/report", json={"track_id": "b:alc",
                                     "position_s": 190.0})
    assert r.status_code == 200
    assert r.json()["outcome"] == mc.PLAY_COMPLETED
    row = conn.execute("SELECT outcome, seconds, output "
                       "FROM play_history").fetchone()
    assert row["outcome"] == mc.PLAY_COMPLETED
    assert row["seconds"] == 190.0
    assert row["output"] == "browser"


def test_report_early_bail_is_a_skip(brig):
    client, conn, _ = brig
    r = client.post("/report", json={"track_id": "b:alc",
                                     "position_s": 12.0})
    assert r.json()["outcome"] == mc.PLAY_SKIPPED


def test_report_without_a_position_errs_toward_skip(brig):
    """outcome_for's rule, one arbiter for both transports: unknown position
    never credits a listen."""
    client, conn, _ = brig
    assert client.post("/report",
                       json={"track_id": "b:alc"}).json()["outcome"] == \
        mc.PLAY_SKIPPED
    assert conn.execute("SELECT seconds FROM play_history"
                        ).fetchone()["seconds"] is None


def test_report_validates_like_rate_does(brig):
    client, conn, _ = brig
    assert client.post("/report", json={"track_id": "../../x",
                                        "position_s": 1}).status_code == 400
    assert client.post("/report", json={"track_id": 7}).status_code == 400
    assert client.post("/report", json={"track_id": "b:alc",
                                        "position_s": True}).status_code == 400
    assert client.post("/report", json={"track_id": "b:alc",
                                        "position_s": "far"}).status_code == 400
    assert client.post("/report", json={"track_id": "b:nope",
                                        "position_s": 1}).status_code == 404
    assert conn.execute("SELECT COUNT(*) AS n "
                        "FROM play_history").fetchone()["n"] == 0


# --- POST /precache ---------------------------------------------------------------

def test_precache_warms_only_what_needs_the_flac_path(brig):
    """aac and mp3 stream raw and an unknown id is a skip, not an error --
    warming is best-effort by contract."""
    client, _, cache = brig
    r = client.post("/precache", json={
        "track_ids": ["b:alc", "b:acc", "b:mp3", "b:nope"]})
    assert r.status_code == 200
    assert r.json() == {"queued": 1}
    assert cache.ensures == [("b:alc", True)]  # background: gated, not urgent


def test_precache_skips_an_already_cached_track(brig):
    client, _, cache = brig
    with open(cache.path_for("b:alc"), "wb") as f:
        f.write(b"X")
    assert client.post("/precache", json={
        "track_ids": ["b:alc"]}).json() == {"queued": 0}


def test_precache_400s_garbage(brig):
    client, _, _ = brig
    for body in ({}, {"track_ids": "b:alc"},
                 {"track_ids": ["b alc"]},
                 {"track_ids": ["b:alc"] * (md.PRECACHE_MAX + 1)}):
        assert client.post("/precache", json=body).status_code == 400, body


def test_precache_without_a_cache_is_a_503(rig):
    client, _, _ = rig
    assert client.post("/precache",
                       json={"track_ids": ["b:abc"]}).status_code == 503
