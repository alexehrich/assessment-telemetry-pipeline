"""Wire protocol between agent (instance) and receiver (tactical server).

Runs inside a mutual-TLS connection. Every frame is a 4-byte big-endian
length followed by the body:

    agent -> receiver   BATCH  body = batch_id (8 bytes) + zlib(messages joined by b"\\n")
    receiver -> agent   ACK    body = batch_id (8 bytes)

The receiver sends ACK only after the batch is committed to storage, so an
acked batch is durable. Unacked batches are resent after a reconnect, which
makes delivery at-least-once; the receiver removes the duplicates.
"""

import asyncio
import struct
import zlib

LEN = struct.Struct("!I")
BATCH_ID = struct.Struct("!Q")

MAX_FRAME = 1 << 20           # 1 MiB on the wire; a real batch is a few KB
MAX_DECOMPRESSED = 8 << 20    # guards the receiver against decompression bombs
COMPRESS_LEVEL = 6


class ProtocolError(Exception):
    pass


def frame(body: bytes) -> bytes:
    return LEN.pack(len(body)) + body


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    (size,) = LEN.unpack(await reader.readexactly(LEN.size))
    if size > MAX_FRAME:
        raise ProtocolError(f"frame of {size} bytes exceeds {MAX_FRAME}")
    return await reader.readexactly(size)


def encode_batch(batch_id: int, messages: list[bytes]) -> bytes:
    """Returns the complete frame for a batch, ready to write."""
    return frame(BATCH_ID.pack(batch_id) + zlib.compress(b"\n".join(messages), COMPRESS_LEVEL))


def decode_batch(body: bytes) -> tuple[int, list[bytes]]:
    if len(body) < BATCH_ID.size:
        raise ProtocolError("batch shorter than its id")
    (batch_id,) = BATCH_ID.unpack_from(body)
    inflater = zlib.decompressobj()
    try:
        data = inflater.decompress(body[BATCH_ID.size:], MAX_DECOMPRESSED)
    except zlib.error as e:
        raise ProtocolError(f"bad compression: {e}") from None
    if inflater.unconsumed_tail or not inflater.eof:
        raise ProtocolError("batch too large or truncated after decompression")
    return batch_id, data.split(b"\n") if data else []


def encode_ack(batch_id: int) -> bytes:
    return frame(BATCH_ID.pack(batch_id))


def decode_ack(body: bytes) -> int:
    if len(body) != BATCH_ID.size:
        raise ProtocolError("ack must be exactly a batch id")
    return BATCH_ID.unpack(body)[0]
