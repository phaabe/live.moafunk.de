"""Write the runner's tick events: one JSON line per tick start and finish.

The monitor reads `<kind>-ticks.jsonl` in the runner's state dir instead of
guessing outcomes from the log. The runner knows why a tick ended, so it
names the outcome and the phase. Each line is one write() below 4 KiB on a
file opened for appending, so lines of two writers never interleave.

  tick_events.py start  --file F --tick 2026-09-28T15:39:39Z --log L
      prints the log size, the offset where this tick's output begins
  tick_events.py mark   --file M --phase STEP
      appends the monotonic time STEP began
  tick_events.py finish --file F --tick T --exit N --phase P [--outcome O|auto]
                        [--action-file A] [--log L --since OFFSET]
                        [--claude-session ID --launch-dir D [--model-exit N]]
                        [--timings M]
  tick_events.py activity --file V --tick T --event model-start|model-end
                          [--action-file A]

`activity` writes one activity record (activity.py) to its own file, not to
the ticks file: the tick ledger (ticks.py) rejects event kinds it does not
know. A model-end has no outcome; the finish event names it.

Only enums, numbers, timestamps and the selector's action/PR/issue are
written: no model text, prompts or commands. The one exception is the
`env-block` event (`env_block()`, written by tick_cooldown.py): it keeps the
model's one-line reason for an environment block. Both events carry `runtime`: the
pinned runtime revision (EPIC_RUNTIME_REVISION, 40-hex) or null in legacy
mode. Event readers ignore unknown keys; tick checkpoints do not store it.

`tokens` is Codex's `tokens used` count; it stays null for Claude. The Claude
runner passes the session ID it gave `claude -p` (empty: no model started).
The finish event then has `session_id` and `usage`, read from that session's
transcript `<CLAUDE_CONFIG_DIR or ~/.claude>/projects/<launch dir>/<ID>.jsonl`
and its subagent transcripts. `usage` is null when no model started, else:

  input        uncached input tokens      (API usage input_tokens)
  output       output tokens, thinking included (output_tokens)
  cache_read   tokens read from the cache (cache_read_input_tokens)
  cache_write  tokens written to the cache (cache_creation_input_tokens)
  complete     true only when the transcript proves full coverage
  reason       null when complete, else one of USAGE_REASONS

All four counters are API tokens, summed over the session's messages. A
message's repeated records count once, with the largest value of each
counter. A counter that could not be read is null, never 0, and does not
cost the other counters; a partial session keeps the counts it has. Permission classifier calls are not part of
the transcript and not counted.

`durations` (only with --timings): seconds per step of the tick, from the
`mark` lines the runner wrote: a step lasts until the next mark, the last one
until the finish. Steps are STEPS; a step marked twice adds up; a step never
reached is missing. The clock is time.monotonic(), shared by all processes, so
a wall clock change does not move it. null when the file is missing,
unreadable, too large or has no valid mark; a bad line is skipped.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
import time
from typing import Any, BinaryIO

OUTCOMES = ("ok", "blocked", "timeout", "killed", "error")
# Every phase a runner may report. ticks.py reads events with this list too.
PHASES = (
    "lock",
    "refresh",
    "select",
    "quota",
    "usage",
    "backoff",
    "gate",
    "recheck",
    "assignment",
    "model",
    "result",
    "verify",
    "record",
    "unknown",
)
# Steps whose duration the runner measures (`mark`, `durations`).
STEPS = ("refresh", "closing", "selection", "model", "validation", "cleanup")
# A tick writes about six marks; more is not a runner file.
MAX_TIMINGS = 4096
TICK = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
REVISION = re.compile(r"[0-9a-f]{40}")
ACTION = re.compile(r"[a-z][a-z-]{0,31}")
ISSUE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/issues/\d{1,9}")
TOKENS = re.compile(r"[0-9]{1,3}(?:,[0-9]{3}){0,4}|[0-9]{1,15}")
MAX_LINE = 4096
# Length of the reason in an `env-block` event; escaped, it stays well inside
# MAX_LINE.
REASON_CHARS = 300
MAX_ACTION_FILE = 65_536
# Only the end of the session's output is scanned for the token count.
MAX_SCAN = 1_048_576
# The helper runs while the runner holds its lock; it must never hang there.
DEADLINE = 10
# Transcript usage key -> event counter.
USAGE_FIELDS = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_read_input_tokens": "cache_read",
    "cache_creation_input_tokens": "cache_write",
}
USAGE_REASONS = (
    "no-transcript",  # no transcript file for the session
    "unreadable",  # a transcript file could not be opened or read
    "too-large",  # the read stopped at MAX_TRANSCRIPT or USAGE_SECONDS
    "no-usage",  # the transcript has no usage record
    "interrupted",  # the session timed out or was stopped
    "malformed",  # a model record could not be read
    "subagents",  # fewer subagent transcripts than subagent calls
)
SESSION = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# All transcript files of one session together; runner sessions use ~1 MiB.
MAX_TRANSCRIPT = 32 * 1024 * 1024
# Well inside DEADLINE, so the finish event is still written.
USAGE_SECONDS = 5
# timeout (124, 137 after the grace) or a signal stopped the session.
INTERRUPTED = (124, 129, 130, 137, 143)
SUBAGENT_TOOLS = ("Agent", "Task")
MAX_COUNT = 10**15
# Events of the `activity` command.
ACTIVITY_EVENTS = ("model-start", "model-end")


def open_regular(path: Path, flags: int) -> int:
    """Open without following links or blocking (a FIFO never waits here)."""
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(f"{path.name} is not a regular file")
    return fd


def read_regular(path: Path) -> BinaryIO:
    return os.fdopen(open_regular(path, os.O_RDONLY), "rb")


def runtime_revision() -> str | None:
    """The pinned runtime revision the launcher exported; None in legacy mode."""
    value = os.environ.get("EPIC_RUNTIME_REVISION") or ""
    return value if REVISION.fullmatch(value) else None


def outcome_of(exit_code: int) -> str:
    """Outcome from the exit code when the runner did not name one."""
    if exit_code == 0:
        return "ok"
    if exit_code == 124:
        return "timeout"
    if exit_code in (129, 130, 137, 143):
        return "killed"
    return "error"


def action_fields(path: Path | None) -> dict[str, Any]:
    """Selector decision fields, typed and bounded; empty when unreadable."""
    fields: dict[str, Any] = {"action": None, "pr": None, "issue": None}
    if path is None:
        return fields
    try:
        with read_regular(path) as stream:
            data = json.loads(stream.read(MAX_ACTION_FILE).decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return fields
    if not isinstance(data, dict):
        return fields
    if isinstance(data.get("action"), str) and ACTION.fullmatch(data["action"]):
        fields["action"] = data["action"]
    pr = data.get("pr")
    if type(pr) is int and 0 < pr < 10**9:
        fields["pr"] = pr
    if isinstance(data.get("issue"), str) and ISSUE.fullmatch(data["issue"]):
        fields["issue"] = data["issue"]
    return fields


def tokens_since(log: Path | None, offset: int | None) -> int | None:
    """Last `tokens used` count this tick printed; best effort."""
    if log is None or offset is None:
        return None
    try:
        with read_regular(log) as stream:
            size = os.fstat(stream.fileno()).st_size
            stream.seek(max(offset, size - MAX_SCAN, 0))
            text = stream.read().decode("utf-8", "replace")
    except OSError:
        return None
    lines = text.splitlines()
    found = None
    for i, line in enumerate(lines[:-1]):
        if line == "tokens used" and TOKENS.fullmatch(lines[i + 1]):
            found = int(lines[i + 1].replace(",", ""))
    return found


def transcript_of(session: str, launch_dir: Path | None) -> Path | None:
    """The session's transcript: the CLI names the folder after its launch dir.

    That is the runner checkout, not the --add-dir feature worktree.
    """
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    projects = root / "projects"
    if launch_dir is not None:
        exact = projects / re.sub(r"[^A-Za-z0-9]", "-", str(launch_dir))
        if (exact / f"{session}.jsonl").exists():
            return exact / f"{session}.jsonl"
    # The CLI shortens very long folder names; the session ID is unique.
    found = sorted(projects.glob(f"*/{session}.jsonl"))
    return found[0] if found else None


@dataclass
class Scan:
    """Usage of one session's transcript files, per message ID."""

    deadline: float
    left: int
    messages: dict[str, dict[str, int | None]] = field(default_factory=dict)
    # Model records with at least one readable counter, over all files.
    records: int = 0
    agent_calls: set[str] = field(default_factory=set)
    malformed: bool = False
    too_large: bool = False

    def read(self, path: Path) -> None:
        """Add one transcript file; OSError when it cannot be read."""
        with read_regular(path) as stream:
            while self.left > 0 and time.monotonic() < self.deadline:
                raw = stream.readline(self.left)
                if not raw:
                    return
                self.left -= len(raw)
                # Only model records carry usage; skip parsing the rest.
                if b'"assistant"' in raw:
                    self.add(raw)
            if stream.read(1):
                self.too_large = True

    def add(self, raw: bytes) -> None:
        try:
            record = json.loads(raw)
        except (ValueError, RecursionError):
            self.malformed = True
            return
        if not isinstance(record, dict) or record.get("type") != "assistant":
            return
        message = record.get("message")
        if not isinstance(message, dict):
            self.malformed = True
            return
        content = message.get("content")
        for part in content if isinstance(content, list) else ():
            if (
                isinstance(part, dict)
                and part.get("type") == "tool_use"
                and part.get("name") in SUBAGENT_TOOLS
            ):
                self.agent_calls.add(str(part.get("id")))
        # A local error note, not an API call.
        if message.get("model") == "<synthetic>":
            return
        key, usage = message.get("id") or record.get("requestId"), message.get("usage")
        if not isinstance(key, str) or not isinstance(usage, dict):
            self.malformed = True
            return
        # Each counter on its own: a missing or bad one stays None and does
        # not cost the others.
        counts: dict[str, int | None] = {}
        for source, name in USAGE_FIELDS.items():
            value = usage.get(source)
            valid = type(value) is int and 0 <= value < MAX_COUNT
            counts[name] = value if valid else None
            if not valid:
                self.malformed = True
        if all(n is None for n in counts.values()):
            return
        self.records += 1
        # Records of one message repeat per content block, and a streamed
        # record can be cumulative: keep the largest value, never add them.
        known = self.messages.get(key)
        if known is not None:
            counts = {
                k: max((v for v in (known[k], n) if v is not None), default=None)
                for k, n in counts.items()
            }
        self.messages[key] = counts


def unavailable(reason: str) -> dict[str, Any]:
    return {**dict.fromkeys(USAGE_FIELDS.values()), "complete": False, "reason": reason}


def usage_of(
    session: str, launch_dir: Path | None, model_exit: int | None
) -> dict[str, Any]:
    """Usage of a started session; model_exit None: the runner was stopped."""
    path = transcript_of(session, launch_dir)
    if path is None:
        return unavailable("no-transcript")
    scan = Scan(time.monotonic() + USAGE_SECONDS, MAX_TRANSCRIPT)
    try:
        scan.read(path)
    except OSError:
        return unavailable("unreadable")
    children = sorted((path.parent / session / "subagents").glob("agent-*.jsonl"))
    unread = False
    # A child without a usable usage record is not collected coverage.
    empty = 0
    for child in children:
        before = scan.records
        try:
            scan.read(child)
        except OSError:
            unread = True
            continue
        if scan.records == before:
            empty += 1
    if not scan.messages:
        if scan.too_large:
            return unavailable("too-large")
        return unavailable("malformed" if scan.malformed else "no-usage")
    reason = None
    if model_exit is None or model_exit in INTERRUPTED:
        reason = "interrupted"
    elif scan.too_large:
        reason = "too-large"
    elif unread:
        reason = "unreadable"
    elif scan.malformed:
        reason = "malformed"
    elif empty or len(children) < len(scan.agent_calls):
        reason = "subagents"
    # A counter no message could read stays None; the rest keep their sums
    # (then `malformed` marks them partial).
    totals: dict[str, int | None] = {}
    for name in USAGE_FIELDS.values():
        known = [m[name] for m in scan.messages.values() if m[name] is not None]
        totals[name] = sum(known) if known else None
    return {**totals, "complete": reason is None, "reason": reason}


def claude_fields(
    session: str, launch_dir: Path | None, model_exit: int | None
) -> dict[str, Any]:
    """`session_id` and `usage` of a Claude tick; empty session: no model."""
    if not session:
        return {"session_id": None, "usage": None}
    if not SESSION.fullmatch(session):
        return {"session_id": None, "usage": unavailable("no-transcript")}
    try:
        usage = usage_of(session, launch_dir, model_exit)
    # Reporting must never cost the finish event, whatever the transcript holds.
    except Exception:  # noqa: BLE001
        usage = unavailable("unreadable")
    return {"session_id": session, "usage": usage}


def durations_of(path: Path | None, now: float) -> dict[str, float] | None:
    """Seconds per step from the runner's marks; None when unavailable."""
    if path is None:
        return None
    try:
        with read_regular(path) as stream:
            raw = stream.read(MAX_TIMINGS + 1)
    except OSError:
        return None
    if len(raw) > MAX_TIMINGS:
        return None
    marks: list[tuple[str, float]] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        words = line.split()
        if len(words) != 2 or words[0] not in STEPS:
            continue
        try:
            at = float(words[1])
        except ValueError:
            continue
        # Out of order or not finite: not a mark of this clock.
        if not 0 <= at <= now or (marks and at < marks[-1][1]):
            continue
        marks.append((words[0], at))
    if not marks:
        return None
    found: dict[str, float] = {}
    for (step, at), (_, until) in zip(marks, [*marks[1:], ("", now)]):
        found[step] = found.get(step, 0.0) + until - at
    return {step: round(seconds, 3) for step, seconds in found.items()}


def mark(path: Path, step: str) -> None:
    """One mark line; one write on an append-only file."""
    line = f"{step} {time.monotonic():.6f}\n".encode()
    fd = open_regular(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def append(path: Path, event: dict[str, Any]) -> None:
    line = (json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8")
    if len(line) >= MAX_LINE:
        raise ValueError("event line is too long")
    fd = open_regular(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        if os.write(fd, line) != len(line):
            raise OSError("short event write")
    finally:
        os.close(fd)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def env_block(
    path: Path,
    tick: str,
    action: dict[str, Any],
    target: str,
    reason: str,
    hold: bool,
) -> None:
    """One `env-block` event, between the tick's start and finish.

    The runner's environment stopped the model (tick_cooldown.py). Unlike the
    other events it carries the model's short reason, one line of at most
    REASON_CHARS characters, because the reason is what Anton must fix.
    `hold` is true when this block set the model hold.
    """
    if not TICK.fullmatch(tick):
        raise ValueError("tick must look like 2026-09-28T15:39:39Z")
    name = action.get("action")
    append(
        path,
        {
            "v": 1,
            "event": "env-block",
            "tick": tick,
            "at": now_iso(),
            "action": name
            if isinstance(name, str) and ACTION.fullmatch(name)
            else None,
            "target": target,
            "reason": " ".join(reason.split())[:REASON_CHARS],
            "hold": hold,
            "runtime": runtime_revision(),
        },
    )


def activity_event(path: Path, tick: str, kind: str, action_file: Path | None) -> None:
    """One activity record of a model boundary; the model ran for both."""
    import activity  # only this command needs the contract

    found = activity.record(
        {
            "event": kind,
            "tick": tick,
            "at": now_iso(),
            "model_started": True,
            **action_fields(action_file),
        }
    )
    if found is None:
        raise ValueError("invalid activity event")
    append(path, found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    finish = commands.add_parser("finish")
    marker = commands.add_parser("mark")
    marker.add_argument("--file", type=Path, required=True)
    marker.add_argument("--phase", choices=STEPS, required=True)
    boundary = commands.add_parser("activity")
    boundary.add_argument("--file", type=Path, required=True)
    boundary.add_argument("--tick", required=True)
    boundary.add_argument("--event", choices=ACTIVITY_EVENTS, required=True)
    boundary.add_argument("--action-file", type=Path)
    for command in (start, finish):
        command.add_argument("--file", type=Path, required=True)
        command.add_argument("--tick", required=True)
        command.add_argument("--log", type=Path)
    finish.add_argument("--exit", type=int, required=True, dest="exit_code")
    finish.add_argument("--phase", choices=PHASES, required=True)
    # "auto": derive from the exit code (bash 3.2 cannot pass empty arrays).
    finish.add_argument("--outcome", choices=(*OUTCOMES, "auto"), default="auto")
    finish.add_argument("--action-file", type=Path)
    finish.add_argument("--since", type=int)
    finish.add_argument("--claude-session")
    finish.add_argument("--launch-dir", type=Path)
    finish.add_argument("--model-exit", type=int)
    finish.add_argument("--timings", type=Path)
    args = parser.parse_args(argv)
    if args.command == "mark":
        try:
            mark(args.file, args.phase)
        except OSError as error:
            print(f"tick: mark failed: {error}", file=sys.stderr)
            return 1
        return 0
    if not TICK.fullmatch(args.tick):
        parser.error("--tick must look like 2026-09-28T15:39:39Z")
    # A hard deadline: whatever blocks, the runner's cleanup goes on.
    previous = signal.signal(
        signal.SIGALRM, lambda *_: sys.exit("tick: event helper timed out")
    )
    signal.alarm(DEADLINE)
    try:
        return write(parser, args)
    finally:
        # In-process callers (tests) must not get the alarm later.
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous or signal.SIG_DFL)


def write(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    try:
        if args.command == "activity":
            activity_event(args.file, args.tick, args.event, args.action_file)
            return 0
        if args.command == "start":
            append(
                args.file,
                {
                    "v": 1,
                    "event": "start",
                    "tick": args.tick,
                    "pid": os.getppid(),
                    "runtime": runtime_revision(),
                },
            )
            # The runner passes this back to finish, to find its token count.
            size = 0
            if args.log is not None:
                try:
                    with read_regular(args.log) as stream:
                        size = os.fstat(stream.fileno()).st_size
                except OSError:
                    size = 0
            print(size)
            return 0
        if not 0 <= args.exit_code <= 255:
            parser.error("--exit must be 0-255")
        append(
            args.file,
            {
                "v": 1,
                "event": "finish",
                "tick": args.tick,
                "at": now_iso(),
                "exit": args.exit_code,
                "outcome": (
                    outcome_of(args.exit_code)
                    if args.outcome == "auto"
                    else args.outcome
                ),
                "phase": args.phase,
                "runtime": runtime_revision(),
                **action_fields(args.action_file),
                "tokens": tokens_since(args.log, args.since),
                **(
                    {}
                    if args.claude_session is None
                    else claude_fields(
                        args.claude_session, args.launch_dir, args.model_exit
                    )
                ),
                **(
                    {}
                    if args.timings is None
                    else {"durations": durations_of(args.timings, time.monotonic())}
                ),
            },
        )
    except (OSError, ValueError) as error:
        print(f"tick: event write failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
