# =============================================================================
# project-squirrel -- music_daemon.py
#
# The playback daemon (issues #129 + #149, epic #115 Phase 2): streams catalog
# tracks over HTTP and drives the Denon AVR over UPnP/DLNA. Runs on pearl as
# the `music-daemon` unit, port 8090. Two outputs, opposite transports:
#
#   denon    the daemon drives the renderer (SOAP) and the renderer pulls
#            bytes UNTOUCHED -- it plays ALAC natively (spike-verified), so
#            transcoding it would throw away the lossless for nothing.
#   browser  the listener's <audio> element pulls /stream?output=browser and
#            the daemon drives NOTHING -- Chrome refuses ALAC outright (the
#            spike again), so the ALAC majority repacks to FLAC through
#            music_cache (lossless to lossless, issue #149's owner rule: no
#            lossy transcode, ever) while natively-decodable formats stay
#            raw. Play-history arrives by client report (POST /report), since
#            a browser tab can't be polled the way a renderer can.
#
# Shape: FastAPI, like vision/merle_daemon.py, because the one hard
# requirement -- HTTP Range on /stream -- doesn't fit the weather.py flat-loop
# shape. Everything else follows the pearl service conventions: bare
# print("[music] ...") logging, config validated at startup so a
# half-configured daemon refuses to run rather than looking healthy,
# KeyboardInterrupt only (SIGTERM stays unhandled -- that's what fires the
# bus Last Will, so `systemctl stop` flips music/status to offline with no
# signal-handling code here).
#
# WHY THE RENDERER PULLS INSTEAD OF US PUSHING: that's DLNA. SetAVTransportURI
# hands the Denon a URL; the Denon fetches the bytes itself. So the daemon is
# two halves that meet only at a URL: an HTTP file server the renderer (and
# later the browser) reads from, and a small SOAP client that tells the
# renderer which URL to read. The Denon receives the catalog file's bytes
# UNTOUCHED -- no transcode, nothing between open("rb") and the socket --
# which is the whole point of that output (lossless in the living room).
#
# RANGE IS HAND-ROLLED AND NON-NEGOTIABLE. Python's SimpleHTTPRequestHandler
# does not implement Range (the spike checked), and no Range means no seeking
# -- the epic's hard UX floor. The Denon itself opens with a probe GET and
# follows with a Range request. Multi-range requests are answered with the
# whole file (RFC 7233 lets a server ignore Range rather than implement
# multipart), which no real renderer or browser sends anyway.
#
# CAPABILITY IS A TABLE WE OWN, NOT A NEGOTIATION. The Denon answers
# GetProtocolInfo with HTTP 500 -- it refuses to say what it plays (the spike
# again), so asking is not an option. OUTPUT_FORMATS below is policy: what we
# are willing to hand each output, decided from the catalog's `format` column
# BEFORE the stream starts (epic principle 4 -- the decision is data we
# already have). Nothing sniffs bytes at runtime.
#
# PLAY HISTORY IS THE REASON THIS SHIPS EARLY. Implicit feedback cannot be
# backfilled; Phase 3's engine and Phase 4's agent both read it. The watcher
# thread records exactly one row per play: `completed` when the transport
# stops with the last observed position near the end, `skipped` when the
# listener moved on (a new /play while playing, or /stop mid-track). The
# position is the LAST ONE OBSERVED WHILE PLAYING, deliberately: the Denon
# resets RelTime to 0:00:00 the moment it stops, so reading the position
# after the stop would call every completion a skip-at-zero.
#
# Config (env, read at lifespan startup -- import stays side-effect-free so
# tests can build the app with everything injected):
#   MERLE_MUSIC_DB           the catalog. Must EXIST -- the indexer owns
#                            creating it; a daemon that silently opened an
#                            empty one would 404 every track while healthy.
#   MERLE_MUSIC_STREAM_BASE  this daemon's own URL as the RENDERER reaches it
#                            (e.g. http://192.168.1.64:8090). Required: the
#                            Denon fetches from this address, and guessing our
#                            own LAN-visible address is how you stream to a
#                            renderer that can't reach you.
#   MERLE_MQTT               the broker (bus.py requires it, no default).
#
# Usage (pearl, by hand -- the unit does the same):
#   MERLE_MUSIC_DB=/home/todd/project-squirrel/music.db \
#   MERLE_MUSIC_STREAM_BASE=http://192.168.1.64:8090 \
#   python -m uvicorn jukebox.music_daemon:app --host 0.0.0.0 --port 8090 \
#       --no-access-log --timeout-graceful-shutdown 3
#   (--no-access-log: the GUI polls /state; issue #125's lesson applies here
#   before it becomes a flood instead of after. --timeout-graceful-shutdown:
#   the RENDERER holds /stream open for the whole song, so an unbounded
#   graceful drain turns SIGTERM into a zombie that outlives its replacement
#   -- observed on pearl mid-playback; the vision daemon's flag, same reason,
#   different client.)
# =============================================================================

import os
import random
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from xml.sax.saxutils import escape

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

import bus
from jukebox import music_cache, music_catalog, music_playlist

STREAM_CHUNK = 1 << 20

# Poll cadence for the watcher. 2s means a completion's recorded `seconds` is
# at most 2s shy of the truth -- noise against 3-minute tracks, and gentle
# enough that the Denon never notices it's being watched.
WATCH_INTERVAL_S = 2.0

# How many consecutive failed polls before the watcher gives the track up for
# lost (renderer unplugged mid-song). Records a skip at the last known
# position rather than watching a ghost forever.
WATCH_MAX_FAILURES = 3

# What we are willing to hand each output -- POLICY, not discovery (see
# banner). The Denon list is every format the catalog holds: ALAC/m4a is
# spike-verified; mp3/flac/wav are DLNA bread-and-butter. A format the Denon
# unexpectedly gags on shows up as an instant STOPPED in the log and a skip
# row -- visible, not fatal. The browser (Phase 2b, issue #149) also takes
# everything -- raw bytes where it decodes natively, the FLAC repack where it
# doesn't (music_cache.needs_flac is that policy's second half, decided from
# the catalog's codec column, never by sniffing).
OUTPUT_FORMATS = {
    "denon": {"m4a", "mp4", "mp3", "flac", "wav"},
    "browser": {"m4a", "mp4", "mp3", "flac", "wav"},
}

# Outputs the daemon drives over UPnP. The browser is deliberately NOT one:
# its transport is the <audio> element in the listener's tab, so /play aims
# only renderers, the GUI plays browser tracks itself off /stream, and
# /report is how those plays reach play_history (the watcher can't poll a
# browser tab).
RENDERER_OUTPUTS = {"denon"}

# The renderer /play matches against discovery, by friendlyName substring
# (case-insensitive). The Denon's own name is "Denon AVR-X4000"; matching the
# brand survives a rename to "Living room Denon" but not a second Denon --
# an actual second one earns config, not a smarter guess.
DENON_NAME_FRAGMENT = "denon"

CONTENT_TYPES = {
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "wav": "audio/wav",
}

# DLNA header pair on every stream response: harmless to browsers, and some
# renderers refuse a source that doesn't declare OP=01 (Range supported).
DLNA_HEADERS = {
    "transferMode.dlna.org": "Streaming",
    "contentFeatures.dlna.org":
        "DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000",
}

SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_SEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 3\r\n"
    "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
    "\r\n"
).encode()


# --- pure: the parsing and shaping the tests pin down --------------------------

def parse_range(header, size):
    """An HTTP Range header -> (start, end) byte positions, both inclusive.

    Returns None for "serve the whole file": no header, a malformed one, or a
    multi-range request (RFC 7233 allows ignoring Range outright, which beats
    implementing multipart/byteranges for a client that doesn't exist).
    Raises ValueError for a syntactically fine but unsatisfiable range --
    the caller's 416, which per the RFC must carry the file size.

    The suffix form ("bytes=-500", the LAST 500 bytes) is real: renderers use
    it to read a trailing index. Getting it wrong plays static."""
    if not header or size <= 0:
        if header and size <= 0:
            raise ValueError("empty file has no satisfiable range")
        return None
    m = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*", header)
    if not m:
        return None  # malformed or multi-range: whole file, status 200
    first, last = m.group(1), m.group(2)
    if first == "" and last == "":
        return None
    if first == "":  # suffix: last N bytes
        n = int(last)
        if n == 0:
            raise ValueError("zero-length suffix")
        return (max(0, size - n), size - 1)
    start = int(first)
    if start >= size:
        raise ValueError("range starts past EOF")
    end = int(last) if last != "" else size - 1
    return (start, min(end, size - 1))


def content_type_for(fmt):
    """The MIME type we declare for a catalog format. audio/mp4 for m4a is
    load-bearing: it's what the Denon accepted for ALAC in the spike."""
    return CONTENT_TYPES.get(fmt, "application/octet-stream")


def didl_for(title, artist, url, mime):
    """The DIDL-Lite metadata for SetAVTransportURI. Renderers commonly
    reject a bare URI with no metadata, and the protocolInfo's MIME is where
    a format-refusing renderer says no CLEANLY (an error on set, not silence
    after play). Escaped field by field: this library really does hold
    titles with & and <."""
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="1" parentID="0" restricted="1">'
        "<dc:title>%s</dc:title>"
        "<upnp:artist>%s</upnp:artist>"
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        '<res protocolInfo="http-get:*:%s:DLNA.ORG_OP=01;'
        'DLNA.ORG_FLAGS=01700000000000000000000000000000">%s</res>'
        "</item></DIDL-Lite>"
        % (escape(title or "Unknown"), escape(artist or "Unknown"),
           mime, escape(url))
    )


def outcome_for(position_s, duration_s):
    """How a play ended, from the last position observed while it was still
    playing. Completed means "reached the end, give or take the poll": within
    10s or 90% of the duration, whichever is more forgiving on short tracks.
    Everything else -- including an unknown duration, where 'the end' is
    unknowable -- is a skip, because implicit feedback must err toward NOT
    crediting a listen that didn't happen."""
    if not duration_s or duration_s <= 0 or position_s is None:
        return music_catalog.PLAY_SKIPPED
    threshold = min(duration_s - 10.0, duration_s * 0.9)
    if position_s >= max(0.0, threshold):
        return music_catalog.PLAY_COMPLETED
    return music_catalog.PLAY_SKIPPED


def hms(seconds):
    """193.4 -> "0:03:13", the AVTransport REL_TIME wire format."""
    s = max(0, int(seconds))
    return "%d:%02d:%02d" % (s // 3600, (s % 3600) // 60, s % 60)


def parse_hms(text):
    """"0:03:13" -> 193.0; None for the not-answers renderers actually send
    ("NOT_IMPLEMENTED", "", garbage). A None position is 'unknown', which
    outcome_for treats as a skip -- never a crash."""
    if not text:
        return None
    m = re.fullmatch(r"(\d+):(\d\d?):(\d\d?)(?:\.\d+)?", text.strip())
    if not m:
        return None
    h, mnt, s = (int(g) for g in m.groups())
    return float(h * 3600 + mnt * 60 + s)


# --- the SOAP half: driving a renderer ------------------------------------------

class Renderer:
    """One discovered UPnP MediaRenderer: a name, an AVTransport control URL,
    and the five verbs the daemon needs. Control URLs come from the device's
    OWN description XML, never hardcoded -- the epic's warning (the LG embeds
    its UUID in service paths; a factory reset moves them), and the Denon
    already moved once between the spike's probes."""

    def __init__(self, name, avtransport_url):
        self.name = name
        self.avtransport_url = avtransport_url

    AVT = "urn:schemas-upnp-org:service:AVTransport:1"

    def _soap(self, action, args=""):
        body = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            "<s:Body>"
            '<u:%s xmlns:u="%s"><InstanceID>0</InstanceID>%s</u:%s>'
            "</s:Body></s:Envelope>" % (action, self.AVT, args, action)
        ).encode()
        req = urllib.request.Request(self.avtransport_url, data=body,
                                     method="POST")
        req.add_header("Content-Type", 'text/xml; charset="utf-8"')
        req.add_header("SOAPACTION", '"%s#%s"' % (self.AVT, action))
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.read().decode("utf-8", "replace")

    def set_uri(self, url, didl):
        self._soap("SetAVTransportURI",
                   "<CurrentURI>%s</CurrentURI>"
                   "<CurrentURIMetaData>%s</CurrentURIMetaData>"
                   % (escape(url), escape(didl)))

    def play(self):
        self._soap("Play", "<Speed>1</Speed>")

    def pause(self):
        self._soap("Pause")

    def stop(self):
        self._soap("Stop")

    def seek(self, seconds):
        self._soap("Seek", "<Unit>REL_TIME</Unit><Target>%s</Target>"
                   % hms(seconds))

    def transport_state(self):
        r = self._soap("GetTransportInfo")
        m = re.search(r"<CurrentTransportState>(.*?)</CurrentTransportState>",
                      r, re.S)
        return m.group(1).strip() if m else "UNKNOWN"

    def position(self):
        """(position_s, duration_s), either None when the renderer won't say."""
        r = self._soap("GetPositionInfo")
        rel = re.search(r"<RelTime>(.*?)</RelTime>", r, re.S)
        dur = re.search(r"<TrackDuration>(.*?)</TrackDuration>", r, re.S)
        return (parse_hms(rel.group(1)) if rel else None,
                parse_hms(dur.group(1)) if dur else None)


def discover_renderers(timeout=4.0):
    """SSDP M-SEARCH for MediaRenderers -> [Renderer]. Best-effort by design:
    a powered-off renderer simply isn't in the list, and /play answers 503
    until a rediscovery finds it. Never raises -- discovery failing must not
    stop the daemon from serving /stream."""
    found = {}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.settimeout(timeout)
        s.sendto(SSDP_SEARCH, SSDP_ADDR)
        while True:
            try:
                data, addr = s.recvfrom(65535)
            except socket.timeout:
                break
            m = re.search(r"(?im)^LOCATION:\s*(\S+)",
                          data.decode("utf-8", "replace"))
            if m and addr[0] not in found:
                found[addr[0]] = m.group(1)
        s.close()
    except OSError as e:
        print("[music] SSDP discovery failed: %s" % e)
        return []

    renderers = []
    for ip, loc in sorted(found.items()):
        try:
            with urllib.request.urlopen(loc, timeout=5) as resp:
                xml = resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as e:
            print("[music] %s: description fetch failed: %s" % (ip, e))
            continue
        name_m = re.search(r"<friendlyName>(.*?)</friendlyName>", xml, re.S)
        name = name_m.group(1).strip() if name_m else ip
        base = loc.rsplit("/", 1)[0] if "://" in loc else loc
        # The description's own base wins when present; otherwise URLBase or
        # the description URL's origin.
        origin = re.match(r"(https?://[^/]+)", loc)
        base = origin.group(1) if origin else base
        ctrl = None
        for svc in re.finditer(r"<service>(.*?)</service>", xml, re.S):
            body = svc.group(1)
            st = re.search(r"<serviceType>(.*?)</serviceType>", body, re.S)
            cu = re.search(r"<controlURL>(.*?)</controlURL>", body, re.S)
            if st and cu and "AVTransport" in st.group(1):
                path = cu.group(1).strip()
                ctrl = path if "://" in path else base + path
        if ctrl:
            renderers.append(Renderer(name, ctrl))
            print("[music] discovered renderer: %s -> %s" % (name, ctrl))
    return renderers


def pick_denon(renderers):
    for r in renderers:
        if DENON_NAME_FRAGMENT in r.name.lower():
            return r
    return None


# --- the player: one current track, one watcher, one history row per play ------

class Player:
    """The daemon's whole mutable state: which track is on which output, and
    the watcher that turns transport changes into play_history rows. One
    lock over both the state and the DB writes -- FastAPI's sync endpoints
    run in a threadpool, the watcher is its own thread, and SQLite is shared."""

    def __init__(self, conn, renderer, stream_base, browser_available=False):
        self.conn = conn
        self.renderer = renderer
        self.stream_base = stream_base.rstrip("/")
        # Whether the cache is configured (issue #149). The browser output
        # is offered exactly when it is: without ffmpeg's landing pad the
        # ALAC majority can't play, and a 62%-broken output in the picker
        # would be a lie wearing a checkmark.
        self.browser_available = browser_available
        self.lock = threading.Lock()
        self.current = None  # {track_id,title,artist,album,duration_s,output}
        self.last_pos = None  # last position observed while PLAYING
        self._failures = 0
        self._stop_evt = threading.Event()
        self._thread = None

    # -- lifecycle

    def start_watcher(self):
        self._thread = threading.Thread(target=self._watch, daemon=True,
                                        name="music-watcher")
        self._thread.start()

    def close(self):
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=WATCH_INTERVAL_S * 2)

    # -- the verbs (called from endpoints; each returns (status, payload))

    def play(self, track_id, output):
        if output not in OUTPUT_FORMATS:
            return 422, {"error": "unknown output: %s" % output}
        if output not in RENDERER_OUTPUTS:
            # The browser's transport is the listener's own <audio> element;
            # there is nothing here to drive (see RENDERER_OUTPUTS).
            return 422, {"error": "%s plays client-side -- stream directly "
                                  "and POST /report" % output}
        if not music_catalog.valid_track_id(track_id):
            return 400, {"error": "malformed track id"}
        with self.lock:
            info = music_catalog.track_info(self.conn, track_id)
            if info is None:
                return 404, {"error": "unknown track"}
            if info["format"] not in OUTPUT_FORMATS[output]:
                return 415, {"error": "%s does not play %s"
                             % (output, info["format"])}
            loc = music_catalog.file_for_track(self.conn, track_id)
            if loc is None:
                return 404, {"error": "track has no file"}
            if self.renderer is None:
                self.renderer = pick_denon(discover_renderers())
                if self.renderer is None:
                    return 503, {"error": "no renderer found -- is the Denon "
                                          "powered on?"}
            # Moving on mid-track is the skip signal (implicit feedback).
            self._record_outcome_locked()
            url = "%s/stream/%s" % (self.stream_base, track_id)
            didl = didl_for(info["title"], info["artist"], url,
                            content_type_for(info["format"]))
            try:
                self.renderer.set_uri(url, didl)
                self.renderer.play()
            except (urllib.error.URLError, OSError, ValueError) as e:
                print("[music] play failed: %s" % e)
                return 502, {"error": "renderer refused: %s" % e}
            self.current = dict(info, output=output)
            self.last_pos = 0.0
            self._failures = 0
            print("[music] playing %s -- %s (%s) on %s"
                  % (info["artist"], info["title"], track_id, output))
            return 200, self._state_locked()

    def resume(self):
        with self.lock:
            if self.current is None or self.renderer is None:
                return 409, {"error": "nothing to resume"}
            try:
                self.renderer.play()
            except (urllib.error.URLError, OSError) as e:
                return 502, {"error": str(e)}
            return 200, self._state_locked()

    def pause(self):
        with self.lock:
            if self.current is None or self.renderer is None:
                return 409, {"error": "nothing playing"}
            try:
                self.renderer.pause()
            except (urllib.error.URLError, OSError) as e:
                return 502, {"error": str(e)}
            return 200, self._state_locked()

    def stop(self):
        with self.lock:
            if self.current is None:
                return 200, self._state_locked()
            try:
                if self.renderer is not None:
                    self.renderer.stop()
            except (urllib.error.URLError, OSError) as e:
                print("[music] stop: renderer already gone (%s)" % e)
            self._record_outcome_locked()
            return 200, self._state_locked()

    def seek(self, seconds):
        with self.lock:
            if self.current is None or self.renderer is None:
                return 409, {"error": "nothing playing"}
            try:
                self.renderer.seek(seconds)
            except (urllib.error.URLError, OSError) as e:
                return 502, {"error": str(e)}
            return 200, self._state_locked()

    def state(self):
        with self.lock:
            return self._state_locked()

    # -- internals (call with self.lock held)

    def _state_locked(self):
        transport = "NO_MEDIA_PRESENT"
        position = None
        if self.current is not None and self.renderer is not None:
            # Read live but never let a sulking renderer break /state -- the
            # GUI polls this; it must degrade to last-known, not 500.
            try:
                transport = self.renderer.transport_state()
                pos, _ = self.renderer.position()
                if pos is not None:
                    position = pos
                    if transport == "PLAYING":
                        self.last_pos = pos
            except (urllib.error.URLError, OSError):
                transport = "UNREACHABLE"
                position = self.last_pos
        out = {
            "transport": transport,
            "position_s": position,
            "track": self.current,
            # The GUI builds browser <audio> srcs from this -- the same
            # LAN-visible address the Denon fetches from, which is exactly
            # the property the browser needs too. Served here rather than
            # baked into a client bundle (the proxy-route rule).
            "stream_base": self.stream_base,
            "outputs": [
                {"id": "denon",
                 "name": self.renderer.name if self.renderer
                 else "Denon (not found)",
                 "kind": "dlna",
                 "available": self.renderer is not None},
                {"id": "browser",
                 "name": "This browser",
                 "kind": "local",
                 "available": self.browser_available},
            ],
        }
        return out

    def _record_outcome_locked(self):
        """Close the book on the current track, if any: one history row,
        outcome judged from the last position seen while playing."""
        if self.current is None:
            return
        outcome = outcome_for(self.last_pos, self.current.get("duration_s"))
        music_catalog.record_play(
            self.conn, self.current["id"], int(time.time()), outcome,
            seconds=self.last_pos, output=self.current["output"])
        print("[music] %s: %s -- %s at %.0fs"
              % (outcome, self.current["artist"], self.current["title"],
                 self.last_pos or 0))
        self.current = None
        self.last_pos = None

    def _watch(self):
        """Poll the transport while a track is loaded; when it stops, decide
        completed-vs-skipped and write the row. This thread is why history
        doesn't depend on anyone's browser tab staying open."""
        while not self._stop_evt.wait(WATCH_INTERVAL_S):
            with self.lock:
                if self.current is None or self.renderer is None:
                    continue
                try:
                    transport = self.renderer.transport_state()
                    pos, _ = self.renderer.position()
                    self._failures = 0
                except (urllib.error.URLError, OSError) as e:
                    self._failures += 1
                    if self._failures >= WATCH_MAX_FAILURES:
                        print("[music] renderer lost mid-track (%s)" % e)
                        self._record_outcome_locked()
                        self.renderer = None
                    continue
                if transport == "PLAYING" and pos is not None:
                    self.last_pos = pos
                elif transport == "STOPPED":
                    # Natural end (or a stop from the AVR's own remote --
                    # same fact: the listener's session with this track is
                    # over). Position already reset; last_pos has the truth.
                    self._record_outcome_locked()


# --- the queue half: candidates for the Phase 3 engine ---------------------------

# How many tracks a /queue call returns unless asked, and the most it will
# ever return. The GUI refills in windows, so a bigger n buys fewer refills
# at the cost of staler exclusions.
QUEUE_DEFAULT_N = 25
QUEUE_MAX_N = 100

# What each queued track carries on the wire: exactly the GUI's TrackRow shape
# (music/lib/catalog-rows.ts), so the client feeds responses through the same
# tested trackFromRow mapper every other surface uses. Scoring fields stay
# server-side -- the GUI has no business rendering raw BPM as precise
# (measured fact 1 in music_playlist.py).
QUEUE_TRACK_KEYS = ("id", "title", "artist", "album", "album_artist",
                    "track_no", "duration_s", "format", "bitrate",
                    "samplerate", "rating")


def queue_candidates(conn, now):
    """Every track the engine may consider, as plain dicts. THE HARD FILTERS
    LIVE IN THIS WHERE CLAUSE, not in the scorer (epic principle 4 -- the ban
    is enforced by the query, not entrusted to arithmetic):

      - strong-down (-2) never enters the candidate set;
      - anything played inside the cooldown window is out (the horizon is the
        engine's COOLDOWN_HOURS -- one constant, shared, so the query and the
        pure rule cannot drift);
      - bpm IS NOT NULL -- the engine scores mood axes, and a track that
        predates analysis has none;
      - the track has a location the daemon can actually stream."""
    cutoff = now - music_playlist.COOLDOWN_HOURS * 3600.0
    rows = conn.execute(
        "SELECT t.id, t.title, t.artist, t.album, t.album_artist, "
        "t.track_no, t.duration_s, t.format, t.bitrate, t.samplerate, "
        "t.bpm, t.replaygain_db, t.dynamic_range_db, t.year, t.genre, "
        "r.value AS rating "
        "FROM tracks t LEFT JOIN ratings r ON r.track_id = t.id "
        "WHERE t.bpm IS NOT NULL "
        "AND (r.value IS NULL OR r.value != ?) "
        "AND t.id NOT IN (SELECT track_id FROM play_history "
        "                 WHERE played_at >= ?) "
        "AND EXISTS (SELECT 1 FROM track_files f WHERE f.track_id = t.id)",
        (music_catalog.RATING_STRONG_DOWN, cutoff)).fetchall()
    return [dict(r) for r in rows]


def seed_track_row(conn, track_id):
    """The feature row a track seed resolves from -- fetched OUTSIDE the
    candidate filters on purpose: the seed is usually the track playing right
    now, which the cooldown just excluded. Its features are still the target;
    it just can't be in the queue (resolve_seed excludes it)."""
    row = conn.execute(
        "SELECT id, bpm, replaygain_db, dynamic_range_db, year, genre "
        "FROM tracks WHERE id = ?", (track_id,)).fetchone()
    return dict(row) if row else None


# --- serving bytes: the one Range-aware file response ----------------------------

def file_response(path, request, media, label):
    """Serve one on-disk file with the hand-rolled Range support (see banner
    -- non-negotiable, and now shared: the Denon's untouched catalog bytes and
    the browser's cached FLAC ride the SAME code, so the Range behavior the
    spike validated cannot drift between outputs). Returns None when the file
    vanished -- the caller's 404, since what that means differs by branch.

    DLNA headers ride on every response: required by renderers, harmless to
    browsers (the spike's finding, unchanged)."""
    try:
        size = os.path.getsize(path)  # trust the fs over a stale catalog
    except OSError:
        return None
    try:
        rng = parse_range(request.headers.get("range"), size)
    except ValueError:
        return Response(status_code=416,
                        headers={"Content-Range": "bytes */%d" % size})
    start, end = rng if rng else (0, size - 1)
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        **DLNA_HEADERS,
    }
    if rng:
        headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
    status = 206 if rng else 200
    if request.method == "HEAD":
        return Response(status_code=status, headers=headers, media_type=media)
    print("[music] stream %s %s bytes %d-%d"
          % (label, "(range)" if rng else "(full)", start, end))

    def body(path=path, start=start, end=end):
        # Principle 1 is enforced, not trusted: "rb" against an ro mount.
        with open(path, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(STREAM_CHUNK, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk

    return StreamingResponse(body(), status_code=status, headers=headers,
                             media_type=media)


# The most track ids one /precache accepts. The GUI warms the next two; the
# cap just keeps a buggy caller from enqueueing the whole library.
PRECACHE_MAX = 8


# --- the app --------------------------------------------------------------------

def create_app(conn=None, renderer=None, stream_base=None,
               publisher_factory=None, cache=None):
    """Build the app. Everything injectable so tests run with a :memory:
    catalog, a fake renderer, a fake cache, and no network; production passes
    nothing and the lifespan reads env -- loudly, at startup, which is where
    a missing MERLE_MUSIC_DB must kill the daemon rather than 404 every track
    from an accidentally-created empty file."""

    injected = dict(conn=conn, renderer=renderer, stream_base=stream_base,
                    cache=cache)
    state = {"player": None, "publisher": None, "cache": None}

    async def lifespan(app):
        db = injected["conn"]
        if db is None:
            path = music_catalog.db_path()
            if not os.path.isfile(path):
                raise RuntimeError(
                    "MERLE_MUSIC_DB does not exist: %s -- the indexer owns "
                    "creating the catalog; refusing to serve an empty one."
                    % path)
            db = music_catalog.connect(path)
            print("[music] catalog: %s -- %r"
                  % (path, music_catalog.counts(db)))
        base = injected["stream_base"] or \
            os.environ.get("MERLE_MUSIC_STREAM_BASE", "").strip()
        if not base:
            raise RuntimeError(
                "MERLE_MUSIC_STREAM_BASE is not set. The renderer fetches "
                "audio FROM this daemon, so it must know its own LAN-visible "
                "URL -- e.g. http://192.168.1.64:8090.")
        rnd = injected["renderer"]
        if rnd is None:
            rnd = pick_denon(discover_renderers())
            if rnd is None:
                print("[music] no renderer found at startup -- will retry "
                      "on first /play (is the Denon on standby?)")
        player = Player(db, rnd, base)
        player.start_watcher()
        state["player"] = player

        # The FLAC cache (issue #149). Unset MERLE_MUSIC_CACHE is the kill
        # switch -- the Denon path runs on a bare checkout, and the picker
        # simply never offers the browser. SET but missing is a config error
        # and dies loudly: the LV didn't mount, and "playable but 62% broken"
        # must not look healthy. Built after the player because the sweep's
        # view of the catalog goes through the player's lock -- SQLite is
        # shared with the watcher and every endpoint, one lock for all of it.
        mcache = injected["cache"]
        if mcache is None:
            croot = music_cache.cache_root()
            if croot:
                if not os.path.isdir(croot):
                    raise RuntimeError(
                        "MERLE_MUSIC_CACHE does not exist: %s -- is the "
                        "media-cache LV mounted? (Servers/Pearl.md)" % croot)

                def expected_names():
                    with player.lock:
                        rows = player.conn.execute(
                            "SELECT id FROM tracks").fetchall()
                    return {music_cache.cache_name(r["id"]) for r in rows}

                mcache = music_cache.MusicCache(
                    croot, music_cache.cache_cap_bytes(), expected_names)
                print("[music] cache: %s (cap %.0f GB)"
                      % (croot, mcache.cap_bytes / (1 << 30)))
                # Startup sweep: yesterday's orphans and any crashed .part
                # go now, not on the first transcode.
                mcache.sweep()
            else:
                print("[music] MERLE_MUSIC_CACHE unset -- browser output "
                      "disabled")
        state["cache"] = mcache
        player.browser_available = mcache is not None
        if publisher_factory is not None:
            state["publisher"] = publisher_factory()
        else:
            state["publisher"] = bus.EventPublisher(
                "music", status_topic=bus.MUSIC_STATUS_TOPIC).start()
        yield
        player.close()
        if hasattr(state["publisher"], "close"):
            state["publisher"].close()

    app = FastAPI(title="Merle music daemon", lifespan=lifespan)

    def player():
        return state["player"]

    @app.get("/state")
    def get_state():
        return player().state()

    @app.post("/play")
    def post_play(cmd: dict):
        # {track_id, output} starts a track; {} resumes a pause. Two verbs on
        # one endpoint because that's how the GUI's toggle actually thinks.
        if not cmd.get("track_id"):
            status, payload = player().resume()
        else:
            status, payload = player().play(cmd["track_id"],
                                            cmd.get("output", "denon"))
        return JSONResponse(payload, status_code=status)

    @app.post("/pause")
    def post_pause():
        status, payload = player().pause()
        return JSONResponse(payload, status_code=status)

    @app.post("/stop")
    def post_stop():
        status, payload = player().stop()
        return JSONResponse(payload, status_code=status)

    @app.post("/seek")
    def post_seek(cmd: dict):
        try:
            seconds = float(cmd.get("seconds"))
        except (TypeError, ValueError):
            return JSONResponse({"error": "seconds must be a number"},
                                status_code=422)
        status, payload = player().seek(seconds)
        return JSONResponse(payload, status_code=status)

    @app.post("/rate")
    def post_rate(cmd: dict):
        # The thumbs (issue #135). The catalog is pearl's to write and this is
        # its writer -- the music app's own handle is readOnly by construction,
        # so a rating comes here rather than growing a second writer.
        #
        # Guard order is /stream's, and it matters more here because this one
        # writes: allowlist the wire's id BEFORE any query, then 404 an
        # unknown-but-well-formed id (a wrong URL, not a broken daemon), then
        # let music_catalog.rate() adjudicate the value -- RATING_VALUES lives
        # there, and duplicating the legal set here is how the two drift.
        #
        # value 0 is the control's THIRD CLICK, which clears (lib/rating.ts's
        # nextRating). It's a legal thing to send and an illegal thing to
        # store, so it dispatches to unrate() here rather than teaching the
        # store a zero it would have to ignore forever after.
        track_id = cmd.get("track_id")
        if not isinstance(track_id, str) or \
                not music_catalog.valid_track_id(track_id):
            return JSONResponse({"error": "malformed track id"},
                                status_code=400)
        value = cmd.get("value")
        # bool subclasses int, so `true` would otherwise sail through as +1.
        if isinstance(value, bool) or not isinstance(value, int):
            return JSONResponse({"error": "value must be an integer"},
                                status_code=400)
        p = player()
        with p.lock:
            if music_catalog.track_info(p.conn, track_id) is None:
                return JSONResponse({"error": "unknown track"},
                                    status_code=404)
            if value == 0:
                music_catalog.unrate(p.conn, track_id)
            else:
                try:
                    # The timestamp is ours, never the client's: this is the
                    # one table we cannot rebuild, and a browser clock is a
                    # guess.
                    music_catalog.rate(p.conn, track_id, value,
                                       int(time.time()))
                except ValueError as e:
                    return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"track_id": track_id, "value": value})

    @app.post("/queue")
    def post_queue(cmd: dict):
        # The playlist engine's one door (issue #139). Generates a list, full
        # stop: it does not start playback, does not touch the renderer, and
        # holds no queue state -- the daemon stays one-track-at-a-time on the
        # transport verbs, and the GUI owns the queue it fetches.
        #
        # Guard order is /rate's: allowlist every wire id BEFORE any query,
        # 404 an unknown-but-well-formed seed track, and let the pure engine
        # decline everything else (an unknown artist is an empty queue, not
        # an error -- there is nothing wrong with the request but the taste).
        seed = cmd.get("seed")
        if not isinstance(seed, dict):
            return JSONResponse({"error": "seed must be an object"},
                                status_code=400)
        track_id = seed.get("track_id")
        artist = seed.get("artist")
        if track_id is not None:
            if not isinstance(track_id, str) or \
                    not music_catalog.valid_track_id(track_id):
                return JSONResponse({"error": "malformed track id"},
                                    status_code=400)
        elif not (isinstance(artist, str) and artist.strip()):
            return JSONResponse(
                {"error": "seed needs a track_id or an artist"},
                status_code=400)
        n = cmd.get("n", QUEUE_DEFAULT_N)
        if isinstance(n, bool) or not isinstance(n, int) \
                or not 1 <= n <= QUEUE_MAX_N:
            return JSONResponse({"error": "n must be an integer in 1..%d"
                                          % QUEUE_MAX_N}, status_code=400)
        exclude = cmd.get("exclude", [])
        if not isinstance(exclude, list) or any(
                not isinstance(x, str) or not music_catalog.valid_track_id(x)
                for x in exclude):
            return JSONResponse({"error": "exclude must be a list of "
                                          "track ids"}, status_code=400)
        now = time.time()
        p = player()
        with p.lock:
            if track_id is not None:
                row = seed_track_row(p.conn, track_id)
                if row is None:
                    return JSONResponse({"error": "unknown track"},
                                        status_code=404)
                engine_seed = {"track": row}
            else:
                engine_seed = {"artist": artist.strip()}
            candidates = queue_candidates(p.conn, now)
        # Production entropy; tests exercise determinism against the pure
        # engine directly with a seeded RNG.
        tracks = music_playlist.build_queue(candidates, engine_seed, n,
                                            random.Random(), now,
                                            exclude_ids=exclude)
        print("[music] queue: %s -> %d tracks"
              % (dict(seed), len(tracks)))
        return {"tracks": [{k: t.get(k) for k in QUEUE_TRACK_KEYS}
                           for t in tracks]}

    @app.api_route("/stream/{track_id}", methods=["GET", "HEAD"])
    def stream(track_id: str, request: Request):
        # The allowlist runs BEFORE the catalog is asked -- a hostile id
        # never reaches a query, let alone the filesystem (do-not-change:
        # frame_archiver's guard genre).
        if not music_catalog.valid_track_id(track_id):
            return Response(status_code=400, content="malformed track id")
        # ?output= picks the per-output policy (issue #149). Default is the
        # Denon so every URL the daemon ever handed a renderer -- including
        # one it's holding open right now -- means exactly what it did in 2a.
        output = request.query_params.get("output", "denon")
        if output not in OUTPUT_FORMATS:
            return Response(status_code=422,
                            content="unknown output: %s" % output)
        p = player()
        with p.lock:
            info = music_catalog.track_info(p.conn, track_id)
            loc = music_catalog.file_for_track(p.conn, track_id)
        if info is None or loc is None:
            return Response(status_code=404, content="unknown track")
        if info["format"] not in OUTPUT_FORMATS[output]:
            return Response(status_code=415, content="%s does not play %s"
                            % (output, info["format"]))
        path = loc["path"]

        if output == "browser" and music_cache.needs_flac(info["format"],
                                                          info.get("codec")):
            cache = state["cache"]
            if cache is None:
                # The kill switch (MERLE_MUSIC_CACHE unset). The GUI never
                # offers the output in this state, so reaching here is a
                # hand-built URL -- refuse it plainly.
                return Response(status_code=503,
                                content="browser output not configured")
            if request.method == "HEAD":
                # A probe must not cost a transcode: answer from the cache
                # if it's there, and say 200-no-length otherwise (the GET
                # will be chunked anyway).
                hit = cache.lookup(track_id)
                if hit:
                    resp = file_response(hit, request, "audio/flac",
                                         "%s->%s" % (track_id, output))
                    if resp is not None:
                        return resp
                return Response(status_code=200, media_type="audio/flac")
            kind, val = cache.ensure(track_id, path)
            if kind == "file":
                resp = file_response(val, request, "audio/flac",
                                     "%s->%s (cache)" % (track_id, output))
                if resp is not None:
                    return resp
                return Response(status_code=404, content="cache entry lost")
            # Cold click: tail the transcode as it runs. 200 and chunked --
            # the final size is unknowable until the encoder finishes, and
            # ignoring Range outright is the RFC-blessed move (parse_range's
            # reasoning; a post-completion seek Range-requests the finished
            # file and gets a real 206).
            print("[music] stream %s->%s cold -- tailing transcode"
                  % (track_id, output))
            return StreamingResponse(music_cache.iter_growing(val),
                                     status_code=200,
                                     media_type="audio/flac")

        # Everything else -- every Denon stream, and the browser's natively
        # decodable majority-minority -- is catalog bytes UNTOUCHED.
        resp = file_response(path, request, content_type_for(info["format"]),
                             "%s->%s" % (track_id, output))
        if resp is None:
            print("[music] stream: file missing: %s" % path)
            return Response(status_code=404, content="file missing")
        return resp

    @app.post("/report")
    def post_report(cmd: dict):
        # The browser's half of play-history (issue #149): the watcher polls
        # the Denon, but a browser tab can't be polled, so it reports its own
        # session's end -- once per play, position at the moment it ended.
        # The OUTCOME is still adjudicated HERE, by the same outcome_for the
        # watcher uses: the client says where playback stopped, never what it
        # meant -- one arbiter, two transports, and a clock that's ours
        # (/rate's reasoning: the history table is irreplaceable and a
        # browser clock is a guess).
        track_id = cmd.get("track_id")
        if not isinstance(track_id, str) or \
                not music_catalog.valid_track_id(track_id):
            return JSONResponse({"error": "malformed track id"},
                                status_code=400)
        pos = cmd.get("position_s")
        # bool subclasses int (the /rate lesson); None is legal -- "position
        # unknown" -- and outcome_for already treats it as a skip.
        if pos is not None and (isinstance(pos, bool)
                                or not isinstance(pos, (int, float))):
            return JSONResponse({"error": "position_s must be a number"},
                                status_code=400)
        p = player()
        with p.lock:
            info = music_catalog.track_info(p.conn, track_id)
            if info is None:
                return JSONResponse({"error": "unknown track"},
                                    status_code=404)
            outcome = outcome_for(
                float(pos) if pos is not None else None,
                info["duration_s"])
            music_catalog.record_play(p.conn, track_id, int(time.time()),
                                      outcome, seconds=pos, output="browser")
        print("[music] %s (browser): %s -- %s at %.0fs"
              % (outcome, info["artist"], info["title"], pos or 0))
        return {"track_id": track_id, "outcome": outcome}

    @app.post("/precache")
    def post_precache(cmd: dict):
        # Queue warming (issue #149): the GUI names the next tracks and the
        # cache repacks them while the current one plays, so a queue advance
        # is always a hit and the transcode delay is unobservable in playlist
        # listening. Best-effort THROUGHOUT: an unknown id, a raw-bytes
        # format, an already-cached track are all skips, not errors --
        # warming is a nicety and must never make the GUI handle failure.
        cache = state["cache"]
        if cache is None:
            return JSONResponse({"error": "browser output not configured"},
                                status_code=503)
        ids = cmd.get("track_ids")
        if not isinstance(ids, list) or len(ids) > PRECACHE_MAX or any(
                not isinstance(x, str) or not music_catalog.valid_track_id(x)
                for x in ids):
            return JSONResponse({"error": "track_ids must be up to %d track "
                                          "ids" % PRECACHE_MAX},
                                status_code=400)
        p = player()
        queued = 0
        for tid in ids:
            with p.lock:
                info = music_catalog.track_info(p.conn, tid)
                loc = music_catalog.file_for_track(p.conn, tid)
            if info is None or loc is None:
                continue
            if not music_cache.needs_flac(info["format"], info.get("codec")):
                continue
            kind, _ = cache.ensure(tid, loc["path"], background=True)
            if kind == "job":
                queued += 1
        if queued:
            print("[music] precache: %d transcode(s) queued" % queued)
        return {"queued": queued}

    return app


# Assembled lazily by the lifespan (env reads happen at uvicorn startup, not
# import) -- so `pytest` can import this module and build its own app with
# everything injected, while `python -m uvicorn jukebox.music_daemon:app`
# still fails loudly on missing config before it serves a single request.
app = create_app()
