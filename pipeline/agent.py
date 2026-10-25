"""Instance agent: receives GCS telemetry over UDP and forwards it over mutual TLS.

Three independent parts, so a slow or dead uplink never slows down the GCS side:

  ingest   UDP datagrams are appended to a list. Nothing else happens on this path.
  batcher  every BATCH_INTERVAL_S the list becomes one compressed batch in the buffer.
  sender   keeps one TLS connection to the tactical server, sends batches from the
           buffer and removes them only when the receiver has acked them.

The buffer lives in memory only and has a hard byte cap. When it is full the oldest
batches are dropped: old telemetry is worth less than fresh telemetry, and nothing
with location data is ever written to the instance's disk.
"""

import asyncio
import collections
import logging
import random
import signal
import socket
import ssl
import sys
import time
from dataclasses import dataclass

from . import protocol, tls
from .config import env, host_port

log = logging.getLogger("agent")

MAX_DATAGRAM = 2048  # a valid message is ~180 bytes
RECV_BUFFER = 1 << 20


@dataclass
class Batch:
    id: int
    frame: bytes
    count: int        # number of telemetry messages
    created: float    # monotonic time the batch was built


class Buffer:
    """Bounded FIFO of batches waiting to be sent. Drops the oldest batches when full."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self.queue: collections.deque[Batch] = collections.deque()
        self.bytes = 0
        self.dropped_msgs = 0

    def __len__(self):
        return len(self.queue)

    def append(self, batch: Batch):
        self.queue.append(batch)
        self.bytes += len(batch.frame)
        self._trim()

    def requeue(self, batches: list[Batch]):
        """Puts unacked batches back at the front, in their original order."""
        for batch in reversed(batches):
            self.queue.appendleft(batch)
            self.bytes += len(batch.frame)
        self._trim()

    def popleft(self) -> Batch:
        batch = self.queue.popleft()
        self.bytes -= len(batch.frame)
        return batch

    def oldest_age(self) -> float:
        return time.monotonic() - self.queue[0].created if self.queue else 0.0

    def _trim(self):
        while self.bytes > self.max_bytes and self.queue:
            self.dropped_msgs += self.popleft().count


class GcsListener(asyncio.DatagramProtocol):
    def __init__(self, agent):
        self.agent = agent

    def datagram_received(self, data, addr):
        if len(data) > MAX_DATAGRAM:
            self.agent.stats["oversize"] += 1
            return
        self.agent.pending.append(data)
        self.agent.stats["received"] += 1


class Agent:
    def __init__(self):
        self.agent_id = env("AGENT_ID")
        self.listen = env("AGENT_LISTEN", "0.0.0.0:4600", host_port)
        self.server_host, self.server_port = env("AGENT_SERVER", cast=host_port)
        self.batch_interval = env("AGENT_BATCH_INTERVAL_S", 1.0, float)
        self.window_bytes = env("AGENT_WINDOW_BYTES", 64 * 1024, int)
        self.ack_timeout = env("AGENT_ACK_TIMEOUT_S", 30.0, float)
        self.connect_timeout = env("AGENT_CONNECT_TIMEOUT_S", 15.0, float)
        self.backoff_base = env("AGENT_BACKOFF_BASE_S", 1.0, float)
        self.backoff_max = env("AGENT_BACKOFF_MAX_S", 30.0, float)
        self.ssl = tls.client_context(env("TLS_CA"), env("TLS_CERT"), env("TLS_KEY"))

        self.buffer = Buffer(env("AGENT_BUFFER_MAX_BYTES", 8 * 1024 * 1024, int))
        self.pending: list[bytes] = []
        self.inflight: dict[int, Batch] = {}  # insertion order = send order
        self.inflight_bytes = 0
        self.next_id = 0
        self.wake = asyncio.Event()
        self.last_progress = 0.0
        self.connected = False
        self.stats = collections.Counter()

    async def start_ingest(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECV_BUFFER)
        sock.bind(self.listen)
        await asyncio.get_running_loop().create_datagram_endpoint(lambda: GcsListener(self), sock=sock)
        log.info("listening for GCS telemetry on udp %s:%d", *self.listen)

    async def batcher(self):
        while True:
            await asyncio.sleep(self.batch_interval)
            if not self.pending:
                continue
            messages, self.pending = self.pending, []
            self.buffer.append(Batch(self.next_id, protocol.encode_batch(self.next_id, messages),
                                     len(messages), time.monotonic()))
            self.next_id += 1
            self.wake.set()

    async def sender(self):
        attempt = 0
        while True:
            acked_before = self.stats["acked_batches"]
            try:
                await self.session()
            except (OSError, EOFError, asyncio.TimeoutError, ssl.SSLError, protocol.ProtocolError) as e:
                log.warning("connection to %s:%d ended: %s", self.server_host, self.server_port,
                            str(e) or type(e).__name__)
            finally:
                self.connected = False
                self._requeue_inflight()
            if self.stats["acked_batches"] > acked_before:
                attempt = 0  # the link worked, start the backoff from scratch
            # Exponential backoff with full jitter: instances that lost the link at the
            # same moment spread their reconnects instead of arriving together.
            delay = random.uniform(0, min(self.backoff_max, self.backoff_base * 2 ** attempt))
            attempt += 1
            self.stats["reconnects"] += 1
            log.info("reconnecting in %.1fs (attempt %d)", delay, attempt)
            await asyncio.sleep(delay)

    async def session(self):
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.server_host, self.server_port, ssl=self.ssl,
                                    server_hostname=self.server_host),
            self.connect_timeout)
        sock = writer.get_extra_info("socket")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self.connected = True
        self.last_progress = time.monotonic()
        log.info("connected to %s:%d (%s)", self.server_host, self.server_port,
                 writer.get_extra_info("ssl_object").version())
        tasks = [asyncio.create_task(coro) for coro in
                 (self._send_loop(writer), self._ack_loop(reader), self._watchdog())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()  # re-raises the reason the session ended
        finally:
            for task in tasks:
                task.cancel()
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except (OSError, asyncio.TimeoutError, ssl.SSLError):
                pass

    async def _send_loop(self, writer):
        while True:
            # The window caps unacked data. A successful write only means the bytes are
            # in a socket buffer (or queued in a slow link), not that they arrived.
            while self.buffer and self.inflight_bytes < self.window_bytes:
                batch = self.buffer.popleft()
                if not self.inflight:
                    self.last_progress = time.monotonic()
                self.inflight[batch.id] = batch
                self.inflight_bytes += len(batch.frame)
                writer.write(batch.frame)
            await writer.drain()
            await self.wake.wait()
            self.wake.clear()

    async def _ack_loop(self, reader):
        while True:
            try:
                batch_id = protocol.decode_ack(await protocol.read_frame(reader))
            except asyncio.IncompleteReadError:
                raise EOFError("server closed the connection") from None
            batch = self.inflight.pop(batch_id, None)
            if batch is None:
                continue
            self.inflight_bytes -= len(batch.frame)
            self.stats["acked_batches"] += 1
            self.stats["acked_msgs"] += batch.count
            self.last_progress = time.monotonic()
            self.wake.set()

    async def _watchdog(self):
        # TCP alone may not notice a dead path for many minutes. If data is in flight and
        # nothing has been acked for ack_timeout, give up on this connection.
        while True:
            await asyncio.sleep(1)
            if self.inflight and time.monotonic() - self.last_progress > self.ack_timeout:
                raise asyncio.TimeoutError(f"no ack for {self.ack_timeout:g}s")

    def _requeue_inflight(self):
        if self.inflight:
            self.buffer.requeue(list(self.inflight.values()))
            self.stats["resent_batches"] += len(self.inflight)
            self.inflight.clear()
            self.inflight_bytes = 0

    async def reporter(self):
        while True:
            await asyncio.sleep(10)
            s = self.stats
            log.info("connected=%s received=%d acked=%d buffered_batches=%d buffered_bytes=%d "
                     "oldest_buffered_s=%.0f inflight=%d dropped_by_cap=%d resent_batches=%d reconnects=%d",
                     self.connected, s["received"], s["acked_msgs"], len(self.buffer), self.buffer.bytes,
                     self.buffer.oldest_age(), len(self.inflight), self.buffer.dropped_msgs,
                     s["resent_batches"], s["reconnects"])

    async def run(self):
        log.info("agent %s -> %s:%d, buffer cap %d bytes", self.agent_id, self.server_host,
                 self.server_port, self.buffer.max_bytes)
        await self.start_ingest()
        await asyncio.gather(self.batcher(), self.sender(), self.reporter())


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # Exit on SIGTERM. The in-memory buffer is lost by design (see README, Part 4).
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        asyncio.run(Agent().run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
