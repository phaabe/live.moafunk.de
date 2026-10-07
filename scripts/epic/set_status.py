"""Change a ticket's board Status and its status::* label together.

The board Status stays the truth. The label exists for its history: GitHub
keeps every `labeled` / `unlabeled` event with a time, also while the local
monitoring stack is off. The Tickets collector reads those events as recorded
change times.

Usage (from the trusted checkout, exactly this form):
  python3 scripts/epic/set_status.py set <issue> <status>
  python3 scripts/epic/set_status.py sync <issue> [<issue>...]
  python3 scripts/epic/set_status.py repair <issue>

Modes:
  set     write the board Status, then swap the status label. Counts as a change.
  sync    copy board -> label inside a status::sync marker window. Not a change.
          Anton only (backfill, cards moved by hand); refused inside a tick.
  repair  same as sync for one issue, also inside a runner tick (half-done set).

Inside a runner tick (EPIC_ACTION_FILE) only the tick's issue is allowed: the
action's issue, or for a PR action the tickets of the PR's `Issue:` lines, read
fresh from GitHub. The runner locks only the action's targets, so the helper
also takes the issue lock of a PR's ticket.

Every call prints one JSON line: issue, mode, from, to, board, label, where
board and label are "ok", "failed" or "unknown".

Exit codes:
  0  board and label match the wanted Status
  1  a write failed and the read after it proves it
  2  usage error, issue not on the board or not in this repo, wrong tick target,
     or the tick's PR could not be read
  3  conflict: the board moved during the call; no label written
  4  unknown: a read after a failed or timed-out write failed too (also the
     read that confirms the sync marker)
  5  not clean (missing, extra or wrong status label, or a leftover
     status::sync marker): run repair first; nothing written
  75 the issue is locked by another helper call or runner
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

import next_action  # noqa: E402
import target_lock  # noqa: E402

REPO = "phaabe/live.moafunk.de"
PROJECT_API = "users/anneoneone/projectsV2/2"
ISSUE_PREFIX = f"https://github.com/{REPO}/issues/"
STATUSES = ("Backlog", "Refinement", "Ready", "In progress", "In review", "Done")
PREFIX = "status::"
MARKER = "status::sync"
GH_TIMEOUT_SECONDS = 60

OK, FAILED, USAGE, CONFLICT, UNKNOWN, NOT_CLEAN = 0, 1, 2, 3, 4, 5
BUSY = target_lock.BUSY


def label_for(status: str) -> str:
    """Board Status name -> label name: "In progress" -> "status::in-progress"."""
    return PREFIX + status.lower().replace(" ", "-")


LABELS = {label_for(s): s for s in STATUSES}


def status_labels(labels: list[str]) -> list[str]:
    return sorted(name for name in labels if name in LABELS)


class ReadFailed(Exception):
    """A GitHub read failed; the caller reports "unknown", never "ok"."""


class Usage(Exception):
    """The call itself is wrong (exit 2)."""


Gh = Callable[[list[str], "str | None"], str]


def run_gh(args: list[str], stdin: str | None = None) -> str:
    """One `gh` call. Raises on a non-zero exit or a timeout."""
    done = subprocess.run(
        ["gh", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=GH_TIMEOUT_SECONDS,
        check=False,
    )
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip() or f"gh exited {done.returncode}")
    return done.stdout


class Board:
    """REST reads and writes for one issue: board item and labels."""

    def __init__(self, gh: Gh = run_gh) -> None:
        self.gh = gh
        self._field: dict[str, Any] | None = None

    def rows(self, endpoint: str) -> list[dict[str, Any]]:
        pages = json.loads(self.gh(["api", "--paginate", "--slurp", endpoint], None))
        return [row for page in pages for row in page]

    def status_field(self) -> dict[str, Any]:
        if self._field is None:
            for field in self.rows(f"{PROJECT_API}/fields?per_page=100"):
                if field.get("name") == "Status":
                    self._field = field
                    break
            else:
                raise ReadFailed("project board has no Status field")
        return self._field

    def item(self, issue: int) -> tuple[int, str | None]:
        """(REST item id, Status name or None). Usage when not on the board."""
        try:
            field = self.status_field()
            rows = self.rows(f"{PROJECT_API}/items?per_page=100&fields[]={field['id']}")
        except (ReadFailed, Usage):
            raise
        except Exception as err:
            raise ReadFailed(f"board read failed: {err}") from err
        for row in rows:
            content = row.get("content") or {}
            if (
                row.get("content_type") == "Issue"
                and content.get("number") == issue
                and content.get("html_url") == f"{ISSUE_PREFIX}{issue}"
            ):
                status = None
                for value in row.get("fields") or []:
                    if value.get("name") == "Status" and value.get("value"):
                        status = str(value["value"]["name"]["raw"])
                return int(row["id"]), status
        raise Usage(f"issue {issue} is not on board 2 in {REPO}")

    def status(self, issue: int) -> str | None:
        return self.item(issue)[1]

    def labels(self, issue: int) -> list[str]:
        try:
            rows = self.rows(f"repos/{REPO}/issues/{issue}/labels?per_page=100")
        except Exception as err:
            raise ReadFailed(f"label read failed: {err}") from err
        return sorted(str(row.get("name")) for row in rows)

    def write_status(self, item_id: int, status: str) -> None:
        field = self.status_field()
        option = next(
            (
                o.get("id")
                for o in field.get("options") or []
                if (o.get("name") or {}).get("raw") == status
            ),
            None,
        )
        if option is None:
            raise Usage(f"board Status has no option {status!r}")
        body = json.dumps({"fields": [{"id": field["id"], "value": option}]})
        self.gh(
            [
                "api",
                "--method",
                "PATCH",
                f"{PROJECT_API}/items/{item_id}",
                "--input",
                "-",
            ],
            body,
        )

    def add_label(self, issue: int, name: str) -> None:
        body = json.dumps({"labels": [name]})
        self.gh(
            [
                "api",
                "--method",
                "POST",
                f"repos/{REPO}/issues/{issue}/labels",
                "--input",
                "-",
            ],
            body,
        )

    def remove_label(self, issue: int, name: str) -> None:
        self.gh(
            [
                "api",
                "--method",
                "DELETE",
                f"repos/{REPO}/issues/{issue}/labels/{quote(name, safe='')}",
            ],
            None,
        )


def try_write(write: Callable[[], None]) -> None:
    """Run a write and swallow its error: the read after it decides the result."""
    try:
        write()
    except Usage:
        raise
    except Exception:
        pass


def lock(path: Path) -> int | None:
    """Open and flock `path` without waiting. None when busy. Never deleted."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def tick_action(env: dict[str, str]) -> dict[str, Any] | None:
    """The running tick's action, or None outside a runner tick."""
    path = env.get("EPIC_ACTION_FILE")
    if not path:
        return None
    try:
        action = json.loads(Path(path).read_text())
    except (OSError, ValueError) as err:
        raise Usage(f"cannot read the tick action: {err}") from err
    if not isinstance(action, dict):
        raise Usage("the tick action is not an object")
    return action


def pr_issues(board: Board, pr: int) -> set[int]:
    """The tickets a PR names in its `Issue:` lines, read fresh (the same rule
    as the runner's write check)."""
    try:
        pull = json.loads(board.gh(["api", f"repos/{REPO}/pulls/{pr}"], None))
    except Exception as err:
        raise Usage(f"cannot read PR {pr} for its Issue: line: {err}") from err
    return next_action.issue_numbers(str(pull.get("body") or ""))


def check_target(board: Board, issue: int, action: dict[str, Any]) -> None:
    """Usage unless `issue` is the tick's issue or its PR's `Issue:` ticket."""
    pr = action.get("pr") if isinstance(action.get("pr"), int) else None
    allowed = set(target_lock.targets(action)) - {pr}
    if issue not in allowed and pr is not None:
        allowed |= pr_issues(board, pr)
    if issue not in allowed:
        raise Usage(f"issue {issue} is not this tick's target {sorted(allowed)}")


def take_locks(issue: int, action: dict[str, Any] | None) -> list[int] | None:
    """Tick lock unless the runner already holds it (the action's own targets,
    not a PR's `Issue:` ticket), then the helper lock. None when either is busy."""
    root = target_lock.lock_dir()
    held: list[int] = []
    names = [f"status-{issue}.lock"]
    if action is None or issue not in target_lock.targets(action):
        names.insert(0, f"{issue}.lock")
    for name in names:
        fd = lock(root / name)
        if fd is None:
            for open_fd in held:
                os.close(open_fd)
            return None
        held.append(fd)
    return held


def report(
    issue: int, mode: str, old: Any, new: Any, board: str, label: str
) -> dict[str, Any]:
    return {
        "issue": issue,
        "mode": mode,
        "from": old,
        "to": new,
        "board": board,
        "label": label,
    }


def code_of(result: dict[str, Any]) -> int:
    states = {result["board"], result["label"]}
    if states == {"ok"}:
        return OK
    if "unknown" in states:
        return UNKNOWN
    return FAILED


def final(board: Board, issue: int, want: str) -> tuple[str, str]:
    """Read board and labels again: ok / failed / unknown for each."""
    try:
        board_state = "ok" if board.status(issue) == want else "failed"
    except ReadFailed:
        board_state = "unknown"
    try:
        found = board.labels(issue)
        clean = status_labels(found) == [label_for(want)] and MARKER not in found
        label_state = "ok" if clean else "failed"
    except ReadFailed:
        label_state = "unknown"
    return board_state, label_state


def do_set(board: Board, issue: int, want: str) -> tuple[int, dict[str, Any]]:
    item_id, old = board.item(issue)
    found = board.labels(issue)
    current = status_labels(found)
    if MARKER in found or old is None or current != [label_for(old)]:
        print(
            f"set_status: issue {issue} is not clean (board {old}, labels "
            f"{current or 'none'}{', sync marker' if MARKER in found else ''}): "
            "run repair first",
            file=sys.stderr,
        )
        return NOT_CLEAN, report(issue, "set", old, want, "failed", "failed")
    if old != want:
        try_write(lambda: board.write_status(item_id, want))
        try:
            now = board.status(issue)
        except ReadFailed:
            return UNKNOWN, report(issue, "set", old, want, "unknown", "failed")
        if now == old:
            return FAILED, report(issue, "set", old, want, "failed", "failed")
        if now != want:
            print(
                f"set_status: conflict: board of {issue} is {now!r}, not {want!r}",
                file=sys.stderr,
            )
            return CONFLICT, report(issue, "set", old, want, "failed", "failed")
        try_write(lambda: board.add_label(issue, label_for(want)))
        try_write(lambda: board.remove_label(issue, label_for(old)))
    result = report(issue, "set", old, want, *final(board, issue, want))
    return code_of(result), result


def do_sync(board: Board, issue: int, mode: str) -> tuple[int, dict[str, Any]]:
    """Copy board -> label inside a status::sync window. Never a change time."""
    _, status = board.item(issue)
    if status is None:
        print(f"set_status: issue {issue} has no board Status", file=sys.stderr)
        return FAILED, report(issue, mode, None, None, "failed", "failed")
    want = label_for(status)
    found = board.labels(issue)
    old = status_labels(found)
    previous = ",".join(old) or None
    if old != [want] or MARKER in found:
        try_write(lambda: board.add_label(issue, MARKER))
        # Without a confirmed marker the label swap would read as a change.
        try:
            marked = MARKER in board.labels(issue)
        except ReadFailed:
            print(f"set_status: cannot confirm {MARKER} on {issue}", file=sys.stderr)
            return UNKNOWN, report(issue, mode, previous, want, "ok", "unknown")
        if not marked:
            print(f"set_status: adding {MARKER} to {issue} failed", file=sys.stderr)
            return FAILED, report(issue, mode, previous, want, "ok", "failed")
        if want not in old:
            try_write(lambda: board.add_label(issue, want))
        for name in old:
            if name != want:
                try_write(lambda name=name: board.remove_label(issue, name))
        try_write(lambda: board.remove_label(issue, MARKER))
    result = report(issue, mode, previous, want, *final(board, issue, status))
    return code_of(result), result


def parse(argv: list[str]) -> tuple[str, list[int], str | None]:
    if not argv or argv[0] not in ("set", "sync", "repair"):
        raise Usage("mode must be set, sync or repair")
    mode, rest = argv[0], argv[1:]
    if mode == "set":
        if len(rest) != 2 or rest[1] not in STATUSES:
            raise Usage(f"set <issue> <status>; status one of {', '.join(STATUSES)}")
        rest, status = rest[:1], rest[1]
    else:
        status = None
        if not rest or (mode == "repair" and len(rest) != 1):
            raise Usage(f"{mode} needs {'one issue' if mode == 'repair' else 'issues'}")
    if not all(arg.isdigit() and int(arg) > 0 for arg in rest):
        raise Usage("issue must be a positive number")
    return mode, [int(arg) for arg in rest], status


def main(
    argv: list[str],
    board: Board | None = None,
    env: dict[str, str] | None = None,
) -> int:
    env = dict(os.environ) if env is None else env
    board = board or Board()
    try:
        mode, issues, status = parse(argv)
        action = tick_action(env)
        if action is not None:
            if mode == "sync":
                raise Usage("sync is Anton's backfill; inside a tick use repair")
            check_target(board, issues[0], action)
    except Usage as err:
        print(f"set_status: {err}", file=sys.stderr)
        return USAGE
    worst = OK
    for issue in issues:
        fds = take_locks(issue, action)
        if fds is None:
            print(f"set_status: issue {issue} is locked; retry", file=sys.stderr)
            return BUSY
        try:
            if mode == "set":
                assert status is not None
                code, result = do_set(board, issue, status)
            else:
                code, result = do_sync(board, issue, mode)
        except Usage as err:
            print(f"set_status: {err}", file=sys.stderr)
            code, result = USAGE, None
        except ReadFailed as err:
            print(f"set_status: {err}", file=sys.stderr)
            code = UNKNOWN
            result = report(issue, mode, None, status, "unknown", "unknown")
        finally:
            for fd in fds:
                os.close(fd)
        if result is not None:
            print(json.dumps(result, sort_keys=True))
        worst = max(worst, code)
    return worst


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
