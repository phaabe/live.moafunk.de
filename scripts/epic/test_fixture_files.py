"""Runner fixtures must contain every file their runner needs.

The runner tests copy a list of real files into a temporary checkout and
write stubs for the rest. When a runner starts to need a new file, an old
list makes the tick stop early, and the tests fail for the wrong reason
(issue 593: test_conflict_routing lacked .codex/review_delivery.py).

Each case builds a fixture's checkout with the fixture's own setUp and reads
it without running a tick:
  - every repository file a runner script names exists,
  - every local module a Python file or a runner script imports exists where
    Python looks for it,
  - every name taken from a module (`from m import a`, `m.a`) is defined
    there, so a stub offers what its real callers read.

Run: python3 -m unittest discover -s scripts/epic
"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import ast
import re
import shutil
import sys
import unittest
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DIRS = ("scripts/epic", ".codex")
# Repository files a runner script names: `scripts/epic/x.py`, `${repo_root}/.codex/x.md`,
# `${code_root}/scripts/epic/x.py` (the pinned runtime or the checkout).
SCRIPT_PATH = re.compile(
    r"(?<![\w.$}/-])(?:\$\{(?:repo|code)_root\}/|\$(?:repo|code)_root/)?"
    r"((?:scripts/epic|\.codex|\.claude)/[\w./-]+\.(?:py|sh|json|md))"
)
# Imports in Python snippets inside a runner script (`python3 -c`, heredocs).
SCRIPT_IMPORT = re.compile(
    r"^\s*(?:from\s+(\w+)\s+import\s+([\w ,]+)|import\s+(\w+))", re.M
)
OPTIONAL = {"ImportError", "ModuleNotFoundError"}


def local_modules() -> set[str]:
    return {path.stem for rel in DIRS for path in (ROOT / rel).glob("*.py")}


class Module:
    """One Python file of the checkout, read with ast."""

    def __init__(self, path: Path, checkout: Path) -> None:
        self.path = path
        self.source = path.read_text()
        self.tree = ast.parse(self.source, str(path))
        self.dirs = [path.parent]
        if "sys.path.insert" in self.source:
            if "scripts/epic" in self.source or '"scripts" / "epic"' in self.source:
                self.dirs.append(checkout / "scripts/epic")
            if ".codex" in self.source:
                self.dirs.append(checkout / ".codex")
        # A stub may import from the real repository by absolute path.
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith("/") and Path(node.value).is_dir():
                    self.dirs.append(Path(node.value))

    def imports(self) -> Iterator[tuple[str, list[str], str | None]]:
        """(module, names taken with `from`, alias bound by `import`).

        Only imports that run when the file loads. An import inside a function
        is deliberate: the module works in a fixture that lacks that file until
        the code path runs (see next_action.py). An import guarded by
        `except ImportError` is optional.
        """
        for node in load_time(self.tree.body):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    yield top, [], alias.asname or top
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [a.name for a in node.names if a.name != "*"]
                yield node.module.split(".")[0], names, None

    def attributes(self, alias: str) -> set[str]:
        return {
            node.attr
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == alias
        }

    def defined(self) -> set[str] | None:
        """Top-level names; None when the module may define more at run time."""
        names: set[str] = set()
        for node in self.tree.body:
            names |= bound(node)
        if "__getattr__" in names or "globals()" in self.source:
            return None
        return names

    def proxy(self) -> str | None:
        """`sys.modules[__name__] = real`: the module that stands in for this one."""
        if "sys.modules[__name__]" not in self.source:
            return None
        for top, _, alias in self.imports():
            if top.startswith("real_") or alias == "real":
                return top
        return None


def load_time(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Statements that run on import: not in functions, not optional imports."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield node
        if isinstance(node, ast.Try):
            optional = any(OPTIONAL & set(handler_names(h)) for h in node.handlers)
            if not optional:
                yield from load_time(node.body)
            for handler in node.handlers:
                yield from load_time(handler.body)
            yield from load_time(node.orelse + node.finalbody)
        elif isinstance(node, (ast.If, ast.With, ast.For, ast.While)):
            yield from load_time(node.body + getattr(node, "orelse", []))
        elif isinstance(node, ast.ClassDef):
            yield from load_time(node.body)


def handler_names(handler: ast.ExceptHandler) -> list[str]:
    kind = handler.type
    items = kind.elts if isinstance(kind, ast.Tuple) else [kind] if kind else []
    return [i.id for i in items if isinstance(i, ast.Name)]


def bound(node: ast.AST) -> set[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return {(a.asname or a.name).split(".")[0] for a in node.names}
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
    names: set[str] = set()
    # Names bound inside top-level if/try blocks (version or import fallbacks).
    for field in ("body", "orelse", "finalbody", "handlers"):
        for child in getattr(node, field, []) or []:
            names |= bound(child)
    return names


def problems(checkout: Path, runners: list[str]) -> list[str]:
    """Everything the runners need that the checkout lacks; empty when complete."""
    found: list[str] = []
    local = local_modules() | {p.stem for p in checkout.rglob("*.py")}
    scripts = [checkout / r for r in runners]
    seen: set[Path] = set()
    while scripts:
        script = scripts.pop()
        if script in seen:
            continue
        seen.add(script)
        # Comments name files too (".claude/hooks/scripts/epic_guard.py reads ...").
        text = re.sub(r"(?m)^\s*#.*$", "", script.read_text())
        for rel in sorted(set(SCRIPT_PATH.findall(text))):
            path = checkout / rel
            if not path.exists():
                found.append(f"{script.relative_to(checkout)} calls missing {rel}")
            elif path.suffix == ".sh":
                scripts.append(path)
        for match in SCRIPT_IMPORT.finditer(text):
            name = match.group(1) or match.group(3)
            if name not in local:
                continue
            rel = script.relative_to(checkout)
            target = next(
                (
                    checkout / d / f"{name}.py"
                    for d in DIRS
                    if (checkout / d / f"{name}.py").exists()
                ),
                None,
            )
            if target is None:
                found.append(f"{rel} imports missing {name}")
                continue
            # `from m import a as b, c`: the names a and c must exist in m.
            used = {
                n.split()[0] for n in (match.group(2) or "").split(",") if n.strip()
            }
            defined = names_of(Module(target, checkout), checkout, {})
            missing = [] if defined is None else sorted(used - defined)
            if missing:
                found.append(f"{rel} uses {name}.{', '.join(missing)}: not in the file")
    modules = {
        p: Module(p, checkout)
        for p in sorted(checkout.rglob("*.py"))
        if "__pycache__" not in p.parts
    }
    for module in modules.values():
        rel = module.path.relative_to(checkout)
        for name, names, alias in module.imports():
            if name not in local:
                continue
            target = next(
                (d / f"{name}.py" for d in module.dirs if (d / f"{name}.py").exists()),
                None,
            )
            if target is None:
                found.append(f"{rel} imports missing {name}")
                continue
            if target.resolve().is_relative_to(ROOT.resolve()):
                continue  # the real repository: complete by definition
            provider = modules.get(target) or Module(target, checkout)
            used = set(names) | (module.attributes(alias) if alias else set())
            defined = names_of(provider, checkout, modules)
            missing = [] if defined is None else sorted(used - defined)
            if missing:
                found.append(f"{rel} uses {name}.{', '.join(missing)}: not in the stub")
    return found


def names_of(
    module: Module, checkout: Path, modules: dict[Path, Module]
) -> set[str] | None:
    names = module.defined()
    real = module.proxy()
    if names is None or real is None:
        return names
    for d in module.dirs:
        path = d / f"{real}.py"
        if path.exists():
            more = names_of(
                modules.get(path) or Module(path, checkout), checkout, modules
            )
            return None if more is None else names | more
    return names


@contextmanager
def built(cls: type[unittest.TestCase], repo: Callable[[Any], Path]) -> Generator[Path]:
    """The checkout of one fixture, set up the way its own tests do."""
    case = cls(unittest.TestLoader().getTestCaseNames(cls)[0])
    case.setUp()
    try:
        yield repo(case)
    finally:
        case.doCleanups()


def fixtures() -> list[
    tuple[str, type[unittest.TestCase], Callable[[Any], Path], list[str]]
]:
    import test_claude_cooldown
    import test_claude_tick
    import test_conflict_routing
    import test_rebase_runner
    import test_waiting

    claude = ["scripts/epic/claude-tick.sh"]
    codex = [".codex/codex-tick.sh"]
    own = lambda case: case.repo  # noqa: E731
    helper = lambda case: case.helper.repo  # noqa: E731
    return [
        (
            "conflict routing, Claude",
            test_conflict_routing.ClaudeSharedReader,
            own,
            claude,
        ),
        (
            "conflict routing, Codex",
            test_conflict_routing.CodexSharedReader,
            own,
            codex,
        ),
        ("Claude tick", test_claude_tick.ClaudeTickTest, own, claude),
        ("Claude cooldown", test_claude_cooldown.ClaudeCooldownTest, own, claude),
        ("rebase runner", test_rebase_runner.RebaseRunnerTest, own, claude),
        ("waiting, Claude", test_waiting.ClaudeRunner, helper, claude),
        ("waiting, Codex", test_waiting.CodexRunner, helper, codex),
    ]


class FixtureFiles(unittest.TestCase):
    def test_every_runner_fixture_has_what_its_runner_needs(self) -> None:
        for label, cls, repo, runners in fixtures():
            with self.subTest(label), built(cls, repo) as checkout:
                self.assertEqual(problems(checkout, runners), [])


class Guard(unittest.TestCase):
    """The check itself: it must notice each kind of gap."""

    def setUp(self) -> None:
        import test_conflict_routing

        self.checkout = self.enterContext(
            built(test_conflict_routing.CodexSharedReader, lambda c: c.repo)
        )
        self.runners = [".codex/codex-tick.sh"]
        self.assertEqual(problems(self.checkout, self.runners), [])

    def test_a_file_the_runner_calls_is_missing(self) -> None:
        (self.checkout / ".codex/review_delivery.py").unlink()
        self.assertIn(
            ".codex/codex-tick.sh calls missing .codex/review_delivery.py",
            problems(self.checkout, self.runners),
        )

    def test_a_module_a_copied_file_imports_is_missing(self) -> None:
        # Only Python code imports routing.py; the runner script never names it.
        (self.checkout / "scripts/epic/routing.py").unlink()
        self.assertIn(
            "scripts/epic/real_next_action.py imports missing routing",
            problems(self.checkout, self.runners),
        )

    def test_a_module_a_runner_snippet_imports_is_missing(self) -> None:
        (self.checkout / ".codex/tick_backoff.py").unlink()
        self.assertTrue(
            any("tick_backoff" in p for p in problems(self.checkout, self.runners))
        )

    def test_a_module_lacks_a_name_a_runner_snippet_imports(self) -> None:
        # codex-tick.sh runs `from tick_backoff import result_outcome`.
        path = self.checkout / ".codex/tick_backoff.py"
        path.write_text(path.read_text().replace("def result_outcome(", "def renamed("))
        self.assertIn(
            ".codex/codex-tick.sh uses tick_backoff.result_outcome: not in the file",
            problems(self.checkout, self.runners),
        )

    def test_a_stub_lacks_a_name_its_real_caller_reads(self) -> None:
        # The 593 case: the real review helper next to a print-only stub.
        codex = self.checkout / ".codex"
        for name in ("review_worktree.py", "feature_git.py"):
            shutil.copyfile(ROOT / ".codex" / name, codex / name)
        found = problems(self.checkout, self.runners)
        self.assertTrue(
            any(
                p.startswith(".codex/review_worktree.py uses feature_worktree.")
                and "REPO" in p
                for p in found
            ),
            found,
        )


if __name__ == "__main__":
    unittest.main(argv=sys.argv)
