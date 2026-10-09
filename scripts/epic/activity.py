"""The shared agent activity contract (leaf A681.1.1 of issue 681).

Both runners write activity records with these names; the collector
(A681.1.2) reads them into its ledger. A record holds only enums, numbers,
timestamps, the selector's action name and its issue or PR: no model text,
prompts, commands or raw errors.

  activity_for(action, model_started)  the activity a tick is in
  reason(code, scope, retry_at)        a validated blocking reason
  record(event)                        the allowlisted activity record of one
                                       tick event, old (v1) events included

A selected action alone is never model work: a code or review action counts
as runner operations until the runner records the model start. Outcomes keep
their wire names (tick_events.OUTCOMES); this module adds no outcome value.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

VERSION = 1

# Activities a runner records. The collector derives the other states
# (retired, late, new, unknown) from registration and coverage; a runner
# never writes them.
ACTIVITIES = (
    "code",  # model session of a code action
    "review",  # model session of a code review
    "refine",  # model session refining a ticket
    "review-refinement",  # model session reviewing a refinement
    "runner",  # selection, preparation, validation, cleanup, runner-only actions
    "idle",  # nothing eligible, or between ticks
    "waiting",  # a recorded wait or refusal
    "paused",  # the pause file stops the runner
    "other",  # a valid action name this contract does not know
)
# Derived by the collector only.
STATES = ACTIVITIES + ("retired", "late", "new", "unknown")

MODEL_ACTIVITY = {
    "claim": "code",
    "continue": "code",
    "fix": "code",
    "fix-checks": "code",
    "resolve-conflict": "code",
    "review": "review",
    "refine": "refine",
    "review-refinement": "review-refinement",
}
RUNNER_ACTIONS = frozenset({"merge", "adopt", "set-ready", "escalate"})
# Contract events written at the model boundaries: the model ran.
MODEL_EVENTS = ("model-start", "model-end")
FIXED_ACTIVITY = {"stop": "paused", "idle": "idle", "wait": "waiting"}

REASON_CODES = (
    "pause_requested",
    "no_eligible_work",
    "dependency_wait",
    "review_wait",
    "checks_wait",
    "conflict_wait",
    "operator_wait",
    # A Ready leaf waits for claim capacity (next_action.py capacity()).
    "capacity_wait",
    "invalid_wait",
    "model_usage_limit",
    "github_quota",
    "permission_denied",
    "connection_failure",
    "ticket_not_ready",
    "target_lock",
    "retry_backoff",
    "environment_failure",
    "unknown",
)
SCOPES = ("agent", "target")
# Reason a selector action stands for when it carries no reason code.
ACTION_REASON = {"stop": "pause_requested", "idle": "no_eligible_work"}

ACTION = re.compile(r"[a-z][a-z-]{0,31}")
TICK = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
TARGET = re.compile(r"(?:pr|issue):[1-9][0-9]{0,8}")
OUTCOMES = ("ok", "blocked", "timeout", "killed", "error")
# Record keys; everything else in a tick event is dropped.
RECORD_KEYS = (
    "v",
    "event",
    "tick",
    "at",
    "action",
    "activity",
    "target",
    "outcome",
    "reason_code",
    "scope",
    "retry_at",
)


def activity_for(action: Any, model_started: bool) -> str:
    """The activity of a tick that selected `action`.

    A model action is model work only after the model started; before that
    it is preparation by the runner. An invalid name is `other`, never a
    guess.
    """
    if not isinstance(action, str) or not ACTION.fullmatch(action):
        return "other"
    if action in FIXED_ACTIVITY:
        return FIXED_ACTIVITY[action]
    if action in MODEL_ACTIVITY:
        return MODEL_ACTIVITY[action] if model_started else "runner"
    if action in RUNNER_ACTIONS:
        return "runner"
    return "other"


def utc(value: Any) -> str | None:
    """`value` as a Z timestamp, or None when it is not one."""
    if not isinstance(value, str) or not TICK.fullmatch(value):
        return None
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return value


def reason(code: Any, scope: Any, retry_at: Any = None) -> dict[str, Any]:
    """A blocking reason. An unknown code is `unknown`, an unknown scope `target`.

    `target` is the safe default: a target wait never makes the whole agent
    look blocked. An invalid retry time is dropped, not guessed.
    """
    return {
        "reason_code": code if code in REASON_CODES else "unknown",
        "scope": scope if scope in SCOPES else "target",
        "retry_at": utc(retry_at),
    }


def target_of(event: dict[str, Any]) -> str | None:
    target = event.get("target")
    if isinstance(target, str) and TARGET.fullmatch(target):
        return target
    for key, prefix in (("pr", "pr"), ("issue", "issue")):
        value = event.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 10**9:
            return f"{prefix}:{value}"
        if isinstance(value, str):
            tail = value.rsplit("/", 1)[-1]
            if tail.isdigit() and TARGET.fullmatch(f"{prefix}:{tail}"):
                return f"{prefix}:{tail}"
    return None


def record(event: Any) -> dict[str, Any] | None:
    """The allowlisted activity record of one tick event, or None.

    Reads v1 tick events (start, finish, env-block) and contract events,
    including its own output: record(record(e)) == record(e).
    Free text never passes: an `env-block` keeps only `environment_failure`
    with agent scope, and a reason code comes only from a known code field,
    never from a `reason` text.
    """
    if not isinstance(event, dict):
        return None
    kind = event.get("event")
    if not isinstance(kind, str) or not ACTION.fullmatch(kind):
        return None
    tick = utc(event.get("tick"))
    if tick is None:
        return None
    raw = event.get("action")
    action = raw if isinstance(raw, str) and ACTION.fullmatch(raw) else None
    model = (
        event.get("model_started") is True
        or kind in MODEL_EVENTS
        # A written record keeps the model activity of its action: reading
        # it again must not turn model work into runner work.
        or (
            action in MODEL_ACTIVITY and event.get("activity") == MODEL_ACTIVITY[action]
        )
    )
    out: dict[str, Any] = {
        "v": VERSION,
        "event": kind,
        "tick": tick,
        "at": utc(event.get("at")),
        "action": action,
        "activity": activity_for(action, model),
        "target": target_of(event),
        "outcome": event.get("outcome") if event.get("outcome") in OUTCOMES else None,
        "reason_code": None,
        "scope": None,
        "retry_at": None,
    }
    if kind == "start" and raw is None:
        # A tick starts before the selector runs: the runner is selecting.
        out["activity"] = "runner"
    if kind == "wait":
        # The runner refused the selected work before a model: a wait.
        out["activity"] = "waiting"
    if kind == "env-block":
        out.update(reason("environment_failure", "agent"))
        out["activity"] = "waiting"
    # A null code is no reason: a written record without one stays so.
    elif event.get("reason_code") is not None:
        out.update(
            reason(event.get("reason_code"), event.get("scope"), event.get("retry_at"))
        )
    elif action in ACTION_REASON:
        out.update(reason(ACTION_REASON[action], "agent"))
    return {key: out[key] for key in RECORD_KEYS}
