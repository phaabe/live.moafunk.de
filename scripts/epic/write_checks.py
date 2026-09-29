"""Fresh GitHub checks right before each write of the headless Claude runner.

Cached data only nominates work (github_state.py). Right before a write, this
module reads what that write needs fresh from GitHub and refuses the write when
it no longer holds, or when the read fails. The ownership check of
https://github.com/phaabe/live.moafunk.de/issues/456 runs after this one.

Active only with EPIC_SHARED_READER=1 and EPIC_ACTION_FILE set (claude-tick.sh
sets it for the model session). Interactive sessions are not checked.

  Write                          Fresh check
  claim (board write or claim    the selector still gives this claim: Ready,
  comment on the issue)          Executor Claude, no blockers, a free slot;
                                 once In progress for Claude, owner writes pass
  board write in a continue      the named item is the tick's issue (or the
                                 PR's `Issue:` ticket), In progress for Claude
  push, PR create                PR open and not merged, branch is the PR's
                                 head; without a PR: issue In progress with
                                 Executor Claude, branch names the issue, no
                                 merged or closed PR for the branch
  verdict comment                review action for this PR and SHA; PR open,
                                 not draft, head equals the reviewed head
  other PR comment or PR write   PR open
  merge                          full merge-guard check from the trusted
                                 checkout (verdict with edit evidence, checks,
                                 files) for the head pinned in the command

Common checks: pause file, focus, assignment (Executor line) and allowed
target (the action's PR, issue or the PR's `Issue:` tickets).

Callers: .claude/hooks/scripts/epic_guard.py (every tool call) and
permission_gate.py (push and merge prompts). guard() returns None to allow or
the reason to refuse.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import github_state as gs
import next_action as na
from github_quota import QuotaExhausted

AGENT = "Claude"
PUSH_ACTIONS = {"fix", "fix-checks", "resolve-conflict", "continue", "claim"}
CREATE_ACTIONS = {"continue", "claim"}
BOARD_ACTIONS = {"claim", "continue"}
VERDICT_LINE = re.compile(
    r"^Review: (APPROVED|CHANGES REQUESTED) by (Claude|Codex) at ([0-9a-f]{40})\s*$",
    re.MULTILINE,
)
# Text that may write through git or gh. Used only when a command cannot be
# read word by word: then it is refused instead of guessed.
WRITE_HINT = re.compile(
    r"\bgit\b.*\bpush\b"
    r"|\bgh\b.*\b(?:pr|issue)\b.*\b(?:merge|comment|ready|edit|close|reopen|create|review)\b"
    r"|\bgh\b.*\bproject\b.*\bitem-"
    r"|\bgh\b.*\bapi\b.*(?:-X|--method)[ =]*(?:POST|PATCH|PUT|DELETE)"
    r"|\bgh\b.*\bapi\b.*(?:\s-[fF]\s|--field|--raw-field|--input)",
    re.DOTALL,
)
WRAPPERS = {"env", "command", "nohup", "time", "exec"}
SHELLS = {"bash", "sh", "zsh", "eval", "xargs", "python", "python3"}
HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
OPERATORS = set(";&|()<>")
# The item of a GraphQL ProjectV2 mutation: `itemId: "PVTI_..."` or `itemId=...`.
ITEM_ID = re.compile(r"\bitemId\b\W{0,4}([A-Za-z0-9_-]+)")
REPO_PATH = re.escape(na.REPO)
# gh flags that take a value, for finding the positional arguments.
GH_VALUE_FLAGS = {
    "-b", "--body", "-F", "--body-file", "-R", "--repo", "-t", "--title",
    "--match-head-commit", "-B", "--base", "-H", "--head", "--subject",
    "-A", "--author-email", "-l", "--label", "--add-label", "--remove-label",
    "-a", "--assignee", "--add-assignee", "--remove-assignee", "-r",
    "--reviewer", "--add-reviewer", "--remove-reviewer", "-m", "--milestone",
    "-p", "--project", "--add-project", "--remove-project", "-T", "--template",
    "-c", "--comment", "-X", "--method", "-f", "--raw-field", "--field",
    "--input", "-q", "--jq", "--cache", "--hostname", "--preview",
    "--id", "--field-id", "--project-id", "--text", "--number", "--date",
    "--single-select-option-id", "--iteration-id", "--owner", "--format",
}  # fmt: skip


class Unclear(Exception):
    """A command that may write but cannot be read safely."""


@dataclass
class Write:
    kind: str  # push, pr-create, merge, verdict, comment, pr-write, issue-write, board
    number: int | None = None
    sha: str | None = None
    branch: str | None = None
    delete: bool = False
    item: str | None = None  # board item: node ID or numeric REST id


def active(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return gs.enabled(values) and bool(values.get("EPIC_ACTION_FILE"))


# --- reading commands ---


def logical_lines(text: str) -> list[str]:
    return text.replace("\\\n", "").split("\n")


def segments(text: str) -> Iterator[tuple[str, str]]:
    """(command line, heredoc body) pairs. Lines after a heredoc are read too."""
    lines = logical_lines(text)
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        m = HEREDOC.search(line)
        if not m:
            yield line, ""
            continue
        tag, body = m.group(2), []
        while i < len(lines) and lines[i].strip() != tag:
            body.append(lines[i])
            i += 1
        i += 1  # the closing tag
        yield line, "\n".join(body)


def words_of(line: str) -> list[list[str]] | None:
    """Simple commands of one line, split at shell operators. None: unreadable."""
    if "$(" in line or "`" in line:
        return None
    try:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    commands: list[list[str]] = [[]]
    for token in tokens:
        if token and set(token) <= OPERATORS:
            commands.append([])
        else:
            commands[-1].append(token)
    return [c for c in commands if c]


def strip_prefix(words: list[str]) -> list[str]:
    while words and (
        re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]) or words[0] in WRAPPERS
    ):
        words = words[1:]
    return words


def read_file(path: str, cwd: str, stdin: str) -> str:
    if path == "-":
        return stdin
    try:
        return (Path(cwd) / os.path.expanduser(path)).read_text()
    except (OSError, UnicodeDecodeError):
        return ""


def split_flags(args: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    """gh flags (every value kept) and positional arguments."""
    flags: dict[str, list[str]] = {}
    positional: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--") and "=" in arg:
            name, _, value = arg.partition("=")
            flags.setdefault(name, []).append(value)
        elif arg in GH_VALUE_FLAGS and i + 1 < len(args):
            flags.setdefault(arg, []).append(args[i + 1])
            i += 1
        elif arg.startswith("-"):
            flags.setdefault(arg, []).append("")
        else:
            positional.append(arg)
        i += 1
    return flags, positional


def first(flags: dict[str, list[str]], *names: str) -> str | None:
    for name in names:
        if flags.get(name):
            return flags[name][-1]
    return None


def number_of(value: str | None) -> int | None:
    """A PR or issue number, also from a full URL. None for a branch name."""
    if not value:
        return None
    m = re.fullmatch(
        rf"(?:https://github\.com/{REPO_PATH}/(?:pull|issues)/)?(\d+)/?", value
    )
    return int(m.group(1)) if m else None


def comment_write(number: int | None, body: str) -> Write:
    m = VERDICT_LINE.search(body)
    if m:
        return Write("verdict", number, sha=m.group(3))
    return Write("comment", number)


def current_branch(cwd: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", cwd, "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def push_writes(args: list[str], cwd: str) -> list[Write]:
    delete = False
    positional: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-d", "--delete"):
            delete = True
        elif arg in ("--all", "--mirror", "--tags", "--follow-tags", "--prune"):
            raise Unclear(f"git push {arg} is not checked; push one branch")
        elif arg in ("-o", "--push-option", "--repo", "--receive-pack", "--exec"):
            i += 1
        elif not arg.startswith("-"):
            positional.append(arg)
        i += 1
    refspecs = positional[1:]
    if not refspecs:
        refspecs = ["HEAD"]
    writes = []
    for refspec in refspecs:
        src, colon, dst = refspec.lstrip("+").partition(":")
        name = dst if colon else src
        if name in ("HEAD", "@"):
            name = current_branch(cwd)
        name = name.removeprefix("refs/heads/")
        if not name:
            raise Unclear("cannot tell which branch this push writes")
        deleted = delete or (bool(colon) and not src)
        writes.append(Write("push", branch=name, delete=deleted))
    return writes


def api_writes(args: list[str], cwd: str, stdin: str) -> list[Write]:
    flags, positional = split_flags(args)
    if not positional:
        return []
    endpoint = positional[0].removeprefix("https://api.github.com/").lstrip("/")
    fields = flags.get("-f", []) + flags.get("--raw-field", [])
    fields += flags.get("-F", []) + flags.get("--field", [])
    method = (first(flags, "-X", "--method") or "").upper()
    if not method:
        method = "POST" if fields or "--input" in flags else "GET"
    if endpoint == "graphql":
        text = " ".join(fields)
        if "mutation" in text and "ProjectV2" in text:
            m = ITEM_ID.search(text)
            return [Write("board", item=m.group(1) if m else None)]
        return []
    if method == "GET":
        return []

    def body() -> str:
        for field in fields:
            name, _, value = field.partition("=")
            if name == "body":
                return (
                    read_file(value[1:], cwd, stdin) if value.startswith("@") else value
                )
        source = first(flags, "--input")
        if source:
            try:
                data = json.loads(read_file(source, cwd, stdin))
                return str(data.get("body") or "") if isinstance(data, dict) else ""
            except ValueError:
                return ""
        return ""

    if "projectsV2" in endpoint:
        m = re.search(r"projectsV2/\d+/items/(\d+)$", endpoint)
        return [Write("board", item=m.group(1) if m else None)]
    repo = rf"repos/{REPO_PATH}"
    if m := re.fullmatch(rf"{repo}/issues/(\d+)/comments", endpoint):
        return [comment_write(int(m.group(1)), body())]
    if re.fullmatch(rf"{repo}/issues/comments/\d+", endpoint):
        raise Unclear("comment edits are not allowed in a tick; post a new comment")
    if m := re.fullmatch(rf"{repo}/issues/(\d+)(?:/.*)?", endpoint):
        return [Write("issue-write", int(m.group(1)))]
    if m := re.fullmatch(rf"{repo}/pulls/(\d+)/merge", endpoint):
        sha = next((f.partition("=")[2] for f in fields if f.startswith("sha=")), None)
        return [Write("merge", int(m.group(1)), sha=sha)]
    if m := re.fullmatch(rf"{repo}/pulls/(\d+)/reviews", endpoint):
        found = comment_write(int(m.group(1)), body())
        return [found if found.kind == "verdict" else Write("pr-write", found.number)]
    if m := re.fullmatch(rf"{repo}/pulls/(\d+)(?:/.*)?", endpoint):
        return [Write("pr-write", int(m.group(1)))]
    if re.fullmatch(rf"{repo}/pulls", endpoint):
        return [Write("pr-create")]
    return []


def gh_writes(words: list[str], cwd: str, stdin: str) -> list[Write]:
    if len(words) < 2:
        return []
    group = words[1]
    if group == "api":
        return api_writes(words[2:], cwd, stdin)
    if group == "project" and len(words) > 2 and words[2].startswith("item-"):
        if words[2] == "item-list":
            return []
        item = (
            first(split_flags(words[3:])[0], "--id")
            if words[2] == "item-edit"
            else None
        )
        return [Write("board", item=item)]
    if group not in ("pr", "issue") or len(words) < 3:
        return []
    sub = words[2]
    flags, positional = split_flags(words[3:])
    number = number_of(positional[0] if positional else None)
    if group == "pr" and sub == "merge":
        return [Write("merge", number, sha=first(flags, "--match-head-commit"))]
    if sub == "comment" or (group == "pr" and sub == "review"):
        text = first(flags, "-b", "--body") or ""
        source = first(flags, "-F", "--body-file")
        if source:
            text += "\n" + read_file(source, cwd, stdin)
        found = comment_write(number, text)
        if sub == "review" and found.kind == "comment":
            return [Write("pr-write", number)]
        return [found]
    if sub in ("ready", "edit", "close", "reopen", "lock", "unlock"):
        return [Write("pr-write" if group == "pr" else "issue-write", number)]
    if group == "pr" and sub == "create":
        return [Write("pr-create")]
    return []


def bash_writes(command: str, cwd: str) -> list[Write]:
    writes: list[Write] = []
    for line, stdin in segments(command):
        commands = words_of(line)
        if commands is None:
            if WRITE_HINT.search(line):
                raise Unclear(
                    "a write must be one plain command, without $( ) or backticks"
                )
            continue
        for words in commands:
            words = strip_prefix(words)
            if not words:
                continue
            name = os.path.basename(words[0])
            if name == "cd" and len(words) == 2:
                cwd = str(Path(cwd) / os.path.expanduser(words[1]))
            elif name in SHELLS and WRITE_HINT.search(" ".join(words[1:])):
                raise Unclear(f"a write inside {name} is not checked; run it directly")
            elif name == "git":
                i, run_cwd = 1, cwd
                while i < len(words) and words[i].startswith("-"):
                    if words[i] == "-C" and i + 1 < len(words):
                        run_cwd = str(Path(run_cwd) / words[i + 1])
                        i += 1
                    elif words[i] == "-c":
                        i += 1
                    i += 1
                if words[i : i + 1] == ["push"]:
                    writes += push_writes(words[i + 1 :], run_cwd)
            elif name == "gh":
                writes += gh_writes(words, cwd, stdin)
    return writes


def tool_writes(tool_name: str, tool_input: dict[str, Any], cwd: str) -> list[Write]:
    if tool_name == "Bash":
        command = tool_input.get("command")
        return bash_writes(command, cwd) if isinstance(command, str) else []
    raw = next(
        (
            tool_input[k]
            for k in ("issue_number", "pullNumber", "pull_number")
            if k in tool_input
        ),
        None,
    )
    number = raw if isinstance(raw, int) else number_of(str(raw or ""))
    if tool_name == "mcp__github__add_issue_comment":
        return [comment_write(number, str(tool_input.get("body") or ""))]
    if tool_name == "mcp__github__create_pull_request_review":
        found = comment_write(number, str(tool_input.get("body") or ""))
        return [found if found.kind == "verdict" else Write("pr-write", number)]
    if tool_name == "mcp__github__update_issue":
        return [Write("issue-write", number)]
    if tool_name == "mcp__github__update_pull_request":
        return [Write("pr-write", number)]
    if tool_name == "mcp__github__create_pull_request":
        return [Write("pr-create")]
    if tool_name == "mcp__github__merge_pull_request":
        return [Write("merge", number, sha=tool_input.get("sha"))]
    return []


# --- fresh checks ---


class Context:
    """The selected action and the fresh reads one tool call needs, read once."""

    def __init__(self, action: dict[str, Any], reader: Callable[[], gs.FreshReader]):
        self.action = action
        self.kind = action.get("action")
        self.pr = action.get("pr") if isinstance(action.get("pr"), int) else None
        tail = str(action.get("issue") or "").rstrip("/").rsplit("/", 1)[-1]
        self.issue = int(tail) if tail.isdigit() else None
        self._make_reader = reader
        self._reader: gs.FreshReader | None = None
        self._pulls: dict[int, dict[str, Any]] = {}
        self._items: list[dict[str, Any]] | None = None

    @property
    def reader(self) -> gs.FreshReader:
        if self._reader is None:
            self._reader = self._make_reader()
        return self._reader

    def pull(self, number: int) -> dict[str, Any]:
        if number not in self._pulls:
            self._pulls[number] = self.reader.pull(number)
        return self._pulls[number]

    def targets(self) -> set[int]:
        """The action's PR and issue, and the PR's `Issue:` tickets."""
        found = {n for n in (self.pr, self.issue) if n is not None}
        if self.pr is not None:
            found |= na.issue_numbers(self.pull(self.pr).get("body") or "")
        return found

    def items(self) -> list[dict[str, Any]]:
        if self._items is None:
            self._items = self.reader.board_items()
        return self._items

    def item(self, number: int) -> dict[str, Any] | None:
        for item in self.items():
            content = item.get("content") or {}
            if content.get("number") == number and na.ISSUE_URL.fullmatch(
                content.get("url") or ""
            ):
                return item
        return None


def common(ctx: Context) -> str | None:
    """Pause, focus and assignment of the action's target."""
    if na.PAUSE_FILE.exists():
        return f"pause file {na.PAUSE_FILE} exists"
    focus = na.read_focus(na.FOCUS_FILE)
    if ctx.pr is not None:
        pull = ctx.pull(ctx.pr)
        author = na.pr_author({"body": pull.get("body") or ""})
        if ctx.kind == "adopt":
            # Before the body edit the PR has no owner; after it, this agent.
            if author not in (None, AGENT):
                return f"PR {ctx.pr} Executor is {author}, not {AGENT}"
        else:
            expected = na.other(AGENT) if ctx.kind == "review" else AGENT
            if author != expected:
                return f"PR {ctx.pr} Executor is {author or 'not set'}, not {expected}"
        if focus:
            names = na.labels(pull)
            for n in na.issue_numbers(pull.get("body") or ""):
                names |= na.labels(ctx.reader.issue(n))
            if not names & focus:
                return f"PR {ctx.pr} left the focus"
    elif ctx.issue is not None and focus:
        if not na.labels(ctx.reader.issue(ctx.issue)) & focus:
            return f"issue {ctx.issue} left the focus"
    return None


def owned_issue(ctx: Context) -> str | None:
    """The action's issue is In progress with this agent as Executor."""
    if ctx.issue is None:
        return "the action names no issue"
    item = ctx.item(ctx.issue)
    if item is None:
        return f"issue {ctx.issue} is not on the board"
    if item.get("status") != "In progress" or item.get("executor") != AGENT:
        return (
            f"issue {ctx.issue} is {item.get('status')} with Executor "
            f"{item.get('executor')}, not In progress for {AGENT}"
        )
    return None


def claim_or_owned(ctx: Context) -> str | None:
    """Before the claim: the selector still gives it (Ready, Executor, no
    blockers, a free slot). After it: the issue is In progress for this agent."""
    item = ctx.item(ctx.issue) if ctx.issue is not None else None
    if item and item.get("status") == "In progress" and item.get("executor") == AGENT:
        return None
    focus = na.read_focus(na.FOCUS_FILE)
    enabled = na.read_actions(os.environ.get(na.ACTIONS_ENV))
    return gs.recheck(AGENT, ctx.action, focus, enabled, False, ctx.reader)


def issue_write(ctx: Context) -> str | None:
    """A write on the action's own issue in an issue tick (no PR yet)."""
    if ctx.kind == "claim":
        return claim_or_owned(ctx)
    if ctx.kind == "continue":
        return owned_issue(ctx)
    return None


def check_board(ctx: Context, write: Write) -> str | None:
    """The item must be the tick's issue and still fit the action."""
    if ctx.kind not in BOARD_ACTIONS:
        return f"a {ctx.kind} tick does not change the board"
    if not write.item:
        return "name the board item (--id, items/<id> or itemId)"
    item = next(
        (i for i in ctx.items() if write.item in (i.get("id"), str(i.get("rest_id")))),
        None,
    )
    if item is None:
        return f"board item {write.item} is not on the board"
    content = item.get("content") or {}
    number = content.get("number")
    if not na.ISSUE_URL.fullmatch(content.get("url") or ""):
        return f"board item {write.item} is not an issue of {na.REPO}"
    allowed = {ctx.issue} if ctx.pr is None else ctx.targets() - {ctx.pr}
    if number not in allowed:
        return f"board item {write.item} is issue {number}, not this tick's issue"
    if ctx.kind == "claim":
        return claim_or_owned(ctx)
    if item.get("status") != "In progress" or item.get("executor") != AGENT:
        return (
            f"issue {number} is {item.get('status')} with Executor "
            f"{item.get('executor')}, not In progress for {AGENT}"
        )
    return None


def open_pr(ctx: Context, number: int) -> str | None:
    pull = ctx.pull(number)
    if pull.get("state") != "open" or pull.get("merged_at"):
        return (
            f"PR {number} is {'merged' if pull.get('merged_at') else pull.get('state')}"
        )
    return None


def check_push(ctx: Context, write: Write) -> str | None:
    if ctx.kind not in PUSH_ACTIONS and not (write.delete and ctx.kind == "merge"):
        return f"a {ctx.kind} tick does not push"
    if ctx.pr is not None:
        pull = ctx.pull(ctx.pr)
        head = (pull.get("head") or {}).get("ref")
        if write.branch != head:
            return f"branch {write.branch} is not PR {ctx.pr}'s branch {head}"
        if write.delete:
            if pull.get("state") == "open":
                return f"PR {ctx.pr} is still open; its branch stays"
            return None
        return open_pr(ctx, ctx.pr)
    reason = owned_issue(ctx)
    if reason:
        return reason
    if write.delete:
        return "a tick without a PR deletes no branch"
    if not re.search(rf"/{ctx.issue}-", write.branch or ""):
        return f"branch {write.branch} does not name issue {ctx.issue} (<type>/{ctx.issue}-<slug>)"
    for pull in ctx.reader.pulls_for_branch(write.branch or ""):
        if pull.get("merged_at") or pull.get("state") != "open":
            return f"branch {write.branch} already has closed PR {pull.get('number')}"
    return None


def check_write(ctx: Context, write: Write) -> str | None:
    reason = common(ctx)
    if reason:
        return reason
    if write.kind == "push":
        return check_push(ctx, write)
    if write.kind == "pr-create":
        if ctx.kind not in CREATE_ACTIONS or ctx.pr is not None:
            return f"a {ctx.kind} tick opens no PR"
        return owned_issue(ctx)
    if write.kind == "merge":
        if ctx.kind != "merge" or write.number is None or write.number != ctx.pr:
            return f"merge of PR {write.number} is not this tick's action"
        if not write.sha or write.sha != ctx.action.get("sha"):
            return "the merge must pin the selected head SHA"
        errors = ctx.reader.merge_errors(write.number, write.sha)
        return f"merge guard: {'; '.join(errors)}" if errors else None
    if write.kind == "verdict":
        if ctx.kind != "review" or write.number is None or write.number != ctx.pr:
            return f"a verdict on PR {write.number} is not this tick's action"
        if write.sha != ctx.action.get("sha"):
            return "the verdict names another head than the reviewed one"
        pull = ctx.pull(write.number)
        if pull.get("state") != "open" or pull.get("draft"):
            return f"PR {write.number} is not an open, ready PR"
        if (pull.get("head") or {}).get("sha") != write.sha:
            return f"PR {write.number} head moved after the review"
        return None
    if write.kind in ("comment", "pr-write", "issue-write"):
        if write.number is None:
            return "name the PR or issue number in the command"
        if write.kind == "pr-write" and write.number != ctx.pr:
            return f"PR {write.number} is not this tick's PR"
        if write.number not in ctx.targets():
            return f"#{write.number} is not this tick's target"
        if write.number == ctx.pr:
            return open_pr(ctx, write.number)
        if ctx.pr is None and write.number == ctx.issue:
            return issue_write(ctx)
        return None
    if write.kind == "board":
        return check_board(ctx, write)
    return f"unknown write {write.kind}"


def load_action() -> dict[str, Any]:
    path = os.environ.get("EPIC_ACTION_FILE") or ""
    action = json.loads(Path(path).read_text())
    if not isinstance(action, dict):
        raise ValueError("the selected action is not an object")
    return action


def guard(
    tool_name: str,
    tool_input: dict[str, Any],
    cwd: str,
    reader: Callable[[], gs.FreshReader] | None = None,
) -> str | None:
    """None to allow the tool call, else why it is refused."""
    if not active():
        return None
    try:
        writes = tool_writes(tool_name, tool_input, cwd)
    except Unclear as error:
        return str(error)
    if not writes:
        return None

    def make_reader() -> gs.FreshReader:
        return gs.FreshReader("write-check", gs.settings().recheck)

    try:
        ctx = Context(load_action(), reader or make_reader)
        for write in writes:
            reason = check_write(ctx, write)
            if reason:
                return reason
    except (OSError, ValueError) as error:
        return f"no selected action or bad input: {error}"
    except (
        gs.ReadBlocked,
        gs.ConfigError,
        QuotaExhausted,
        subprocess.SubprocessError,
    ) as error:
        return f"fresh GitHub read failed, so the write is refused: {error}"
    return None
