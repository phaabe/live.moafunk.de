"""Send fixture-only API requests and a synthetic WebSocket capture."""

from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import struct
import sys
import time
from pathlib import Path


def request(method: str, path: str, body: str = "") -> str:
    connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=15)
    headers = {"Content-Type": "application/json", "Cookie": "session=fixture-session"}
    connection.request(method, path, body, headers)
    response = connection.getresponse()
    data = response.read().decode()
    if response.status != 200:
        raise RuntimeError(f"{method} {path}: {response.status} {data}")
    return data


def frame(payload: bytes, opcode: int) -> bytes:
    mask = os.urandom(4)
    size = len(payload)
    header = bytes([0x80 | opcode])
    if size < 126:
        header += bytes([0x80 | size])
    elif size < 65536:
        header += b"\xfe" + struct.pack("!H", size)
    else:
        header += b"\xff" + struct.pack("!Q", size)
    return (
        header
        + mask
        + bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    )


def produce() -> None:
    connection = socket.create_connection(("127.0.0.1", 8000), timeout=15)
    key = base64.b64encode(os.urandom(16)).decode()
    connection.sendall(
        (
            f"GET /ws/stream?show_id=1 HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nCookie: session=fixture-session\r\n\r\n"
        ).encode()
    )
    handshake = b""
    deadline = time.monotonic() + 15
    while b"\r\n\r\n" not in handshake:
        chunk = connection.recv(4096)
        if not chunk:
            raise RuntimeError("WebSocket server closed before upgrade")
        handshake += chunk
        if len(handshake) > 16384 or time.monotonic() > deadline:
            raise RuntimeError("WebSocket upgrade exceeded its bound")
    if not handshake.startswith(b"HTTP/1.1 101"):
        raise RuntimeError(f"Upgrade failed: {handshake!r}")
    data = Path("/work/tone.webm").read_bytes()
    for offset in range(0, len(data), 4096):
        connection.sendall(frame(data[offset : offset + 4096], 2))
        time.sleep(0.05)
    # Allow the decoder and recorder queues to consume the synthetic payload.
    time.sleep(2)
    status = json.loads(request("GET", "/api/recording/status"))
    Path("/work/capture-active.json").write_text(json.dumps(status))
    connection.close()  # Unexpected disconnect; exercise the real 30-second grace.


if __name__ == "__main__":
    if sys.argv[1] == "produce":
        produce()
    else:
        print(request(*sys.argv[1:]))
