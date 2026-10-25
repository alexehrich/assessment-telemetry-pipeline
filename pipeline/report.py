"""Summarises what the receiver stored, per drone, for a time window.

    python -m pipeline.report [--minutes N] [--from-ms T] [--to-ms T]

For each drone: messages stored, messages missing (gaps in seq), the largest gaps,
and delivery delay (received_ms - ts_ms). The window filters on ts_ms, the time the
GCS built the message, so a scenario's window shows whether its data arrived later.
"""

import argparse
import sqlite3
import time

from .config import env


def percentile(sorted_values, p):
    if not sorted_values:
        return 0
    return sorted_values[min(len(sorted_values) - 1, int(p / 100 * len(sorted_values)))]


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--minutes", type=float, help="only the last N minutes")
    p.add_argument("--from-ms", type=int)
    p.add_argument("--to-ms", type=int)
    p.add_argument("--db", default=env("RECEIVER_DB", "/data/telemetry.db"))
    args = p.parse_args()

    now_ms = int(time.time() * 1000)
    lo = args.from_ms or (now_ms - int(args.minutes * 60000) if args.minutes else 0)
    hi = args.to_ms or now_ms
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    print(f"window {time.strftime('%H:%M:%S', time.localtime(lo / 1000)) if lo else 'start'} - "
          f"{time.strftime('%H:%M:%S', time.localtime(hi / 1000))}")
    print(f"{'drone':<12} {'stored':>8} {'missing':>8} {'delay p50':>10} {'p95':>8} {'max':>8}  largest gaps (seq)")
    drones = [r[0] for r in db.execute(
        "SELECT DISTINCT drone_id FROM telemetry WHERE ts_ms BETWEEN ? AND ? ORDER BY 1", (lo, hi))]
    for drone in drones:
        rows = db.execute("SELECT seq, received_ms - ts_ms FROM telemetry "
                          "WHERE drone_id = ? AND ts_ms BETWEEN ? AND ? ORDER BY ts_ms", (drone, lo, hi)).fetchall()
        gaps = []
        for (a, _), (b, _) in zip(rows, rows[1:]):
            if b > a + 1:  # a drop in seq means the simulator restarted, not a loss
                gaps.append((b - a - 1, a + 1, b - 1))
        missing = sum(g[0] for g in gaps)
        delays = sorted(d for _, d in rows)
        largest = ", ".join(f"{n} ({s}-{e})" for n, s, e in sorted(gaps, reverse=True)[:3]) or "-"
        print(f"{drone:<12} {len(rows):>8} {missing:>8} {percentile(delays, 50) / 1000:>9.1f}s "
              f"{percentile(delays, 95) / 1000:>7.1f}s {delays[-1] / 1000:>7.1f}s  {largest}")
    if not drones:
        print("(no data in this window)")


if __name__ == "__main__":
    main()
