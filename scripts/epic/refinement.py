"""Refinement records on an issue: proposal, verdict, escalation, reset, exempt list.

Pure functions over issue comments in the shape decide() reads
(`body`, `createdAt`, `url`, `includesCreatedEdit`, and `id` when known).
Shared rules: https://github.com/phaabe/live.moafunk.de/issues/487.

- Proposal: first line PROPOSAL_MARKER, then one fenced JSON block with
  PROPOSAL_KEYS. Never edited. The newest comment with the marker counts; when
  it is edited, progress stops (no fallback to an older one).
- Digest: SHA-256 of canonical JSON of {issue, proposal_comment_id, proposal}.
- Verdict: `Refinement: APPROVED|CHANGES REQUESTED by <agent> at <digest>`, the
  whole body. Edited verdicts and verdicts by the proposer do not count.
- Escalation: first line ESCALATION_MARKER. Reset: `Refinement reset: Anton for
  <escalation comment URL>`, valid only after the newest escalation it names.
- Exempt list (on the design issue): first line EXEMPT_START, then issue URLs.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

AGENTS = ("Claude", "Codex")
REFINEMENT_LABEL = "refinement"
PHASE_REVIEW = "refinement::review"
PHASE_CHANGES = "refinement::changes-requested"
PHASE_LABELS = (PHASE_REVIEW, PHASE_CHANGES)
PROPOSAL_MARKER = "<!-- epic-refinement v1 -->"
ESCALATION_MARKER = "<!-- epic-refinement-escalation v1 -->"
EXEMPT_START = "Refinement exempt: accepted by Anton"
PROPOSAL_KEYS = (
    "request",
    "proposer",
    "executor",
    "leaves",
    "files",
    "depends_on",
    "labels",
    "acceptance_criteria",
    "scope",
)
LIST_KEYS = ("leaves", "files", "depends_on", "labels", "acceptance_criteria")
# Rejected rounds per ticket until Anton resets; failed author runs per key.
MAX_REJECTED_ROUNDS = 3
MAX_FAILED_RUNS = 2
NO_RESET = "none"

FENCED_JSON = re.compile(r"\A```json[ \t]*\n(.*)\n```[ \t]*\Z", re.DOTALL)
VERDICT = re.compile(
    r"^Refinement: (APPROVED|CHANGES REQUESTED) by (Claude|Codex) at ([0-9a-f]{64})$"
)
RESET = re.compile(r"^Refinement reset: Anton for (https://\S+)$")
URL = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/issues/\d+\b")


@dataclass(frozen=True)
class Proposal:
    comment_id: int
    url: str
    created_at: str
    data: dict[str, Any]
    digest: str


@dataclass(frozen=True)
class Verdict:
    state: str
    by: str
    digest: str
    created_at: str
    url: str


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(issue: int, comment_id: int, proposal: dict[str, Any]) -> str:
    """Digest of one proposal comment. A new comment changes it, same content too."""
    payload = {"issue": issue, "proposal_comment_id": comment_id, "proposal": proposal}
    return hashlib.sha256(canonical(payload).encode()).hexdigest()


def parse_proposal(body: str) -> tuple[dict[str, Any] | None, str | None]:
    """The proposal in a comment body, or None and the problem.

    Returns (None, None) when the body is no proposal comment at all.
    """
    first, _, rest = (body or "").partition("\n")
    if first.strip() != PROPOSAL_MARKER:
        return None, None
    m = FENCED_JSON.match(rest.strip())
    if not m:
        return None, "proposal is not one fenced JSON block"
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as error:
        return None, f"proposal JSON does not parse: {error.msg}"
    if not isinstance(data, dict):
        return None, "proposal JSON is not an object"
    missing = [k for k in PROPOSAL_KEYS if k not in data]
    if missing:
        return None, f"proposal misses {', '.join(missing)}"
    if data["proposer"] not in AGENTS:
        return None, "proposal proposer is not Claude or Codex"
    for key in LIST_KEYS:
        value = data[key]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            return None, f"proposal {key} is not a list of strings"
    for key in ("request", "scope"):
        if not isinstance(data[key], str):
            return None, f"proposal {key} is not a string"
    if data["executor"] is not None and not isinstance(data["executor"], str):
        return None, "proposal executor is not a string"
    return data, None


def comment_id(comment: dict[str, Any]) -> int | None:
    """REST id, else the number at the end of the comment URL."""
    if isinstance(comment.get("id"), int):
        return comment["id"]
    m = re.search(r"#issuecomment-(\d+)$", comment.get("url") or "")
    return int(m.group(1)) if m else None


def by_time(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        comments or [], key=lambda c: (c.get("createdAt") or "", comment_id(c) or 0)
    )


def latest_proposal(
    issue: int, comments: list[dict[str, Any]]
) -> tuple[Proposal | None, str | None]:
    """The newest proposal comment, or None and why it blocks.

    (None, None): the issue has no proposal comment yet.
    """
    marked = [
        c
        for c in by_time(comments)
        if (c.get("body") or "").partition("\n")[0].strip() == PROPOSAL_MARKER
    ]
    if not marked:
        return None, None
    newest = marked[-1]
    url = newest.get("url") or ""
    if newest.get("includesCreatedEdit"):
        return None, f"newest proposal {url} was edited; post a new one"
    data, problem = parse_proposal(newest.get("body") or "")
    if data is None:
        return None, f"newest proposal {url}: {problem}"
    cid = comment_id(newest)
    if cid is None:
        return None, f"newest proposal {url} has no comment id"
    return (
        Proposal(
            cid, url, newest.get("createdAt") or "", data, digest(issue, cid, data)
        ),
        None,
    )


def is_enrolled(labels: set[str], comments: list[dict[str, Any]]) -> bool:
    """Label `refinement`, or a proposal comment (removing the label keeps it)."""
    if REFINEMENT_LABEL in labels:
        return True
    return any(
        (c.get("body") or "").partition("\n")[0].strip() == PROPOSAL_MARKER
        for c in comments or []
    )


def verdicts(comments: list[dict[str, Any]]) -> list[Verdict]:
    """Unedited refinement verdicts, oldest first."""
    found = []
    for c in by_time(comments):
        if c.get("includesCreatedEdit"):
            continue
        m = VERDICT.fullmatch(c.get("body") or "")
        if m:
            found.append(
                Verdict(
                    m.group(1),
                    m.group(2),
                    m.group(3),
                    c.get("createdAt") or "",
                    c.get("url") or "",
                )
            )
    return found


def current_verdict(
    proposal: Proposal, comments: list[dict[str, Any]]
) -> Verdict | None:
    """The newest valid verdict for this proposal's digest, not by its proposer."""
    valid = [
        v
        for v in verdicts(comments)
        if v.digest == proposal.digest
        and v.by != proposal.data["proposer"]
        and v.created_at >= proposal.created_at
    ]
    return valid[-1] if valid else None


def latest_escalation(comments: list[dict[str, Any]]) -> dict[str, Any] | None:
    marked = [
        c
        for c in by_time(comments)
        if (c.get("body") or "").partition("\n")[0].strip() == ESCALATION_MARKER
    ]
    return marked[-1] if marked else None


def active_reset(comments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Anton's valid reset of the newest escalation, or None.

    Only an unedited, one-line reset after the newest escalation that names its
    URL counts. An old reset never clears a newer escalation.
    """
    escalation = latest_escalation(comments)
    if escalation is None or not escalation.get("url"):
        return None
    for c in reversed(by_time(comments)):
        if (c.get("createdAt") or "") <= (escalation.get("createdAt") or ""):
            break
        if c.get("includesCreatedEdit"):
            continue
        m = RESET.fullmatch(c.get("body") or "")
        if m and m.group(1) == escalation["url"]:
            return c
    return None


def counting_start(comments: list[dict[str, Any]]) -> str:
    """Rounds count after the active reset; without one, from the start."""
    reset = active_reset(comments)
    return (reset.get("createdAt") or "") if reset else ""


def rejected_rounds(comments: list[dict[str, Any]]) -> int:
    """Distinct digests with CHANGES REQUESTED after the active reset.

    Rounds count across proposal revisions; a new proposal does not reset them.
    """
    start = counting_start(comments)
    return len(
        {
            v.digest
            for v in verdicts(comments)
            if v.state == "CHANGES REQUESTED" and v.created_at > start
        }
    )


def escalated(comments: list[dict[str, Any]]) -> bool:
    """An escalation exists that no valid reset has cleared."""
    return latest_escalation(comments) is not None and active_reset(comments) is None


def attempt_key(
    issue: int, comments: list[dict[str, Any]], proposal: Proposal | None
) -> str:
    """`refine:<issue>:<revision>:<reset comment ID>` for the shared attempt store.

    Revision is the proposal comment ID, `0` before the first proposal; the
    reset part is NO_RESET before any active reset.
    """
    revision = str(proposal.comment_id) if proposal else "0"
    reset = active_reset(comments)
    reset_id = comment_id(reset) if reset else None
    return f"refine:{issue}:{revision}:{reset_id if reset_id is not None else NO_RESET}"


def exempt_issues(comments: list[dict[str, Any]]) -> frozenset[str]:
    """Issue URLs in the newest unedited exempt-list comment of the design issue."""
    lists = [
        c
        for c in by_time(comments)
        if (c.get("body") or "").partition("\n")[0].strip() == EXEMPT_START
    ]
    if not lists or lists[-1].get("includesCreatedEdit"):
        return frozenset()
    return frozenset(URL.findall(lists[-1].get("body") or ""))
