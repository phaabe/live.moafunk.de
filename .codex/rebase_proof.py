"""Run shared proof suites with protected runner files kept read-only."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
from uuid import uuid4


def sandbox_suite(
    top: Path, suite: dict[str, Any], policy: Any, codex: str | None, command_path: str
) -> dict[str, Any]:
    entry = {key: suite[key] for key in ("name", "cwd", "command")}
    if not isinstance(codex, str) or not Path(codex).is_absolute():
        raise ValueError("the pinned attempt has no absolute Codex executable")
    if Path(codex).resolve(strict=True) != Path(codex):
        raise ValueError("the pinned Codex executable must not contain symlinks")
    cwd = (top / suite["cwd"]).resolve()
    if not cwd.is_relative_to(top.resolve()):
        raise ValueError("suite cwd must be inside the selected worktree")
    try:
        with tempfile.TemporaryDirectory(prefix="rp-", dir="/private/tmp") as folder:
            name = f"proof_{uuid4().hex}"
            permission = (
                f'permissions.{name}={{extends=":workspace",'
                'filesystem={":slash_tmp"="read",":tmpdir"="read",'
                f'{json.dumps(folder)}="write"}},network={{enabled=false}}}}'
            )
            command = [
                str(codex),
                "sandbox",
                "-P",
                name,
                "-c",
                permission,
                "-C",
                str(cwd),
                "--allow-unix-socket",
                folder,
                *suite["command"],
            ]
            env = {
                key: value
                for key, value in policy.suite_env().items()
                if not key.startswith(("CODEX_", "GIT_", "PYTHON", "EPIC_"))
            }
            env.update(TMPDIR=folder, PATH=command_path, CODEX_PROOF_SANDBOX="1")
            out = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=policy.SUITE_TIMEOUT,
            )
    except FileNotFoundError as error:
        return {**entry, "exit": None, "result": "skipped", "tail": str(error)}
    except subprocess.TimeoutExpired:
        return {**entry, "exit": None, "result": "failed", "tail": "timed out"}
    result = {0: "passed", 127: "skipped"}.get(out.returncode, "failed")
    return {
        **entry,
        "exit": out.returncode,
        "result": result,
        "tail": (out.stdout + out.stderr)[-2000:],
    }


def main() -> int:
    # Called only by the installed helper with validated, protected context.
    runner, top, state_dir = map(Path, sys.argv[1:4])
    pr, head, onto = int(sys.argv[4]), sys.argv[5], sys.argv[6]
    sys.path.insert(0, str(runner / "scripts/epic"))
    import rebase_policy as policy

    try:
        attempt = json.loads((state_dir / "rebase-attempt.json").read_text())
        table = attempt.get("proof_suites")
        if not isinstance(table, list) or not all(policy.valid_suite(s) for s in table):
            raise ValueError("the pinned attempt has no valid proof suites")
        command_path = attempt.get("proof_path")
        if not isinstance(command_path, str) or not command_path:
            raise ValueError("the pinned attempt has no proof command path")
        policy.suites = lambda: table
        policy.run_suite = lambda worktree, suite: sandbox_suite(
            worktree,
            suite,
            policy,
            attempt.get("proof_codex"),
            command_path,
        )
        proof = policy.prove(top, pr, attempt["base"], state_dir)
        problem = policy.unclean(top) or policy.proof_problem(
            proof, top, pr, head, onto
        )
        if problem:
            raise ValueError(problem)
        print(f"Test proof is valid for {head}.")
        return 0
    except (
        policy.Problem,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"proof: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
