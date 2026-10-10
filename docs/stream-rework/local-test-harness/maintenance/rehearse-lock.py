"""Test maintenance lock ownership through a real interactive Bash PTY.

Run with Python 3.11+ and the local desktop-linux Docker context. The pinned
backend image supplies Bash and util-linux flock; the backend never starts.
No ports, bind mounts or network access are used. The owned container is removed
on exit. This tests shell/descriptor behavior, not SSH or tmux survival, host
admission policy, or an automated deployment refusal gate.
"""

from __future__ import annotations

import errno
import os
import pty
import select
import subprocess
import time
import uuid
from dataclasses import dataclass

IMAGE = "ghcr.io/phaabe/live.moafunk.de-backend@sha256:77c304243b6b7dd048574f6abfea40d9fc053b2a6f9b9aa771bfdedcb4e080fe"
DOCKER = ("docker", "--context", "desktop-linux")
PROMPT = b"__MAINTENANCE_LOCK_PTY__ "
LOCK = "/tmp/moafunk-api-deploy.lock"
FDINFO_LOCK = (
    "^lock:[[:space:]]+[0-9]+:[[:space:]]+FLOCK[[:space:]]+"
    "ADVISORY[[:space:]]+WRITE[[:space:]]"
)


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        (*DOCKER, *args), check=check, capture_output=True, timeout=30
    )


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


@dataclass
class Owner:
    master: int
    process: subprocess.Popen[bytes]

    @classmethod
    def start(cls, container: str) -> Owner:
        master, slave = pty.openpty()
        try:
            process = subprocess.Popen(
                (
                    *DOCKER,
                    "exec",
                    "-it",
                    "--env",
                    "TERM=dumb",
                    "--env",
                    f"PS1={PROMPT.decode()}",
                    container,
                    "/bin/bash",
                    "--noprofile",
                    "--norc",
                    "-i",
                ),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
            )
        except OSError:
            os.close(master)
            raise
        finally:
            os.close(slave)
        owner = cls(master, process)
        try:
            owner.read(prompt=True)
        except (OSError, RuntimeError):
            owner.close()
            raise
        return owner

    def read(self, *, prompt: bool) -> bytes:
        output = bytearray()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            readable, _, _ = select.select([self.master], [], [], 0.1)
            if not readable:
                continue
            try:
                chunk = os.read(self.master, 4096)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                chunk = b""
            if not chunk:
                require(not prompt, f"Owner exited before prompt: {output!r}")
                return bytes(output)
            output.extend(chunk)
            require(len(output) <= 65536, "Unexpectedly large PTY response")
            if prompt and output.endswith(PROMPT):
                return bytes(output)
        raise RuntimeError(f"Timed out waiting for owner PTY: {output!r}")

    def send(self, command: str) -> None:
        os.write(self.master, command.encode() + b"\n")

    def command(self, command: str) -> bytes:
        self.send(command)
        return self.read(prompt=True)

    def close(self) -> None:
        os.close(self.master)
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=10)


def probe(container: str, expected: int) -> None:
    result = docker(
        "exec", container, "flock", "-n", "-E", "75", LOCK, "true", check=False
    )
    require(
        result.returncode == expected,
        f"Lock probe expected {expected}, got {result.returncode}: {result.stderr!r}",
    )


def acquire(owner: Owner, settings: str) -> None:
    owner.command(settings)
    owner.command(f"exec 9>>{LOCK} || exit 1")
    owner.command("flock -n 9 || exit 1")


def ownership(owner: Owner, expected: bool) -> None:
    result = owner.command(
        f"if [[ /proc/$$/fd/9 -ef {LOCK} ]] && "
        f"grep -Eq '{FDINFO_LOCK}' /proc/$$/fdinfo/9; "
        "then printf 'OWNER_CHECK=%s\\n' yes; "
        "else printf 'OWNER_CHECK=%s\\n' no; fi"
    )
    marker = b"OWNER_CHECK=yes" if expected else b"OWNER_CHECK=no"
    require(
        marker in result.splitlines(), f"Unexpected descriptor ownership: {result!r}"
    )


def regression(container: str) -> None:
    original = Owner.start(container)
    try:
        acquire(original, "set -euo pipefail")
        probe(container, 75)
        original.send("grep absent /dev/null")
        original.read(prompt=False)
        require(
            original.process.wait(timeout=10) == 1,
            "Failed diagnostic did not exit strict owner",
        )
        probe(container, 0)
        print(
            "PASS: interactive set -euo pipefail owner exits on failed grep and loses lock",
            flush=True,
        )
    finally:
        original.close()

    revised = Owner.start(container)
    try:
        acquire(revised, "set +e; set +u; set +o pipefail; umask 077")
        require(
            b"INTERACTIVE=yes"
            in revised.command("[[ $- == *i* ]] && printf 'INTERACTIVE=%s\\n' yes"),
            "Owner is not interactive",
        )
        ownership(revised, True)
        probe(container, 75)
        # A failed command and a failed last pipeline stage must retain the PTY.
        for diagnostic in ("grep absent /dev/null", "printf present | grep absent"):
            revised.command(diagnostic)
            result = revised.command("printf 'DIAGNOSTIC_STATUS=%s\\n' \"$?\"")
            require(
                b"DIAGNOSTIC_STATUS=1" in result, "Diagnostic did not fail as expected"
            )
            ownership(revised, True)
            probe(container, 75)
        revised.command("false | true")
        require(
            b"PIPELINE_STATUS=0"
            in revised.command("printf 'PIPELINE_STATUS=%s\\n' \"$?\""),
            "pipefail remains enabled",
        )
        revised.command("unset OPTIONAL_MAINTENANCE_VALUE")
        require(
            b"OPTIONAL<>"
            in revised.command(
                "printf 'OPTIONAL<%s>\\n' \"$OPTIONAL_MAINTENANCE_VALUE\""
            ),
            "Unset optional variable was not safe",
        )
        probe(container, 75)
        ownership(revised, True)
        require(revised.process.poll() is None, "Revised lock owner exited")
        print(
            "PASS: failed grep/pipeline and unset optional variable retain owner; fresh descriptor gets 75",
            flush=True,
        )

        contender = Owner.start(container)
        try:
            contender.command("set +e; set +u; set +o pipefail")
            contender.command(f"exec 9>>{LOCK} || exit 1")
            contender.send("flock -n 9 || exit 1")
            contender.read(prompt=False)
            require(
                contender.process.wait(timeout=10) == 1,
                "Busy-lock contender did not exit",
            )
            probe(container, 75)
        finally:
            contender.close()
        print(
            "PASS: explicit acquisition failure exits contender without losing original owner",
            flush=True,
        )

        # Negative fixture: an open original descriptor is not proof of ownership.
        revised.command("flock -u 9")
        replacement = Owner.start(container)
        try:
            acquire(replacement, "set +e; set +u; set +o pipefail")
            require(revised.process.poll() is None, "Original owner shell exited")
            require(
                b"FD_OPEN=yes"
                in revised.command(
                    "[[ -e /proc/$$/fd/9 ]] && printf 'FD_OPEN=%s\\n' yes"
                ).splitlines(),
                "Original descriptor did not remain open",
            )
            probe(container, 75)
            ownership(revised, False)
            ownership(replacement, True)
            replacement.send("flock -u 9; exec 9>&-; exit 0")
            replacement.read(prompt=False)
            require(
                replacement.process.wait(timeout=10) == 0,
                "Replacement owner did not exit",
            )
        finally:
            replacement.close()
        probe(container, 0)
        print(
            "PASS: fdinfo rejects original unlocked descriptor despite live shell and contention 75 from another owner",
            flush=True,
        )

        # Reset only this disposable fixture to test pathname replacement next.
        revised.command("flock -n 9 || exit 1")
        ownership(revised, True)
        revised.command(f"mv {LOCK} {LOCK}.retired; : >{LOCK}")
        require(
            b"FD_LOCK=yes"
            in revised.command(
                f"grep -Eq '{FDINFO_LOCK}' /proc/$$/fdinfo/9 && printf 'FD_LOCK=%s\\n' yes"
            ).splitlines(),
            "Original inode lost its descriptor lock",
        )
        ownership(revised, False)
        probe(container, 0)
        revised.command(f"mv -f {LOCK}.retired {LOCK}")
        ownership(revised, True)
        probe(container, 75)
        print(
            "PASS: inode check rejects a replaced lock pathname while old descriptor remains locked",
            flush=True,
        )

        revised.command("flock -u 9; exec 9>&-")
        probe(container, 0)
        require(
            revised.process.poll() is None, "Explicit release unexpectedly ended shell"
        )
        revised.send("exit 0")
        revised.read(prompt=False)
        require(revised.process.wait(timeout=10) == 0, "Owner did not exit cleanly")
        print(
            "PASS: explicit release is detected by probe status 0 and lock is acquirable",
            flush=True,
        )
    finally:
        revised.close()


def main() -> None:
    container = f"moafunk-maintenance-lock-{uuid.uuid4().hex[:12]}"
    docker(
        "run",
        "-d",
        "--name",
        container,
        "--platform",
        "linux/amd64",
        "--network",
        "none",
        "--read-only",
        "--user",
        "65534:65534",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=16m",
        "--entrypoint",
        "/bin/sleep",
        IMAGE,
        "infinity",
    )
    try:
        print(f"Image: {IMAGE}", flush=True)
        print(
            docker("exec", container, "flock", "--version").stdout.decode().strip(),
            flush=True,
        )
        regression(container)
        print(
            "LIMIT: local PTY/lock behavior only; no SSH/tmux or production gate qualification",
            flush=True,
        )
    finally:
        docker("rm", "-f", container)


if __name__ == "__main__":
    main()
