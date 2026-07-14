# =============================================================================
# project-squirrel -- frame_archiver.py
#
# The still-shot filing clerk (issue #90): a tiny pearl-resident service that
# subscribes to the frame topics (driveway/frames/<frame_id>/{full,thumb} --
# raw JPEG bytes, published fire-and-forget by the daemon at event time) and
# writes them to disk, where the MCC's /frames route can always reach them.
#
# Why pearl and not the daemon's HTTP surface: daemon-down is the STEADY STATE
# for the 24/7 dashboard -- the Field Journal deliberately rehydrates from the
# broker's retained topics while bluejay sleeps, and its thumbnails must
# survive the same nap. The bytes therefore live where the MCC lives (pearl's
# local disk now; when the USB NAS arrives, migration is repointing
# MERLE_FRAMES_DIR at the mount -- nothing else changes).
#
# Contract, same ethos as the bus: a dropped frame (broker down, missed
# message, failed write) is a moment nobody archived -- the event row in
# SQLite still exists, frame_id and all -- never a lost record. Retention is
# a rolling window (MERLE_FRAMES_KEEP_DAYS, default 14): the journal shows a
# quiet placeholder for anything pruned.
#
# Config (the bus.py conventions):
#   MERLE_MQTT              required, no default -- the broker's host[:port]
#   MERLE_FRAMES_DIR        where the JPEGs land (default: frames/ under the
#                           working directory -- the weather_history.json
#                           pattern; the systemd unit's WorkingDirectory is
#                           the repo checkout)
#   MERLE_FRAMES_KEEP_DAYS  retention window in days (default 14)
#
# Runs on pearl as the frame-archiver unit -- see Servers/Pearl.md.
# =============================================================================

import os
import re
import time

import paho.mqtt.client as mqtt

import bus

DEFAULT_FRAMES_DIR = "frames"
KEEP_DAYS_DEFAULT = 14.0
PRUNE_INTERVAL_S = 3600.0   # prune hourly; retention is day-grained anyway

# The filename guard: topic-derived ids may only be [A-Za-z0-9_-]. No dots
# (kills "..", and the daemon never mints them), no slashes or backslashes
# (path separators on either OS), nothing a filesystem could interpret. The
# daemon's mint_frame_id is safe by construction, but the bus is a shared
# room -- never trust the wire.
SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")


def keep_days():
    """MERLE_FRAMES_KEEP_DAYS: unset/blank means the default; a malformed
    value fails at startup (the env_float convention -- never run
    half-configured while looking healthy)."""
    raw = os.environ.get("MERLE_FRAMES_KEEP_DAYS", "").strip()
    return float(raw) if raw else KEEP_DAYS_DEFAULT


def frames_dir():
    return os.environ.get("MERLE_FRAMES_DIR", "").strip() or DEFAULT_FRAMES_DIR


def frame_filename(topic):
    """The archive filename for one frame topic, or None for anything foreign
    or unsafe -- the path-traversal guard. bus.frame_topic_parts() already
    rejects foreign topics, extra slashes, and unknown variants; this adds
    the character allowlist so a hostile id ("..", "a\\b", "%2e%2e") dies
    here, never on the filesystem. full -> <id>.jpg, thumb -> <id>.thumb.jpg
    (the names the MCC's /frames route reads)."""
    parts = bus.frame_topic_parts(topic)
    if parts is None:
        return None
    frame_id, variant = parts
    if not SAFE_ID.fullmatch(frame_id):
        return None
    return f"{frame_id}.jpg" if variant == "full" else f"{frame_id}.thumb.jpg"


def prune_selection(files, now, days):
    """Which archived files are past the retention window: [(name, mtime)]
    -> the names to delete. Pure -- injected clock and listing -- so the
    boundary is testable; a file exactly at the cutoff survives."""
    cutoff = now - days * 86400.0
    return [name for name, mtime in files if mtime < cutoff]


def prune(root, days, now=None):
    """Delete archived frames older than the retention window. Any listing or
    delete failure is logged and skipped -- retention is housekeeping, never
    worth killing the service over."""
    try:
        files = [(e.name, e.stat().st_mtime) for e in os.scandir(root)
                 if e.is_file() and e.name.endswith(".jpg")]
    except OSError as e:
        print(f"[frames] prune skipped, can't list {root}: {e}")
        return
    doomed = prune_selection(files, time.time() if now is None else now, days)
    for name in doomed:
        try:
            os.remove(os.path.join(root, name))
        except OSError as e:
            print(f"[frames] prune failed for {name}: {e}")
    if doomed:
        print(f"[frames] pruned {len(doomed)} files past {days:g} days")


def main():
    root = frames_dir()
    days = keep_days()
    os.makedirs(root, exist_ok=True)
    host, port = bus.broker_address()   # required, no default (bus.py)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id="frame-archiver")

    def on_connect(c, userdata, flags, reason_code, properties):
        # (Re)subscribe on every (re)connect, the narrator pattern.
        c.subscribe(bus.FRAMES_WILDCARD)
        print(f"[frames] filing to {root}/ (keeping {days:g} days), "
              f"listening to {bus.FRAMES_WILDCARD}")

    def on_message(c, userdata, msg):
        # Runs on paho's network thread; a small-file write is instant, so
        # nothing here threatens the MQTT keepalive. Whole-file writes, no
        # temp/rename dance: a torn write needs the process to die mid-write,
        # and the cost is one unreadable JPEG the journal shows a placeholder
        # for -- the same cost as never receiving it.
        name = frame_filename(msg.topic)
        if name is None:
            return   # foreign or hostile topic: not our JSON, not our problem
        try:
            with open(os.path.join(root, name), "wb") as f:
                f.write(msg.payload)
        except OSError as e:
            print(f"[frames] write failed for {name}: {e}")

    client.on_connect = on_connect
    client.on_message = on_message
    # connect() (not connect_async): an archiver with no bus has no job, so
    # fail loudly at launch. Once up, paho auto-reconnects forever.
    client.connect(host, port)
    client.loop_start()
    try:
        while True:
            prune(root, days)
            time.sleep(PRUNE_INTERVAL_S)
    except KeyboardInterrupt:
        # Manual desk runs; under systemd SIGTERM just drops the socket.
        client.loop_stop()
        client.disconnect()
        print("\n[frames] off duty.")


if __name__ == "__main__":
    main()
