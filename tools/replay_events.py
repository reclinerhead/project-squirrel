# =============================================================================
# project-squirrel -- replay_events.py
#
# Rehearsal mode: republish archived events from the SQLite log onto the live
# bus (driveway/events) with their original relative timing, so narration can
# be tuned without waiting for real animals. The narrator can't tell the
# difference -- that's the point of the bus.
#
#   python replay_events.py                          # last 50 events, real time
#   python replay_events.py --last 200 --speed 4     # 4x faster
#   python replay_events.py --kinds arrival,departure
#
# Gaps longer than --max-gap (default 60s) are clamped: the archive spans hours
# of driveway silence, and rehearsal shouldn't.
# =============================================================================

import argparse
import json
import sqlite3
import time
from datetime import datetime

import paho.mqtt.client as mqtt

import bus


def load_events(db_path, last, kinds):
    """The most recent `last` archived events (optionally filtered by kind),
    returned in chronological order for replay."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    query = "SELECT ts, kind, details FROM events"
    params = []
    if kinds:
        query += f" WHERE kind IN ({','.join('?' * len(kinds))})"
        params += kinds
    query += " ORDER BY id DESC LIMIT ?"
    params.append(last)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [{"ts": r["ts"], "kind": r["kind"],
             "details": json.loads(r["details"]) if r["details"] else None}
            for r in reversed(rows)]


def gaps(events, speed, max_gap):
    """Seconds to wait before each event (0 for the first). Pure, so the timing
    math is testable without sleeping."""
    waits = []
    prev = None
    for e in events:
        t = datetime.fromisoformat(e["ts"])
        waits.append(0.0 if prev is None
                     else min(max(0.0, (t - prev).total_seconds()), max_gap) / speed)
        prev = t
    return waits


def main():
    ap = argparse.ArgumentParser(description="Replay archived events onto the bus")
    ap.add_argument("--db", default="merle.db")
    ap.add_argument("--last", type=int, default=50, help="how many recent events (default 50)")
    ap.add_argument("--kinds", help="comma-separated filter, e.g. arrival,departure")
    ap.add_argument("--speed", type=float, default=1.0, help="time multiplier (default 1 = original pace)")
    ap.add_argument("--max-gap", type=float, default=60.0,
                    help="clamp silences longer than this many seconds (default 60)")
    args = ap.parse_args()

    kinds = [k.strip() for k in args.kinds.split(",")] if args.kinds else None
    events = load_events(args.db, args.last, kinds)
    if not events:
        print("Nothing to replay -- the archive has no matching events.")
        return

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="merle-replay")
    host, port = bus.broker_address()
    client.connect(host, port)   # fail loudly: a replay with no bus is nothing
    client.loop_start()

    print(f"Replaying {len(events)} events at {args.speed}x...")
    try:
        for wait, e in zip(gaps(events, args.speed, args.max_gap), events):
            time.sleep(wait)
            # QoS 1 + wait: unlike the daemon's fire-and-forget, a replay's whole
            # job is delivery, so don't outrun the broker.
            client.publish(bus.EVENTS_TOPIC, json.dumps(e), qos=1).wait_for_publish(5)
            print(f"  {e['ts']}  {e['kind']}")
    except KeyboardInterrupt:
        print("\nReplay stopped.")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
