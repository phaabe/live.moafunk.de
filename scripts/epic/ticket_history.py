"""Board status history for the Tickets dashboard; the runners never read it.

The REST board API returns only the current Status, and issue timelines miss
most changes. So the collector records what it observes itself:

  ticket-status.jsonl    status changes seen between two fresh snapshots
  ticket-coverage.jsonl  coverage gaps: fresh snapshots more than 10 min apart
  ticket-history.json    the last fresh snapshot and when each ticket was seen
  alloy/ticket-segments.jsonl
                         one line per segment revision, shipped to Loki for
                         the status history panel (status · agent text)

Only observed changes exist: a status visited and left between two snapshots
is lost. A change seen across a gap, and a ticket's first entry (seed), never
start or end a measurement.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any

import next_action as epic
from ticks import LOCAL, Sink, nearest_rank

Json = dict[str, Any]
REPO_URL = f"https://github.com/{epic.REPO}"
STATUSES = ("Backlog", "Refinement", "Ready", "In progress", "In review", "Done")
# Fixed codes for the status timeline fallback; Unknown is 0.
CODES = {"Unknown": 0, **{status: i + 1 for i, status in enumerate(STATUSES)}}
EXECUTORS = ("Claude", "Codex", "Anton", "Unassigned", "Unknown")
# Statuses with a time-in-status statistic.
TIMED = ("Refinement", "Ready", "In progress", "In review")
AGENT_STATUSES = ("In progress", "In review")
QUANTILES = (0.5, 0.85)
LEVELS = ("Task", "Subtask")
DAY = 86400

GAP_SECONDS = 600  # fresh snapshots further apart leave a coverage gap
RETAIN_DAYS = 90
KEEP_DONE_DAYS = 14  # completed episodes kept and counted
MAX_EPISODES = 50  # per-episode rows exported, newest first
WINDOW = 7 * DAY  # the history panel's range
CORRECTION = DAY  # a closed segment's agent may still change
REFRESH = 3600  # open segments are rewritten so a 7-day query finds them
MAX_SEGMENT_FILE = 8_000_000  # bytes; then old revisions are dropped
LEDGER = "ticket-status.jsonl"
COVERAGE = "ticket-coverage.jsonl"
STATE = "ticket-history.json"
SEGMENTS = "alloy/ticket-segments.jsonl"
TICK_SOURCES = {"events": 1, "events_unverified": 0}  # name -> verified
# A segment a later gap replaced; the history panel skips it.
RETIRED = "retired"


def berlin_day(at: float) -> str:
    return datetime.fromtimestamp(at, LOCAL).strftime("%Y-%m-%d")


# --- JSON lines files ---


def read_lines(path: Path) -> list[Json]:
    """Every row of a JSON lines file. A broken last line (a write cut short)
    is logged and cut off the file, so the next append starts clean. A broken
    line before it raises ValueError: the file is unknown, never guessed."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    whole_lines = data.rfind(b"\n") + 1
    lines = data[:whole_lines].split(b"\n")[:-1]
    rows: list[Json] = []
    keep = 0  # bytes of the valid lines read so far
    for number, line in enumerate(lines):
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("not an object")
        except ValueError:
            if number < len(lines) - 1 or whole_lines < len(data):
                raise ValueError(f"{path.name}: broken line {number + 1}") from None
            break
        rows.append(row)
        keep += len(line) + 1
    if keep < len(data):
        logging.warning("%s: dropping a broken last line", path.name)
        with path.open("r+b") as stream:
            stream.truncate(keep)
            os.fsync(stream.fileno())
    return rows


def append_lines(path: Path, rows: Iterable[Json]) -> None:
    text = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    if not text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write(text.encode())
        stream.flush()
        os.fsync(stream.fileno())


def rewrite_lines(path: Path, rows: Iterable[Json]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        try:
            for row in rows:
                out.write(json.dumps(row, sort_keys=True) + "\n")
            out.flush()
            os.fsync(out.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


# --- ledger rows ---


def whole(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("times are whole non-negative seconds")
    return value


@dataclass(frozen=True)
class Entry:
    """One observed status change, or a ticket's first entry (seed)."""

    issue: int
    to: str
    seen_at: int
    executor: str
    frm: str | None = None
    seen_before: int | None = None
    seed: bool = False

    @property
    def uncertain(self) -> bool:
        return (
            self.seen_before is not None
            and self.seen_at - self.seen_before > GAP_SECONDS
        )

    @property
    def clean(self) -> bool:
        """May start or end a measurement."""
        return not self.seed and not self.uncertain

    def row(self) -> Json:
        row: Json = {
            "issue": self.issue,
            "from": self.frm,
            "to": self.to,
            "seen_before": self.seen_before,
            "seen_at": self.seen_at,
            "executor": self.executor,
        }
        if self.seed:
            row["source"] = "seed"
        if self.uncertain:
            row["uncertain"] = True
        if self.to == "Done":
            # The episode id; never changed after it is written.
            row["episode"] = self.seen_at
        return row

    @classmethod
    def parse(cls, row: Json) -> Entry:
        issue, to, executor = row.get("issue"), row.get("to"), row.get("executor")
        frm, before = row.get("from"), row.get("seen_before")
        seed = row.get("source") == "seed"
        if (
            type(issue) is not int
            or issue <= 0
            or to not in CODES
            or executor not in EXECUTORS
            or row.get("source") not in (None, "seed")
            or (seed and (frm is not None or before is not None))
            or (not seed and (frm not in CODES or frm == to))
        ):
            raise ValueError("bad ticket status row")
        entry = cls(
            issue,
            to,
            whole(row.get("seen_at")),
            executor,
            frm,
            None if before is None else whole(before),
            seed,
        )
        if entry.seen_before is not None and entry.seen_before > entry.seen_at:
            raise ValueError("seen_before after seen_at")
        return entry


@dataclass(frozen=True)
class Gap:
    start: int
    end: int

    @classmethod
    def parse(cls, row: Json) -> Gap:
        gap = cls(whole(row.get("gap_start")), whole(row.get("gap_end")))
        if gap.end <= gap.start:
            raise ValueError("empty coverage gap")
        return gap


def overlaps(gaps: Iterable[Gap], start: float, end: float) -> bool:
    return any(gap.start < end and gap.end > start for gap in gaps)


# --- episodes and measurements ---


def episodes(entries: list[Entry]) -> list[list[Entry]]:
    """An episode ends at a Done entry; the next one starts after it."""
    found: list[list[Entry]] = [[]]
    for entry in entries:
        found[-1].append(entry)
        if entry.to == "Done":
            found.append([])
    return [episode for episode in found if episode]


@dataclass
class Episode:
    issue: int
    done: Entry
    cycle: float | None
    lead: float | None
    # Status -> summed seconds; a status with an unclean interval is left out.
    times: dict[str, float]


def measure(entries: list[Entry], gaps: list[Gap]) -> Episode | None:
    """The measurements of a completed episode with a clean Done entry."""
    done = entries[-1]
    if done.to != "Done" or not done.clean:
        return None

    def span(first: Entry | None) -> float | None:
        if first is None or not first.clean:
            return None
        if overlaps(gaps, first.seen_at, done.seen_at):
            return None
        return float(done.seen_at - first.seen_at)

    starts = [e for e in entries[:-1] if e.to == "In progress"]
    readies = [e for e in entries[:-1] if e.to == "Ready"]
    times: dict[str, float] = {}
    bad: set[str] = set()
    for entry, after in zip(entries, entries[1:], strict=False):
        if entry.to not in TIMED:
            continue
        if (
            not entry.clean
            or not after.clean
            or overlaps(gaps, entry.seen_at, after.seen_at)
        ):
            bad.add(entry.to)
            continue
        times[entry.to] = times.get(entry.to, 0.0) + after.seen_at - entry.seen_at
    return Episode(
        done.issue,
        done,
        span(starts[0] if starts else None),
        span(readies[-1] if readies else None),
        {status: value for status, value in times.items() if status not in bad},
    )


def retained(entries: list[Entry], now: float) -> list[Entry]:
    """Drop entries older than RETAIN_DAYS unless they belong to the current
    unfinished episode or to one completed in the last KEEP_DONE_DAYS."""
    cutoff = now - RETAIN_DAYS * DAY
    recent = now - KEEP_DONE_DAYS * DAY
    keep: list[Entry] = []
    by_issue: dict[int, list[Entry]] = {}
    for entry in entries:
        by_issue.setdefault(entry.issue, []).append(entry)
    for rows in by_issue.values():
        for episode in episodes(rows):
            last = episode[-1]
            if last.to != "Done" or last.seen_at >= recent:
                keep += episode
            else:
                keep += [e for e in episode if e.seen_at >= cutoff]
    return sorted(keep, key=lambda e: (e.seen_at, e.issue))


# --- segments ---


@dataclass
class Segment:
    """One ledger interval of a ticket, split at coverage gaps."""

    segment_id: str
    issue: int
    status: str  # a board status, or "gap"
    start: int
    end: int | None
    agent: str = ""
    verified: int = 1
    # (finished_at, tick id, started_at) of the tick that named the agent;
    # the first two order ticks, the start rechecks the overlap.
    tick: tuple[float, str, float] | None = None


def overlapping(tick: tuple[float, str, float], segment: Segment, now: float) -> bool:
    end = now if segment.end is None else segment.end
    return tick[2] <= end and tick[0] >= segment.start


def intervals(entries: list[Entry], gaps: list[Gap]) -> list[Segment]:
    """A ticket's segments. They touch end to start; a gap is its own
    segment ("gap"), drawn as no data."""
    found: list[Segment] = []
    issue = entries[0].issue if entries else 0
    for entry, after in zip(entries, [*entries[1:], None], strict=True):
        start = entry.seen_at
        end = after.seen_at if after else None
        for gap in sorted(gaps, key=lambda g: g.start):
            if gap.end <= start or (end is not None and gap.start >= end):
                continue
            if gap.start > start:
                found.append(
                    Segment(f"{issue}-{start}", issue, entry.to, start, gap.start)
                )
            gap_start = max(gap.start, start)
            gap_end = gap.end if end is None else min(gap.end, end)
            found.append(
                Segment(f"{issue}-{gap_start}-gap", issue, "gap", gap_start, gap_end)
            )
            start = gap_end
        if end is None or end > start:
            found.append(Segment(f"{issue}-{start}", issue, entry.to, start, end))
    return found


def segment_row(segment: Segment, rev: int, now: int) -> Json:
    return {
        "segment_id": segment.segment_id,
        "rev": rev,
        "issue": segment.issue,
        "status": segment.status,
        "start": segment.start,
        "end": "" if segment.end is None else segment.end,
        "agent": segment.agent,
        "verified": segment.verified,
        "tick": list(segment.tick) if segment.tick else None,
        "emitted_at": now,
    }


@dataclass
class Stored:
    segment: Segment
    rev: int
    emitted_at: int


def parse_segment(row: Json) -> Stored:
    tick = row.get("tick")
    end = row.get("end")
    try:
        segment = Segment(
            row["segment_id"],
            row["issue"],
            row["status"],
            whole(row["start"]),
            None if end == "" else whole(end),
            row["agent"],
            row["verified"],
            None if tick is None else (float(tick[0]), str(tick[1]), float(tick[2])),
        )
        stored = Stored(segment, whole(row["rev"]), whole(row["emitted_at"]))
    except (KeyError, TypeError, IndexError) as error:
        raise ValueError("bad segment row") from error
    if (
        not isinstance(segment.segment_id, str)
        or type(segment.issue) is not int
        or segment.status not in (*CODES, "gap", RETIRED)
        or not isinstance(segment.agent, str)
        or segment.verified not in (0, 1)
    ):
        raise ValueError("bad segment row")
    return stored


def agent_ticks(views: dict[str, list[Json]]) -> list[tuple[str, int, Json]]:
    """(agent, verified, tick) of finished ticks from runner events; the
    legacy log is never used."""
    found = []
    for agent, rows in views.items():
        for tick in rows:
            verified = TICK_SOURCES.get(tick.get("source", "log"))
            if verified is None or tick.get("end") is None:
                continue
            found.append((agent, verified, tick))
    return found


def best_tick(
    segment: Segment,
    targets: set[str],
    actions: tuple[str, ...],
    ticks: list[tuple[str, int, Json]],
    now: float,
) -> tuple[str, int, tuple[float, str, float]] | None:
    """The newest matching tick whose run overlaps the segment."""
    end = now if segment.end is None else segment.end
    best = None
    for agent, verified, tick in ticks:
        if tick.get("action") not in actions or tick.get("target") not in targets:
            continue
        if tick["start"] > end or tick["end"] < segment.start:
            continue
        key = (float(tick["end"]), str(tick["id"]), float(tick["start"]))
        if best is None or key[:2] > best[2][:2]:
            best = (agent, verified, key)
    return best


# --- the history ---


@dataclass
class TicketTime:
    status: str
    entered: int
    # "1" exact, "0" a lower bound (seed or uncertain entry), "gap" when the
    # time in status overlaps a coverage gap.
    exact: str
    ready_entered: int | None
    agent: str


@dataclass
class Summary:
    tickets: dict[int, TicketTime] = field(default_factory=dict)
    # Completed episodes with a clean Done entry in the last KEEP_DONE_DAYS.
    episodes: list[Episode] = field(default_factory=list)


class History:
    """The ledger, gaps and segments of one runtime directory."""

    def __init__(self, runtime: Path) -> None:
        self.runtime = runtime
        self.loaded = False

    def path(self, name: str) -> Path:
        return self.runtime / name

    def load(self, now: float) -> None:
        """Replay the files; drop duplicates and entries past retention."""
        rows = read_lines(self.path(LEDGER))
        entries: list[Entry] = []
        seen: set[tuple[int, str, int]] = set()
        for row in rows:
            entry = Entry.parse(row)
            key = (entry.issue, entry.to, entry.seen_at)
            if key not in seen:
                seen.add(key)
                entries.append(entry)
        entries.sort(key=lambda e: (e.seen_at, e.issue))
        kept = retained(entries, now)
        self.gaps = sorted(
            {Gap.parse(row) for row in read_lines(self.path(COVERAGE))},
            key=lambda g: g.start,
        )
        oldest = min((e.seen_before or e.seen_at for e in kept), default=now)
        kept_gaps = [gap for gap in self.gaps if gap.end >= oldest]
        if len(kept) != len(rows):
            rewrite_lines(self.path(LEDGER), (e.row() for e in kept))
        if len(kept_gaps) != len(self.gaps):
            rewrite_lines(
                self.path(COVERAGE),
                ({"gap_start": g.start, "gap_end": g.end} for g in kept_gaps),
            )
        self.gaps = kept_gaps
        self.entries: dict[int, list[Entry]] = {}
        for entry in kept:
            self.entries.setdefault(entry.issue, []).append(entry)
        try:
            state = json.loads(self.path(STATE).read_text())
        except FileNotFoundError:
            state = {"v": 1, "last": None, "seen": {}}
        if (
            not isinstance(state, dict)
            or state.get("v") != 1
            or not isinstance(state.get("seen"), dict)
        ):
            raise ValueError("bad ticket history state")
        self.last = None if state.get("last") is None else whole(state["last"])
        self.seen = {int(k): whole(v) for k, v in state["seen"].items()}
        self.segments: dict[str, Stored] = {}
        for row in read_lines(self.path(SEGMENTS)):
            stored = parse_segment(row)
            old = self.segments.get(stored.segment.segment_id)
            if old is None or stored.rev >= old.rev:
                self.segments[stored.segment.segment_id] = stored
        self.loaded = True

    def observe(self, board: dict[int, tuple[str, str]], fetched_at: int) -> bool:
        """Record one board snapshot: issue -> (status, executor).

        A snapshot not newer than the last one (the shared reader's cache) is
        skipped. Returns True when it was new.
        """
        if self.last is not None and fetched_at <= self.last:
            return False
        gaps = []
        if self.last is not None and fetched_at - self.last > GAP_SECONDS:
            gaps.append(Gap(self.last, fetched_at))
        added: list[Entry] = []
        for issue, (status, executor) in sorted(board.items()):
            rows = self.entries.get(issue)
            if not rows:
                added.append(Entry(issue, status, fetched_at, executor, seed=True))
            elif rows[-1].to != status:
                # Unknown when the state file was lost: 0 makes it uncertain.
                before = self.seen.get(issue, self.last) or 0
                added.append(
                    Entry(issue, status, fetched_at, executor, rows[-1].to, before)
                )
        # Gaps first: a crash after them only repeats an entry, which replay drops.
        append_lines(
            self.path(COVERAGE),
            ({"gap_start": g.start, "gap_end": g.end} for g in gaps),
        )
        append_lines(self.path(LEDGER), (entry.row() for entry in added))
        self.gaps += gaps
        for entry in added:
            self.entries.setdefault(entry.issue, []).append(entry)
        self.last = fetched_at
        for issue in board:
            self.seen[issue] = fetched_at
        state = {
            "v": 1,
            "last": self.last,
            "seen": {str(k): v for k, v in self.seen.items()},
        }
        temporary = self.path(STATE + ".tmp")
        temporary.write_text(json.dumps(state, sort_keys=True))
        os.replace(temporary, self.path(STATE))
        return True

    def update_segments(
        self,
        shown: Iterable[int],
        prs: dict[int, int],
        views: dict[str, list[Json]] | None,
        now: int,
    ) -> None:
        """Write new segment revisions for the shown tickets.

        A revision is written when a segment opens, closes, or its agent
        changes, and every REFRESH for an open one. Agents come from finished
        runner ticks; without ticks (views None) stored agents stay. A closed
        segment is frozen CORRECTION seconds after its end.
        """
        ticks = agent_ticks(views) if views is not None else []
        out: list[Json] = []
        open_by_issue: dict[int, list[Stored]] = {}
        for stored in self.segments.values():
            if stored.segment.end is None:
                open_by_issue.setdefault(stored.segment.issue, []).append(stored)
        for issue in sorted(set(shown)):
            pr = prs.get(issue)
            issue_url = f"{REPO_URL}/issues/{issue}"
            pr_url = f"{REPO_URL}/pull/{pr}" if pr else None
            wanted = intervals(self.entries.get(issue, []), self.gaps)
            ids = {segment.segment_id for segment in wanted}
            for stored in open_by_issue.get(issue, []):
                if stored.segment.segment_id in ids:
                    continue
                # A gap that starts at the segment's start replaced it: close
                # it with no length, so replay never has two open segments.
                old = stored.segment
                retired = Segment(old.segment_id, issue, RETIRED, old.start, old.start)
                out.append(segment_row(retired, stored.rev + 1, now))
                self.segments[old.segment_id] = Stored(retired, stored.rev + 1, now)
            for segment in wanted:
                stored = self.segments.get(segment.segment_id)
                # Old intervals are skipped, unless one was exported open and
                # still needs its close revision.
                was_open = stored is not None and stored.segment.end is None
                if (
                    segment.end is not None
                    and segment.end < now - WINDOW
                    and not was_open
                ):
                    continue
                if stored is not None:
                    old = stored.segment
                    segment.agent, segment.verified = old.agent, old.verified
                    segment.tick = old.tick
                    # A gap can shorten the segment: drop a tick that no
                    # longer overlaps it, even when the segment is frozen.
                    if segment.tick and not overlapping(segment.tick, segment, now):
                        segment.agent, segment.verified, segment.tick = "", 1, None
                frozen = segment.end is not None and now - segment.end > CORRECTION
                if segment.status in AGENT_STATUSES and ticks and not frozen:
                    if segment.status == "In progress":
                        targets = {issue_url} | ({pr_url} if pr_url else set())
                        actions: tuple[str, ...] = ("claim", "continue")
                    else:
                        targets = {pr_url} if pr_url else set()
                        actions = ("review",)
                    best = best_tick(segment, targets, actions, ticks, now)
                    if best and (
                        segment.tick is None or best[2][:2] > segment.tick[:2]
                    ):
                        segment.agent, segment.verified, segment.tick = best
                if stored is None:
                    rev = 1
                else:
                    old = stored.segment
                    # The tick too: a newer tick of the same agent must be
                    # kept, or an older one of another agent could win later.
                    same = (old.status, old.end, old.agent, old.verified, old.tick) == (
                        segment.status,
                        segment.end,
                        segment.agent,
                        segment.verified,
                        segment.tick,
                    )
                    refresh = segment.end is None and now - stored.emitted_at >= REFRESH
                    if same and not refresh:
                        continue
                    rev = stored.rev + 1
                out.append(segment_row(segment, rev, now))
                self.segments[segment.segment_id] = Stored(segment, rev, now)
        path = self.path(SEGMENTS)
        append_lines(path, out)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return
        if size > MAX_SEGMENT_FILE:
            self.compact_segments(now)

    def compact_segments(self, now: int) -> None:
        """Keep each segment's newest revision; drop closed segments older than
        the panel window. Alloy reads the new file again; Loki drops exact
        duplicates and Alloy drops lines older than its limit."""
        # The ledger's current open interval of every ticket keeps its
        # agent and revision count, also when the ticket is not shown now.
        # An open segment the ledger no longer has is dropped, so nothing
        # stays open for ever.
        current = {
            segment.segment_id
            for rows in self.entries.values()
            for segment in intervals(rows, self.gaps)[-1:]
            if segment.end is None
        }
        recent = now - WINDOW - DAY
        keep = {
            key: stored
            for key, stored in self.segments.items()
            if (stored.segment.end is None and key in current)
            or (stored.segment.end is not None and stored.segment.end >= recent)
        }
        rewrite_lines(
            self.path(SEGMENTS),
            (
                segment_row(s.segment, s.rev, s.emitted_at)
                for s in sorted(keep.values(), key=lambda s: s.emitted_at)
            ),
        )
        self.segments = keep

    def summary(self, now: float) -> Summary:
        found = Summary()
        recent = now - KEEP_DONE_DAYS * DAY
        for issue, rows in self.entries.items():
            last = rows[-1]
            # A gap first: the status may have changed and come back in it.
            if overlaps(self.gaps, last.seen_at, now):
                exact = "gap"
            elif not last.clean:
                exact = "0"
            else:
                exact = "1"
            readies = [e.seen_at for e in rows if e.to == "Ready"]
            agent = ""
            if last.to in AGENT_STATUSES:
                current = [
                    s.segment
                    for s in self.segments.values()
                    if s.segment.issue == issue
                    and s.segment.end is None
                    and s.segment.status == last.to
                ]
                if current:
                    segment = max(current, key=lambda s: s.start)
                    agent = segment.agent
                    if agent and not segment.verified:
                        agent += " (unverified)"
            found.tickets[issue] = TicketTime(
                last.to,
                last.seen_at,
                exact,
                readies[-1] if readies else None,
                agent,
            )
            for episode in episodes(rows):
                measured = measure(episode, self.gaps)
                if measured and measured.done.seen_at >= recent:
                    found.episodes.append(measured)
        found.episodes.sort(key=lambda e: (-e.done.seen_at, e.issue))
        return found


def board_statuses(state: Json) -> dict[int, tuple[str, str]]:
    """Every Task and Subtask of this repository on the board, with the
    status and Executor the history records."""
    found = {}
    for number, item in epic.board_issues(state).items():
        if item.get("level") not in LEVELS:
            continue
        raw = item.get("status")
        status = raw if isinstance(raw, str) and raw in STATUSES else "Unknown"
        executor = item.get("executor") or "Unassigned"
        found[number] = (status, executor if executor in EXECUTORS else "Unknown")
    return found


def quantile_rows(values: list[float]) -> dict[float, float]:
    """Nearest-rank quantiles; no samples gives no value."""
    return {q: nearest_rank(values, q) for q in QUANTILES} if values else {}


def history_metrics(
    metrics: Sink, summary: Summary, shown: Iterable[int], now: float
) -> None:
    """Entered times of the shown tickets and the flow statistics."""
    for issue in sorted(set(shown)):
        ticket = summary.tickets.get(issue)
        if ticket is None:
            continue
        metrics.add(
            "ticket_status_entered_seconds",
            ticket.entered,
            issue=str(issue),
            exact=ticket.exact,
        )
    eligible = summary.episodes
    for episode in eligible[:MAX_EPISODES]:
        labels = {"issue": str(episode.issue), "episode": str(episode.done.seen_at)}
        metrics.add(
            "ticket_done_seconds",
            episode.done.seen_at,
            **labels,
            executor=episode.done.executor,
        )
        if episode.cycle is not None:
            metrics.add("ticket_cycle_seconds", episode.cycle, **labels)
        if episode.lead is not None:
            metrics.add("ticket_lead_seconds", episode.lead, **labels)
    for status in TIMED:
        values = [e.times[status] for e in eligible if status in e.times]
        for q, value in quantile_rows(values).items():
            metrics.add(
                "ticket_time_in_status_seconds", value, status=status, quantile=str(q)
            )
    cycles = [e.cycle for e in eligible if e.cycle is not None]
    for q, value in quantile_rows(cycles).items():
        metrics.add("ticket_cycle_quantile_seconds", value, quantile=str(q))
    leads = [e.lead for e in eligible if e.lead is not None]
    if leads:
        metrics.add("ticket_lead_median_seconds", nearest_rank(leads, 0.5))
    # Calendar days: a DST change makes a day 23 or 25 hours long.
    today = datetime.fromtimestamp(now, LOCAL).date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(KEEP_DONE_DAYS)]
    counts = {(day, executor): 0 for day in days for executor in EXECUTORS}
    for episode in eligible:
        key = (berlin_day(episode.done.seen_at), episode.done.executor)
        if key in counts:
            counts[key] += 1
    for (day, executor), count in counts.items():
        metrics.add("tickets_done_day", count, day=day, executor=executor)
