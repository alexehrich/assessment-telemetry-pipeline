"""Tactical-server receiver: accepts batches from known instances over mutual TLS.

An instance is known when its certificate is signed by the pipeline CA AND its
common name is in RECEIVER_ALLOWED_IDS. The CN is the instance identity: every
message in a batch must carry that same team_id, so one team cannot write data
in another team's name.

Each batch is validated, written to SQLite in one transaction and only then
acked. The primary key (drone_id, seq, ts_ms) drops resent duplicates.
"""

import asyncio
import collections
import json
import logging
import signal
import sqlite3
import ssl
import sys
import time

from . import protocol, tls
from .config import env, host_port

log = logging.getLogger("receiver")

SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry (
    team_id     TEXT    NOT NULL,
    drone_id    TEXT    NOT NULL,
    seq         INTEGER NOT NULL,
    ts_ms       INTEGER NOT NULL,
    lat         REAL    NOT NULL,
    lon         REAL    NOT NULL,
    alt_m       REAL    NOT NULL,
    heading_deg REAL    NOT NULL,
    speed_mps   REAL    NOT NULL,
    battery_pct INTEGER NOT NULL,
    received_ms INTEGER NOT NULL,
    PRIMARY KEY (drone_id, seq, ts_ms)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS telemetry_ts ON telemetry (team_id, ts_ms);
"""
COLUMNS = ("team_id", "drone_id", "seq", "ts_ms", "lat", "lon", "alt_m",
           "heading_deg", "speed_mps", "battery_pct")
INSERT = (f"INSERT OR IGNORE INTO telemetry ({', '.join(COLUMNS)}, received_ms) "
          f"VALUES ({', '.join('?' * (len(COLUMNS) + 1))})")


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def parse(raw: bytes, team_id: str, received_ms: int):
    """Returns a row tuple, or None if the message is malformed or not from this team."""
    try:
        m = json.loads(raw)
    except ValueError:
        return None
    if not (isinstance(m, dict)
            and m.get("team_id") == team_id
            and isinstance(m.get("drone_id"), str) and m["drone_id"].startswith(team_id + "-")
            and _int(m.get("seq")) and m["seq"] >= 0
            and _int(m.get("ts_ms"))
            and _number(m.get("lat")) and -90 <= m["lat"] <= 90
            and _number(m.get("lon")) and -180 <= m["lon"] <= 180
            and all(_number(m.get(k)) for k in ("alt_m", "heading_deg", "speed_mps"))
            and _int(m.get("battery_pct")) and 0 <= m["battery_pct"] <= 100):
        return None
    return tuple(m[c] for c in COLUMNS) + (received_ms,)


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def insert(self, rows) -> int:
        """Inserts rows in one transaction and returns how many were new."""
        with self.db:
            before = self.db.total_changes
            self.db.executemany(INSERT, rows)
            return self.db.total_changes - before


class Receiver:
    def __init__(self):
        self.listen = env("RECEIVER_LISTEN", "0.0.0.0:7000", host_port)
        self.allowed = {i.strip() for i in env("RECEIVER_ALLOWED_IDS").split(",") if i.strip()}
        self.idle_timeout = env("RECEIVER_IDLE_TIMEOUT_S", 120.0, float)
        self.max_connections = env("RECEIVER_MAX_CONNECTIONS", 100, int)
        self.ssl = tls.server_context(env("TLS_CA"), env("TLS_CERT"), env("TLS_KEY"))
        self.store = Store(env("RECEIVER_DB", "/data/telemetry.db"))
        self.sessions: dict[str, asyncio.StreamWriter] = {}
        self.stats: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)

    async def handle(self, reader, writer):
        peer = writer.get_extra_info("peername")
        # The handshake is done here rather than by start_server, so that a failed one
        # (unknown CA, no client certificate) is logged instead of silently dropped.
        try:
            await writer.start_tls(self.ssl, ssl_handshake_timeout=15)
        except (ssl.SSLError, ConnectionError, asyncio.TimeoutError) as e:
            log.warning("rejected %s: TLS handshake failed: %s", peer, getattr(e, "reason", None) or e)
            writer.close()
            return
        identity = tls.peer_common_name(writer.get_extra_info("ssl_object"))
        if identity not in self.allowed:
            # Signed by our CA but not (or no longer) allowed, e.g. a revoked instance.
            log.warning("rejected %s: identity %r is not an allowed instance", peer, identity)
            writer.close()
            return
        if len(self.sessions) >= self.max_connections and identity not in self.sessions:
            log.warning("rejected %s (%s): connection limit reached", peer, identity)
            writer.close()
            return
        previous = self.sessions.get(identity)
        if previous is not None:
            # After an outage the old connection may be half-dead. The newest one wins.
            log.info("%s reconnected, closing its previous connection", identity)
            previous.close()
        self.sessions[identity] = writer
        log.info("accepted %s as %s", peer, identity)
        stats = self.stats[identity]
        try:
            while True:
                body = await asyncio.wait_for(protocol.read_frame(reader), self.idle_timeout)
                batch_id, messages = protocol.decode_batch(body)
                now_ms = int(time.time() * 1000)
                rows = [r for r in (parse(m, identity, now_ms) for m in messages) if r]
                new = self.store.insert(rows)
                stats["batches"] += 1
                stats["stored"] += new
                stats["duplicates"] += len(rows) - new
                stats["invalid"] += len(messages) - len(rows)
                writer.write(protocol.encode_ack(batch_id))  # only after the commit
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            log.info("%s disconnected", identity)
        except asyncio.TimeoutError:
            log.info("%s idle for %gs, closing", identity, self.idle_timeout)
        except protocol.ProtocolError as e:
            log.warning("%s sent a bad frame (%s), closing", identity, e)
        finally:
            if self.sessions.get(identity) is writer:
                del self.sessions[identity]
            writer.close()

    async def reporter(self):
        while True:
            await asyncio.sleep(10)
            for identity in sorted(self.stats):
                s = self.stats[identity]
                log.info("%s connected=%s batches=%d stored=%d duplicates=%d invalid=%d",
                         identity, identity in self.sessions, s["batches"], s["stored"],
                         s["duplicates"], s["invalid"])

    async def run(self):
        server = await asyncio.start_server(self.handle, *self.listen)
        log.info("listening on tcp %s:%d, allowed instances: %s", *self.listen, ", ".join(sorted(self.allowed)))
        async with server:
            await asyncio.gather(server.serve_forever(), self.reporter())


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        asyncio.run(Receiver().run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
