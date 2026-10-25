#!/usr/bin/env python3
"""Plays a drone team's ground control station (GCS).

Sends synthetic telemetry for N drones, one compact JSON object per UDP
datagram, at a fixed rate per drone. Fire-and-forget: nothing is retried.
"""

import argparse
import json
import math
import os
import random
import signal
import socket
import sys
import time

RADIUS_M = 400.0
SPEED_MPS = 12.0
ALT_M = 120.0
M_PER_DEG_LAT = 111320.0
BATTERY_CYCLE_S = 40 * 60  # battery drains from 100 to 20 % over this time, then wraps
REPORT_S = 10.0
MAX_LAG_S = 1.0


def target(value):
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise argparse.ArgumentTypeError(f"expected host:port, got {value!r}")
    return host, int(port)


def origin(value):
    lat, lon = map(float, value.split(","))
    if not (-90 < lat < 90 and -180 <= lon <= 180):
        raise argparse.ArgumentTypeError(f"expected lat in (-90, 90) and lon in [-180, 180], got {value!r}")
    return lat, lon


def parse_args():
    env = os.environ.get
    p = argparse.ArgumentParser(description="Send synthetic drone telemetry over UDP, playing a team's GCS.")
    p.add_argument("--team-id", default=env("SIM_TEAM_ID"), help="team id (env SIM_TEAM_ID), required")
    p.add_argument("--drones", type=int, default=env("SIM_DRONES", "2"), help="number of drones (env SIM_DRONES)")
    p.add_argument("--rate", type=float, default=env("SIM_RATE_HZ", "50"), help="messages per second per drone (env SIM_RATE_HZ)")
    p.add_argument("--target", type=target, default=env("SIM_TARGET", "127.0.0.1:4600"), help="host:port to send to (env SIM_TARGET)")
    p.add_argument("--origin", type=origin, default=env("SIM_ORIGIN", "-10.0,-30.0"), help="circle centre as lat,lon in degrees (env SIM_ORIGIN)")
    args = p.parse_args()
    if not args.team_id:
        p.error("--team-id or SIM_TEAM_ID is required")
    if args.drones < 1:
        p.error("--drones must be >= 1")
    if not args.rate > 0:
        p.error("--rate must be > 0")
    return args


def connect(sock, host, port):
    """Points the socket at the current address of host. Returns False if it does not resolve."""
    # A connected UDP socket gets ICMP port-unreachable back as ConnectionRefusedError
    # on a later send, so sends to a closed port show up as dropped.
    try:
        sock.connect(socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)[0][4])
        return True
    except OSError:
        return False


def main():
    args = parse_args()
    # As PID 1 in a container SIGTERM has no default action, so exit explicitly.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: sys.exit(0))

    host, port = args.target
    lat0, lon0 = args.origin
    m_per_deg_lon = M_PER_DEG_LAT * math.cos(math.radians(lat0))
    omega = SPEED_MPS / RADIUS_M  # rad/s
    print(f"start team_id={args.team_id} drones={args.drones} rate_hz={args.rate:g} "
          f"target={host}:{port} origin={lat0},{lon0}", flush=True)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    resolved = connect(sock, host, port)
    start = schedule_start = time.monotonic()
    next_report = start + REPORT_S
    tick = seq = sent = dropped = 0
    while True:
        delay = schedule_start + tick / args.rate - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        elif delay < -MAX_LAG_S:
            # Too far behind: restart the schedule instead of bursting to catch up.
            schedule_start, tick = time.monotonic(), 0
        tick += 1

        now = time.monotonic()
        t = now - start
        battery = int(100 - 80 * (t % BATTERY_CYCLE_S) / BATTERY_CYCLE_S)
        for i in range(args.drones):
            phase = 2 * math.pi * i / args.drones
            bearing = omega * t + phase  # from the origin, clockwise from north
            msg = {
                "team_id": args.team_id,
                "drone_id": f"{args.team_id}-{i + 1}",
                "seq": seq,
                "ts_ms": int(time.time() * 1000),
                "lat": round(lat0 + RADIUS_M * math.cos(bearing) / M_PER_DEG_LAT, 7),
                "lon": round(lon0 + RADIUS_M * math.sin(bearing) / m_per_deg_lon, 7),
                "alt_m": round(ALT_M + 3 * math.sin(2 * math.pi * t / 60 + phase) + random.uniform(-0.2, 0.2), 1),
                # Flying clockwise, so the tangent is 90 degrees right of the bearing.
                # The outer % 360 turns a rounded 360.0 into 0.0.
                "heading_deg": round((math.degrees(bearing) + 90) % 360, 1) % 360,
                "speed_mps": round(SPEED_MPS + random.uniform(-0.3, 0.3), 1),
                "battery_pct": battery,
            }
            if not resolved:
                dropped += 1
                continue
            try:
                sock.send(json.dumps(msg, separators=(",", ":")).encode())
                sent += 1
            except OSError:
                dropped += 1
        seq += 1

        if now >= next_report:
            print(f"last {REPORT_S:g}s: sent={sent} dropped={dropped}", flush=True)
            sent = dropped = 0
            resolved = connect(sock, host, port)
            next_report = now + REPORT_S


if __name__ == "__main__":
    main()
