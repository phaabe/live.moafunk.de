"""Readiness failures remain bounded and stop disposable fixture children."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/epic"))
import isolated_env  # noqa: E402, F401

import os
import signal
import socket
import subprocess
import tempfile
import time
import unittest

from fixture_readiness import accept_ready, stop_fixture


class ReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="ready-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = self.root / "fixture.log"
        self.address = str(self.root / "ready.sock")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.listener.close)
        self.listener.bind(self.address)
        self.listener.listen(1)

    def start(self, body: str) -> subprocess.Popen:
        output = self.log.open("w")
        self.addCleanup(output.close)
        process = subprocess.Popen(
            [sys.executable, "-c", body],
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.addCleanup(stop_fixture, process)
        return process

    def test_delayed_and_fragmented_readiness(self) -> None:
        process = self.start(
            "import socket,time\n"
            "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)\n"
            f"s.connect({self.address!r})\n"
            "time.sleep(.2)\ns.sendall(b're')\ntime.sleep(.2)\n"
            "s.sendall(b'ady')\ns.recv(1)\n"
        )
        connection = accept_ready(self.listener, process, self.log, timeout=5)
        with connection:
            connection.sendall(b"x")
        self.assertEqual(process.wait(timeout=5), 0)

    def test_early_exit_reports_output_without_full_wait(self) -> None:
        process = self.start(
            "print('fixture setup failed',flush=True)\nraise SystemExit(7)"
        )
        started = time.monotonic()
        with self.assertRaisesRegex(
            AssertionError, "(?s)exited before ready: 7.*fixture setup failed"
        ):
            accept_ready(self.listener, process, self.log, timeout=10)
        self.assertLess(time.monotonic() - started, 5)

    def test_timeout_stops_child_and_closes_inherited_socket(self) -> None:
        process = self.start(
            "import socket,subprocess,sys,time\n"
            "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)\n"
            f"s.connect({self.address!r})\n"
            "subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],pass_fds=(s.fileno(),))\n"
            "s.sendall(b'waiting')\ntime.sleep(60)\n"
        )
        # Accept ourselves to prove the descendant inherited a live descriptor.
        self.listener.settimeout(5)
        connection, _ = self.listener.accept()
        with connection:
            connection.settimeout(5)
            self.assertEqual(connection.recv(7), b"waiting")
            with self.assertRaisesRegex(AssertionError, "startup exceeded"):
                accept_ready(self.listener, process, self.log, timeout=0.2)
            self.assertEqual(connection.recv(1), b"")
        self.assertIsNotNone(process.poll())

    def test_early_exit_stops_term_ignoring_child_and_closes_socket(self) -> None:
        child = (
            "import signal,socket,sys,time; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "s=socket.socket(fileno=int(sys.argv[1])); "
            "s.sendall(b'waiting'); time.sleep(60)"
        )
        process = self.start(
            "import socket,subprocess,sys\n"
            "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)\n"
            f"s.connect({self.address!r})\n"
            f"subprocess.Popen([sys.executable,'-c',{child!r},str(s.fileno())],pass_fds=(s.fileno(),))\n"
            "raise SystemExit(3)\n"
        )
        try:
            self.listener.settimeout(5)
            connection, _ = self.listener.accept()
            with connection:
                connection.settimeout(5)
                # The child has installed its signal handler and holds the socket.
                self.assertEqual(connection.recv(7), b"waiting")
                self.assertEqual(process.wait(timeout=5), 3)
                with self.assertRaisesRegex(AssertionError, "exited before ready: 3"):
                    accept_ready(self.listener, process, self.log, timeout=5)
                self.assertEqual(connection.recv(1), b"")
        finally:
            # Also clean up when running this regression against the broken helper.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


if __name__ == "__main__":
    unittest.main()
