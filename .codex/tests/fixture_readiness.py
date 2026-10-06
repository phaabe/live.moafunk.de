"""Bound fixture startup separately from the behavior being tested."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import socket
import subprocess
import time

STARTUP_SECONDS = 60.0


def stop_fixture(process: subprocess.Popen) -> None:
    """Stop a disposable fixture started with start_new_session=True."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def accept_ready(
    listener: socket.socket,
    process: subprocess.Popen,
    log: Path,
    timeout: float = STARTUP_SECONDS,
) -> socket.socket:
    deadline = time.monotonic() + timeout
    connection = None
    received = b""
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(
                    f"fixture exited before ready: {process.returncode}"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                if connection is None:
                    listener.settimeout(min(0.1, remaining))
                    connection, _ = listener.accept()
                connection.settimeout(min(0.1, remaining))
                chunk = connection.recv(5 - len(received))
                if not chunk:
                    raise AssertionError("fixture closed readiness socket")
                received += chunk
                if received == b"ready":
                    return connection
                if not b"ready".startswith(received):
                    raise AssertionError(f"invalid readiness message: {received!r}")
            except socket.timeout:
                continue
        raise AssertionError(f"fixture startup exceeded {timeout:g}s")
    except BaseException as error:
        if connection is not None:
            connection.close()
        stop_fixture(process)
        evidence = (
            log.read_text(errors="replace")[-4000:]
            if log.exists()
            else "no fixture log"
        )
        if isinstance(error, AssertionError):
            raise AssertionError(f"{error}\nfixture log:\n{evidence}") from error
        raise
