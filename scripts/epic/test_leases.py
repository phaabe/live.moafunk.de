"""Lease store tests. Run: python3 -m unittest discover -s scripts/epic"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, field
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
import unittest
from unittest import mock

import leases
from leases import (
    ALL_FILES,
    BLOCKED,
    GONE,
    INVALID,
    LOST,
    OK,
    REFUSED,
    Blocked,
    Lost,
    ProcessEvidence,
    ProcessIdentity,
    Refused,
    StopReport,
    Store,
)

HELPER = Path(__file__).resolve().parent / "leases.py"
HEAD_A = "a" * 40
HEAD_B = "b" * 40


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        self.locks = self.tmp / "state" / "target-locks"
        self.root = self.tmp / "state" / "leases" / "v1"
        self.pointer = self.home / ".local" / "state" / "epic-loop" / "leases-root"
        self.clock = Clock()
        self.store = self.make_store()
        leases.init(self.store)

    def make_store(self, root: Path | None = None) -> Store:
        return Store(root or self.root, self.pointer, 0.3, self.clock)

    def claim(self, issue: int, owner: str = "claude", **kw: Any) -> dict:
        return leases.acquire(
            self.store, action="claim", owner=owner, issue=issue, **kw
        )

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        """The real CLI with the default root and pointer, as a runner calls it."""
        env = {**os.environ, "EPIC_LOCK_DIR": str(self.locks), "HOME": str(self.home)}
        return subprocess.run(
            [sys.executable, str(HELPER), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )


class FilesTest(unittest.TestCase):
    def test_normalizes_repository_relative_globs(self) -> None:
        self.assertEqual(leases.normalize_glob(" ./scripts//epic/ "), "scripts/epic")
        self.assertEqual(leases.normalize_glob("`backend/src/**`"), "backend/src/**")
        self.assertEqual(leases.normalize_glob("a/*.py"), "a/*.py")

    def test_rejects_absolute_traversal_and_unsupported_syntax(self) -> None:
        for bad in (
            "/etc/passwd",
            "~/x",
            "a/../b",
            "..",
            "a\\b",
            "a/{b,c}",
            "a/[ab]",
            "a/x**",
            "",
            "./",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                leases.normalize_glob(bad)

    def test_files_line_from_ticket_body(self) -> None:
        body = "Intro\n\nFiles: scripts/epic/leases.py, `scripts/epic/test_leases.py`\n"
        self.assertEqual(
            leases.files_line(body),
            ["scripts/epic/leases.py", "scripts/epic/test_leases.py"],
        )
        self.assertIsNone(leases.files_line("no line here\nFiles is a word"))
        with self.assertRaises(ValueError):
            leases.files_line("Files: /abs/path")

    def test_overlap_by_literal_prefix(self) -> None:
        self.assertTrue(leases.overlaps("scripts/epic/**", "scripts/epic/leases.py"))
        self.assertTrue(leases.overlaps("scripts/*", "scripts/epic/x.py"))
        self.assertTrue(leases.overlaps(ALL_FILES, "backend/src/main.rs"))
        self.assertTrue(leases.overlaps("*.md", "backend/x.rs"))  # empty prefix
        self.assertFalse(leases.overlaps("backend/**", "frontend/**"))
        self.assertFalse(leases.overlaps("scripts/epic/a.py", "scripts/epic/b.py"))


class StoreTest(Case):
    def test_missing_store_blocks_and_is_not_created(self) -> None:
        self.store.file.unlink()
        with self.assertRaises(Blocked):
            self.claim(5)
        with self.assertRaises(Blocked):
            leases.listing(self.store)
        self.assertFalse(self.store.file.exists())

    def test_init_refuses_an_existing_store(self) -> None:
        with self.assertRaises(Refused):
            leases.init(self.store)

    def test_corrupt_store_blocks_and_stays_untouched(self) -> None:
        self.claim(5)
        for raw in ('{"schema": 1, "version": ', "[]", '{"schema": 99}'):
            with self.subTest(raw=raw):
                self.store.file.write_text(raw)
                with self.assertRaises(Blocked):
                    self.claim(6)
                with self.assertRaises(Blocked):
                    leases.check(self.store, key="impl:5", owner="claude", generation=1)
                self.assertEqual(self.store.file.read_text(), raw)

    def test_records_acquire_cannot_write_block(self) -> None:
        a = self.claim(5, files=["a/**"])
        leases.renew(
            self.store, owner="claude", key="impl:5", generation=a["generation"], pr=50
        )
        self.claim(6, owner="codex", files=["b/**"])
        leases.acquire(self.store, action="review", owner="codex", pr=50, head=HEAD_A)
        good = json.loads(self.store.file.read_text())
        floor = good["floor"]

        def impl5(d: dict) -> dict:
            return d["leases"]["impl:5"]

        mutations = {
            "impl without files": lambda d: impl5(d).update(files=[]),
            "pending slot with a PR": lambda d: impl5(d).update(slot="pending"),
            "PR slot without a PR": lambda d: d["leases"]["impl:6"].update(slot="pr"),
            "review with files": lambda d: d["leases"][f"review:50:{HEAD_A}"].update(
                files=["c/**"]
            ),
            "overlapping reservations": lambda d: d["leases"]["impl:6"].update(
                files=["a/x.py"]
            ),
            "two leases on one PR": lambda d: d["leases"]["impl:6"].update(
                pr=50, slot="pr"
            ),
            "generation not in history": lambda d: impl5(d).update(
                generation=impl5(d)["generation"] - 1
            ),
            "key does not match issue": lambda d: impl5(d).update(issue=7),
            "agent does not match owner": lambda d: impl5(d).update(agent="codex"),
            "someone else's evidence": lambda d: impl5(d).update(
                evidence=evidence("codex").to_json()
            ),
            "unknown field": lambda d: impl5(d).update(extra=1),
            "unsafe glob": lambda d: impl5(d).update(files=["../x"]),
            "generation outside the store": lambda d: d["generations"].update(
                {"impl:9": floor - 1}
            ),
            "tentative not a bool": lambda d: impl5(d).update(tentative="no"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name):
                data = json.loads(json.dumps(good))
                mutate(data)
                raw = json.dumps(data)
                self.store.file.write_text(raw)
                with self.assertRaises(Blocked):
                    self.claim(7, owner="codex", files=["z/**"])
                self.assertEqual(self.store.file.read_text(), raw)
        self.store.file.write_text(json.dumps(good))
        leases.listing(self.store)  # the unchanged store is valid

    def test_record_missing_a_field_is_corrupt(self) -> None:
        self.claim(5)
        data = json.loads(self.store.file.read_text())
        del data["leases"]["impl:5"]["owner"]
        self.store.file.write_text(json.dumps(data))
        with self.assertRaises(Blocked):
            leases.check(self.store, key="impl:5", owner="claude", generation=1)

    def test_record_without_generation_history_is_corrupt(self) -> None:
        self.claim(5)
        data = json.loads(self.store.file.read_text())
        data["generations"] = {}
        self.store.file.write_text(json.dumps(data))
        with self.assertRaises(Blocked):
            leases.listing(self.store)

    def test_crash_during_write_keeps_the_old_store(self) -> None:
        rec = self.claim(5, files=["a/**"])
        before = self.store.file.read_bytes()
        with mock.patch("leases.os.replace", side_effect=OSError("crash")):
            with self.assertRaises(Blocked):
                self.claim(6, files=["b/**"])
        self.assertEqual(self.store.file.read_bytes(), before)
        got = leases.check(
            self.store, key="impl:5", owner="claude", generation=rec["generation"]
        )
        self.assertEqual(got["issue"], 5)
        self.assertEqual(
            self.claim(6, files=["b/**"])["issue"], 6
        )  # a leftover temp file is harmless

    def test_lock_timeout_blocks(self) -> None:
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl,sys,time\n"
                "f=open(sys.argv[1],'a'); fcntl.flock(f, fcntl.LOCK_EX)\n"
                "print('held', flush=True); time.sleep(30)",
                str(self.store.lock_file),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(holder.kill)
        assert holder.stdout is not None
        self.assertEqual(holder.stdout.readline().strip(), "held")
        with self.assertRaises(Blocked):
            self.claim(5)
        holder.kill()
        holder.wait()
        self.claim(5)  # the OS freed the lock when the holder died

    def test_moved_root_blocks_until_handoff(self) -> None:
        rec = self.claim(5, files=["scripts/epic/**"])
        moved = self.make_store(self.tmp / "other" / "v1")
        with self.assertRaises(Blocked):
            leases.listing(moved)
        with self.assertRaises(Refused):
            leases.init(moved)  # never an empty ledger while old work exists
        leases.handoff(moved, self.root)
        got = leases.check(
            moved, key="impl:5", owner="claude", generation=rec["generation"]
        )
        self.assertEqual(got["files"], ["scripts/epic/**"])
        with self.assertRaises(Blocked):
            leases.listing(self.store)  # the old store is retired
        self.store = moved
        self.assertEqual(
            self.claim(6, owner="codex", files=["b/**"])["generation"],
            rec["generation"],
        )  # same floor
        leases.release(
            moved,
            key="impl:5",
            owner="claude",
            generation=rec["generation"],
            outcome="done",
        )
        self.assertEqual(
            self.claim(5, files=["a/**"])["generation"], rec["generation"] + 1
        )

    def test_interrupted_handoff_can_be_finished(self) -> None:
        rec = self.claim(5, files=["a/**"])
        real_save = Store.save
        for step in ("copy", "retire", "pointer"):
            with self.subTest(step):
                old, new = self.store, self.make_store(self.tmp / step / "v1")
                calls = []

                def save(store: Store, data: dict) -> None:
                    calls.append(store.root)
                    if (step, len(calls)) in (("copy", 1), ("retire", 2)):
                        raise Blocked("disk full")
                    real_save(store, data)

                pointer = (
                    mock.patch.object(
                        Store, "write_pointer", side_effect=Blocked("disk full")
                    )
                    if step == "pointer"
                    else nullcontext()
                )
                with mock.patch.object(Store, "save", save), pointer:
                    with self.assertRaises(Blocked):
                        leases.handoff(new, old.root)
                if step == "retire":
                    # The old store is still the source: work it takes now
                    # must reach the new store too.
                    self.claim(6, owner="codex", files=["b/**"])
                leases.handoff(new, old.root)
                got = leases.check(
                    new, key="impl:5", owner="claude", generation=rec["generation"]
                )
                self.assertEqual(got["files"], ["a/**"])
                if step == "retire":
                    self.assertIn("impl:6", new.read()["leases"])
                with self.assertRaises(Blocked):
                    leases.listing(old)
                leases.handoff(new, old.root)  # done: running it again is harmless
                self.store = new

    def test_bad_input_never_reaches_the_store(self) -> None:
        before = self.store.file.read_bytes()
        for argv in (
            ["--issue", "0"],
            ["--issue", "-3"],
            ["--issue", "5", "--head", "typo"],
            ["--issue", "5", "--files", "../x"],
        ):
            with self.subTest(argv=argv):
                out = self.cli(
                    "acquire", "--action", "claim", "--owner", "claude", *argv
                )
                self.assertEqual(out.returncode, INVALID, out.stderr)
        self.assertEqual(self.store.file.read_bytes(), before)
        self.assertEqual(self.cli("list").returncode, OK)

    def test_invalid_change_is_not_written(self) -> None:
        rec = self.claim(5, files=["a/**"])
        before = self.store.file.read_bytes()
        with self.assertRaises(ValueError):
            leases.renew(
                self.store,
                owner="claude",
                key="impl:5",
                generation=rec["generation"],
                pr=0,
            )
        with self.assertRaises(ValueError):
            with self.store.transaction() as data:
                data["leases"]["impl:5"]["files"] = []
        self.assertEqual(self.store.file.read_bytes(), before)
        leases.listing(self.store)

    def test_store_is_registered_before_it_exists(self) -> None:
        other = self.tmp / "fresh"
        store = Store(other / "v1", other / "ptr", 0.3, self.clock)
        with mock.patch.object(Store, "write_pointer", side_effect=Blocked("full")):
            with self.assertRaises(Blocked):
                leases.init(store)
        self.assertFalse(store.file.exists())  # nothing usable, nothing to split
        with self.assertRaises(Blocked):
            leases.acquire(store, action="claim", owner="claude", issue=5)

    def test_failed_save_after_registration_retries_with_a_new_floor(self) -> None:
        other = self.tmp / "fresh"
        store = Store(other / "v1", other / "ptr", 0.3, self.clock)
        with mock.patch.object(Store, "save", side_effect=Blocked("full")):
            with self.assertRaises(Blocked):
                leases.init(store)
        first = json.loads(store.pointer.read_text())["store"]
        with self.assertRaises(Refused):
            leases.init(store)
        with mock.patch(
            "leases.secrets.randbits",
            side_effect=[(first >> leases.GENERATION_BITS) - 1, 7],
        ):
            leases.init(store, recreate=True)
        self.assertEqual(store.read()["floor"], 8 << leases.GENERATION_BITS)

    def test_unregistered_store_blocks(self) -> None:
        pointer = json.loads(self.pointer.read_text())
        spare = 99 << leases.GENERATION_BITS
        pointer.update(store=spare, floors=[*pointer["floors"], spare])
        self.pointer.write_text(json.dumps(pointer))
        with self.assertRaises(Blocked):
            self.claim(5)
        self.pointer.unlink()
        with self.assertRaises(Blocked):
            leases.listing(self.store)

    def test_store_files_are_private(self) -> None:
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)


class AcquireTest(Case):
    def test_claim_takes_key_pending_slot_and_files(self) -> None:
        rec = self.claim(5, files=["scripts/epic/leases.py"])
        self.assertEqual(rec["key"], "impl:5")
        self.assertEqual(rec["slot"], "pending")
        self.assertTrue(rec["tentative"])
        self.assertEqual(rec["files"], ["scripts/epic/leases.py"])
        self.assertEqual(rec["generation"], self.store.read()["floor"] + 1)

    def test_other_instance_is_refused_same_owner_keeps_generation(self) -> None:
        first = self.claim(5)
        with self.assertRaises(Refused):
            self.claim(5, owner="claude-2")
        with self.assertRaises(Refused):
            self.claim(5, owner="codex")
        again = self.claim(5)
        self.assertEqual(again["generation"], first["generation"])
        self.assertEqual(leases.used_slots(self.store.read(), "claude", {}), 1)

    def test_adopt_of_a_pr_without_issue_line(self) -> None:
        rec = leases.acquire(self.store, action="adopt", owner="codex", pr=77)
        self.assertEqual((rec["key"], rec["slot"], rec["pr"]), ("impl:pr:77", "pr", 77))

    def test_slots_are_per_kind_and_capped(self) -> None:
        self.claim(1, files=["a/**"])
        self.claim(2, files=["b/**"])
        with self.assertRaises(Refused):
            self.claim(3, files=["c/**"])
        self.claim(3, owner="codex", files=["backend/**"])  # its own slots

    def test_open_prs_only_add_to_the_count(self) -> None:
        with self.assertRaises(Refused):
            self.claim(1, files=["w/**"], open_prs={10: None, 11: None})
        rec = self.claim(1, files=["w/**"])
        leases.renew(
            self.store,
            owner="claude",
            key="impl:1",
            generation=rec["generation"],
            pr=10,
        )
        data = self.store.read()
        # Converted once: the caller's list, stale or fresh, never frees or doubles it.
        self.assertEqual(leases.used_slots(data, "claude", {}), 1)
        self.assertEqual(leases.used_slots(data, "claude", {10: None}), 1)
        self.assertEqual(leases.used_slots(data, "claude", {10: None, 12: None}), 2)
        self.claim(2, files=["x/**"], open_prs={10: None})
        with self.assertRaises(Refused):
            self.claim(3, files=["y/**"])

    def test_missing_files_reserve_everything(self) -> None:
        self.claim(1)
        with self.assertRaises(Refused):
            self.claim(2, owner="codex", files=["backend/**"])

    def test_overlapping_files_refused_disjoint_allowed(self) -> None:
        self.claim(1, files=["scripts/epic/**"])
        with self.assertRaises(Refused):
            self.claim(2, owner="codex", files=["scripts/epic/leases.py"])
        self.claim(2, owner="codex", files=["backend/src/**"])

    def test_released_reservation_no_longer_blocks(self) -> None:
        rec = self.claim(1, files=["scripts/epic/**"])
        leases.release(
            self.store,
            key="impl:1",
            owner="claude",
            generation=rec["generation"],
            outcome="abandoned",
        )
        self.claim(2, owner="codex", files=["scripts/epic/x.py"])

    def test_needs_takeover_keeps_slot_and_files(self) -> None:
        rec = self.claim(1, files=["scripts/epic/**"])
        leases.flag(self.store, key="impl:1", reason="owner looks dead")
        with self.assertRaises(Refused):
            self.claim(2, owner="codex", files=["scripts/epic/x.py"])
        with self.assertRaises(Refused):
            self.claim(1, owner="claude-2")
        listed = leases.listing(self.store)["leases"][0]
        self.assertEqual(listed["state"], "needs-takeover")
        leases.renew(
            self.store, owner="claude", key="impl:1", generation=rec["generation"]
        )
        self.assertEqual(leases.listing(self.store)["leases"][0]["state"], "active")

    def test_expect_version(self) -> None:
        version = self.store.read()["version"]
        self.claim(1)
        with self.assertRaises(Refused):
            self.claim(2, files=["x/**"], expect_version=version)

    def test_bad_input(self) -> None:
        for kw in (
            {"action": "claim", "owner": "nobody", "issue": 1},
            {"action": "dance", "owner": "claude", "issue": 1},
            {"action": "claim", "owner": "claude"},
            {"action": "review", "owner": "claude", "pr": 1, "head": "abc"},
            {"action": "claim", "owner": "claude", "issue": 1, "files": ["/etc"]},
        ):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                leases.acquire(self.store, **kw)  # type: ignore[arg-type]


class ReviewFindingsTest(Case):
    def test_published_pr_of_a_pending_claim_counts_once(self) -> None:
        self.claim(1, files=["a/**"])
        data = self.store.read()
        self.assertEqual(leases.used_slots(data, "claude", {600: 1}), 1)
        self.assertEqual(leases.used_slots(data, "claude", {600: None}), 2)
        self.claim(2, files=["b/**"], open_prs={600: 1})
        with self.assertRaises(Refused):
            self.claim(3, files=["c/**"], open_prs={600: 1})

    def test_one_pr_belongs_to_one_implementation_lease(self) -> None:
        leases.acquire(self.store, action="adopt", owner="codex", pr=9, files=["a/**"])
        rec = self.claim(5, files=["b/**"])
        with self.assertRaises(Refused):
            leases.renew(
                self.store,
                owner="claude",
                key="impl:5",
                generation=rec["generation"],
                pr=9,
            )
        with self.assertRaises(Refused):
            leases.acquire(
                self.store,
                action="fix",
                owner="claude",
                issue=6,
                pr=9,
                files=["c/**"],
            )

    def test_reacquired_lease_is_not_tentative(self) -> None:
        rec = self.claim(5)
        self.claim(5)  # the next tick
        with self.assertRaises(Refused):
            leases.release(
                self.store,
                key="impl:5",
                owner="claude",
                generation=rec["generation"],
                outcome="stale",
            )

    def test_touch_without_evidence_drops_old_evidence(self) -> None:
        rec = self.claim(5, evidence=evidence())
        self.assertIsNotNone(rec["evidence"])
        self.assertIsNone(self.claim(5)["evidence"])  # a new tick, no evidence
        leases.renew(
            self.store,
            owner="claude",
            key="impl:5",
            generation=rec["generation"],
            evidence=evidence(),
        )
        [got] = leases.renew(self.store, owner="claude")
        self.assertIsNone(got["evidence"])
        [got] = leases.renew(self.store, owner="claude", evidence=evidence())
        self.assertEqual(got["evidence"], evidence().to_json())


class ConcurrencyTest(Case):
    """Real processes racing on the real lock file."""

    def race(self, argvs: list[list[str]]) -> list[int]:
        with ThreadPoolExecutor(len(argvs)) as pool:
            return [p.returncode for p in pool.map(lambda a: self.cli(*a), argvs)]

    def test_simultaneous_acquisition_of_one_key(self) -> None:
        owners = ["claude", "claude-2", "claude-3", "claude-4", "claude-5", "claude-6"]
        codes = self.race(
            [
                ["acquire", "--action", "claim", "--owner", o, "--issue", "9"]
                for o in owners
            ]
        )
        self.assertEqual(sorted(codes), [OK] + [REFUSED] * 5)

    def test_last_free_slot(self) -> None:
        codes = self.race(
            [
                [
                    "acquire",
                    "--action",
                    "claim",
                    "--owner",
                    "claude",
                    "--issue",
                    str(n),
                    "--files",
                    f"dir{n}/**",
                    "--open-pr",
                    "40:99",
                ]
                for n in range(1, 7)
            ]
        )
        self.assertEqual(sorted(codes), [OK] + [REFUSED] * 5)
        self.assertEqual(leases.used_slots(self.store.read(), "claude", {40: None}), 2)

    def test_overlapping_files(self) -> None:
        codes = self.race(
            [
                [
                    "acquire",
                    "--action",
                    "adopt",
                    "--owner",
                    "codex",
                    "--pr",
                    str(n),
                    "--files",
                    "scripts/epic/**",
                ]
                for n in range(50, 56)
            ]
        )
        self.assertEqual(sorted(codes), [OK] + [REFUSED] * 5)

    def test_cli_output_and_exit_codes(self) -> None:
        out = self.cli(
            "acquire", "--action", "claim", "--owner", "claude", "--issue", "5"
        )
        self.assertEqual(out.returncode, OK, out.stderr)
        lease = json.loads(out.stdout)["lease"]
        self.assertEqual(lease["key"], "impl:5")
        gen = str(lease["generation"])
        self.assertEqual(
            self.cli(
                "check", "--key", "impl:5", "--owner", "claude", "--generation", gen
            ).returncode,
            OK,
        )
        self.assertEqual(
            self.cli(
                "check", "--key", "impl:5", "--owner", "claude", "--generation", "1"
            ).returncode,
            LOST,
        )
        self.assertEqual(
            self.cli(
                "acquire", "--action", "claim", "--owner", "x", "--issue", "5"
            ).returncode,
            INVALID,
        )
        self.assertEqual(
            self.cli("takeover", "--key", "impl:5", "--to", "claude-2").returncode,
            REFUSED,
        )
        self.store.file.write_text("{")
        self.assertEqual(self.cli("list").returncode, BLOCKED)


class LifecycleTest(Case):
    def test_generation_is_never_reused(self) -> None:
        first = self.claim(5)
        leases.release(
            self.store,
            key="impl:5",
            owner="claude",
            generation=first["generation"],
            outcome="done",
        )
        second = self.claim(5)
        self.assertEqual(second["generation"], first["generation"] + 1)
        with self.assertRaises(Lost):
            leases.check(
                self.store, key="impl:5", owner="claude", generation=first["generation"]
            )
        # A lost store recreated by hand, at the same clock time.
        self.store.file.unlink()
        with self.assertRaises(Refused):
            leases.init(self.store)  # a lost store is never replaced by accident
        leases.init(self.store, recreate=True)
        third = self.claim(5)
        self.assertNotIn(
            third["generation"], (first["generation"], second["generation"])
        )
        for old in (first, second):
            with self.assertRaises(Lost):
                leases.check(
                    self.store,
                    key="impl:5",
                    owner="claude",
                    generation=old["generation"],
                )

    def test_recreated_store_never_reuses_an_incarnation(self) -> None:
        used = self.store.read()["floor"]
        self.store.file.unlink()
        repeat = (used >> leases.GENERATION_BITS) - 1
        with mock.patch("leases.secrets.randbits", side_effect=[repeat, 41]):
            leases.init(self.store, recreate=True)
        self.assertEqual(self.store.read()["floor"], 42 << leases.GENERATION_BITS)
        self.assertEqual(
            sorted(json.loads(self.pointer.read_text())["floors"]),
            sorted([used, 42 << leases.GENERATION_BITS]),
        )

    def test_stale_recheck_releases_only_a_tentative_lease(self) -> None:
        rec = self.claim(5, files=["a/**"])
        leases.release(
            self.store,
            key="impl:5",
            owner="claude",
            generation=rec["generation"],
            outcome="stale",
        )
        rec = self.claim(6, files=["a/**"])
        leases.renew(
            self.store,
            owner="claude",
            key="impl:6",
            generation=rec["generation"],
            work=True,
        )
        with self.assertRaises(Refused):
            leases.release(
                self.store,
                key="impl:6",
                owner="claude",
                generation=rec["generation"],
                outcome="stale",
            )
        with self.assertRaises(Refused):
            self.claim(7, owner="codex", files=["a/b.py"])  # files kept
        self.assertEqual(leases.used_slots(self.store.read(), "claude", {}), 1)

    def test_renew_all_and_long_session(self) -> None:
        a = self.claim(1, files=["a/**"])
        self.claim(2, files=["b/**"])
        for _ in range(3):  # every 60 s during a model session
            self.clock.now += 60
            renewed = leases.renew(self.store, owner="claude")
            self.assertEqual({r["renewed_at"] for r in renewed}, {self.clock.now})
        self.clock.now += 30 * 86_400  # no expiry because time passed
        self.assertEqual(
            leases.check(
                self.store, key="impl:1", owner="claude", generation=a["generation"]
            )["state"],
            "active",
        )
        with self.assertRaises(Refused):
            self.claim(1, owner="claude-2")

    def test_renew_wrong_generation_is_lost(self) -> None:
        rec = self.claim(1)
        with self.assertRaises(Lost):
            leases.renew(
                self.store,
                owner="claude",
                key="impl:1",
                generation=rec["generation"] - 1,
            )
        with self.assertRaises(Lost):
            leases.renew(
                self.store, owner="claude-2", key="impl:1", generation=rec["generation"]
            )

    def test_pr_link_cannot_change(self) -> None:
        rec = self.claim(1)
        leases.renew(
            self.store,
            owner="claude",
            key="impl:1",
            generation=rec["generation"],
            pr=10,
        )
        with self.assertRaises(Refused):
            leases.renew(
                self.store,
                owner="claude",
                key="impl:1",
                generation=rec["generation"],
                pr=11,
            )

    def test_extend_is_atomic_and_keeps_ownership_on_overlap(self) -> None:
        rec = self.claim(1, files=["a/**"])
        self.claim(2, owner="codex", files=["b/**"])
        gen = rec["generation"]
        got = leases.extend(
            self.store, key="impl:1", owner="claude", generation=gen, files=["c/x.py"]
        )
        self.assertEqual(got["files"], ["a/**", "c/x.py"])
        with self.assertRaises(Refused):
            leases.extend(
                self.store,
                key="impl:1",
                owner="claude",
                generation=gen,
                files=["d/**", "b/y.py"],
            )
        now = leases.check(self.store, key="impl:1", owner="claude", generation=gen)
        self.assertEqual(now["files"], ["a/**", "c/x.py"])
        with self.assertRaises(ValueError):
            leases.extend(
                self.store, key="impl:1", owner="claude", generation=gen, files=["../x"]
            )
        with self.assertRaises(Lost):
            leases.extend(
                self.store, key="impl:1", owner="codex", generation=gen, files=["e/**"]
            )

    def test_check_compares_the_real_target(self) -> None:
        rec = self.claim(5)
        gen = rec["generation"]
        leases.renew(
            self.store,
            owner="claude",
            key="impl:5",
            generation=gen,
            pr=50,
            branch="feat/5-x",
        )
        ok: dict[str, Any] = {"key": "impl:5", "owner": "claude", "generation": gen}
        leases.check(self.store, **ok, issue=5, pr=50, branch="feat/5-x")
        for bad in (
            {"repo": "someone/else"},
            {"issue": 6},
            {"pr": 51},
            {"branch": "feat/6-y"},
        ):
            with self.subTest(bad=bad), self.assertRaises(Lost):
                leases.check(self.store, **ok, **bad)  # type: ignore[arg-type]
        leases.release(self.store, **ok, outcome="done")
        with self.assertRaises(Lost):
            leases.check(self.store, **ok)

    def test_pr_write_before_the_lease_knows_the_pr_is_lost(self) -> None:
        rec = self.claim(5)
        with self.assertRaises(Lost):
            leases.check(
                self.store,
                key="impl:5",
                owner="claude",
                generation=rec["generation"],
                pr=50,
            )


class ReviewTest(Case):
    def review(self, head: str, owner: str = "claude", pr: int = 50) -> dict:
        return leases.acquire(
            self.store, action="review", owner=owner, pr=pr, head=head
        )

    def test_review_and_impl_leases_coexist_on_one_pr(self) -> None:
        leases.acquire(self.store, action="adopt", owner="codex", pr=50, issue=5)
        rec = self.review(HEAD_A)
        self.assertEqual((rec["files"], rec["slot"]), ([], None))
        self.assertEqual(leases.used_slots(self.store.read(), "claude", {}), 0)
        with self.assertRaises(Refused):
            self.review(HEAD_A, owner="claude-2")  # one reviewer per head

    def test_superseded_head_cannot_publish_a_verdict(self) -> None:
        old = self.review(HEAD_A)
        leases.check(
            self.store,
            key=old["key"],
            owner="claude",
            generation=old["generation"],
            pr=50,
            head=HEAD_A,
        )
        self.review(HEAD_B, owner="claude-2")
        with self.assertRaises(Lost):
            leases.check(
                self.store,
                key=old["key"],
                owner="claude",
                generation=old["generation"],
                pr=50,
                head=HEAD_A,
            )
        with self.assertRaises(Lost):
            leases.renew(
                self.store, owner="claude", key=old["key"], generation=old["generation"]
            )

    def test_superseded_head_can_be_acquired_again(self) -> None:
        # A stale selector or a force-push back: head A is current again.
        old = self.review(HEAD_A)
        newer = self.review(HEAD_B, owner="claude-2")
        again = self.review(HEAD_A, owner="claude-3")
        self.assertEqual(again["generation"], old["generation"] + 1)
        with self.assertRaises(Lost):
            leases.check(
                self.store,
                key=newer["key"],
                owner="claude-2",
                generation=newer["generation"],
                head=HEAD_B,
            )
        with self.assertRaises(Lost):  # the old owner's token stays dead
            leases.check(
                self.store,
                key=old["key"],
                owner="claude",
                generation=old["generation"],
                head=HEAD_A,
            )

    def test_impl_push_supersedes_reviews_of_the_old_head(self) -> None:
        impl = leases.acquire(self.store, action="fix", owner="codex", pr=50, issue=5)
        old = self.review(HEAD_A)
        leases.renew(
            self.store,
            owner="codex",
            key="impl:5",
            generation=impl["generation"],
            head=HEAD_B,
        )
        with self.assertRaises(Lost):
            leases.check(
                self.store,
                key=old["key"],
                owner="claude",
                generation=old["generation"],
                head=HEAD_A,
            )

    def test_verdict_check_needs_the_real_head(self) -> None:
        rec = self.review(HEAD_A)
        with self.assertRaises(Lost):
            leases.check(
                self.store, key=rec["key"], owner="claude", generation=rec["generation"]
            )


class NoNetworkUnderLockTest(Case):
    def test_operations_start_no_process_or_socket(self) -> None:
        with (
            mock.patch("subprocess.Popen", side_effect=AssertionError("process")),
            mock.patch("socket.socket", side_effect=AssertionError("socket")),
        ):
            rec = self.claim(5, files=["a/**"])
            gen = rec["generation"]
            leases.renew(
                self.store,
                owner="claude",
                key="impl:5",
                generation=gen,
                pr=9,
                work=True,
            )
            leases.extend(
                self.store, key="impl:5", owner="claude", generation=gen, files=["b/**"]
            )
            leases.check(self.store, key="impl:5", owner="claude", generation=gen)
            leases.listing(self.store)
            leases.release(
                self.store, key="impl:5", owner="claude", generation=gen, outcome="done"
            )


# --- Takeover -----------------------------------------------------------------


def evidence(owner: str = "claude") -> ProcessEvidence:
    return ProcessEvidence(
        owner=owner,
        provider="fake",
        wrapper=ProcessIdentity(4001, "Tue Sep 29 20:00:00 2026"),
        groups=(ProcessIdentity(4002, "Tue Sep 29 20:00:01 2026"),),
        descendants=(ProcessIdentity(4100, "Tue Sep 29 20:01:00 2026"),),
        recorded_at=1_800_000_000.0,
    )


@dataclass
class FakeProvider:
    """Policy tests only. A real provider reads live processes."""

    name: str = "fake"
    live: bool = True
    blocked: bool = True
    wrapper: str = GONE
    groups: dict[int, str] = field(default_factory=lambda: {4002: GONE})
    descendants: dict[int, str] = field(default_factory=lambda: {4100: GONE})
    escaped: tuple[ProcessIdentity, ...] = ()
    calls: list[str] = field(default_factory=list)

    def admission_blocked(self, owner: str) -> bool:
        self.calls.append(f"admission {owner}")
        return self.blocked

    def stop(self, evidence: ProcessEvidence) -> StopReport:
        self.calls.append(f"stop {evidence.owner}")
        return StopReport(self.wrapper, self.groups, self.descendants, self.escaped)


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def snapshot(tree: Path) -> dict[str, bytes]:
    """Every file in the worktree, including .git, by content."""
    return {
        str(p.relative_to(tree)): p.read_bytes()
        for p in sorted(tree.rglob("*"))
        if p.is_file()
        and "refs/epic-recovery" not in str(p)
        and not p.name.endswith(".lock")
    }


class TakeoverTest(Case):
    def setUp(self) -> None:
        super().setUp()
        self.worktree = self.tmp / "wt"
        self.worktree.mkdir()
        env_git = ["-c", "user.email=t@example.com", "-c", "user.name=t"]
        git("init", "-q", "-b", "feat/5-x", cwd=self.worktree)
        (self.worktree / "tracked.txt").write_text("one\n")
        git("add", "tracked.txt", cwd=self.worktree)
        git(*env_git, "commit", "-q", "-m", "one", cwd=self.worktree)
        # Unpushed work of every kind: staged, unstaged, untracked binary.
        (self.worktree / "staged.txt").write_text("staged\n")
        git("add", "staged.txt", cwd=self.worktree)
        (self.worktree / "tracked.txt").write_text("one\nunstaged\n")
        (self.worktree / "blob.bin").write_bytes(bytes(range(256)) * 4)
        self.head = git("rev-parse", "HEAD", cwd=self.worktree).strip()
        rec = self.claim(
            5,
            files=["a/**"],
            branch="feat/5-x",
            worktree=str(self.worktree),
            evidence=evidence(),
        )
        self.gen = rec["generation"]
        leases.renew(
            self.store,
            owner="claude",
            key="impl:5",
            generation=self.gen,
            pr=50,
            work=True,
            evidence=evidence(),
        )

    def take(self, provider: object, **kw: Any) -> dict:
        return leases.takeover(
            self.store,
            key="impl:5",
            to="claude-2",
            provider=provider,  # type: ignore[arg-type]
            locks=self.locks,
            **kw,  # type: ignore[arg-type]
        )

    def assert_refused_and_unchanged(self, provider: object) -> None:
        before = self.store.file.read_bytes()
        with self.assertRaises(Refused):
            self.take(provider)
        self.assertEqual(self.store.file.read_bytes(), before)

    def test_takeover_preserves_work_and_bumps_generation(self) -> None:
        before = snapshot(self.worktree)
        rec = self.take(FakeProvider())
        self.assertEqual((rec["owner"], rec["generation"]), ("claude-2", self.gen + 1))
        self.assertEqual((rec["pr"], rec["files"], rec["slot"]), (50, ["a/**"], "pr"))
        self.assertIsNone(rec["worktree"])
        self.assertEqual(snapshot(self.worktree), before)  # index and files untouched
        handoff = rec["handoffs"][0]
        self.assertEqual(handoff["head"], self.head)
        self.assertEqual(handoff["branch"], "feat/5-x")
        self.assertIn("blob.bin", handoff["status"])
        self.assertIn("staged.txt", handoff["status"])
        pinned = git("rev-parse", handoff["recovery_ref"], cwd=self.worktree).strip()
        self.assertEqual(pinned, self.head)
        with self.assertRaises(Lost):
            leases.check(self.store, key="impl:5", owner="claude", generation=self.gen)
        leases.check(
            self.store, key="impl:5", owner="claude-2", generation=self.gen + 1
        )

    def test_recovery_record_survives_release_and_reacquire(self) -> None:
        rec = self.take(FakeProvider())
        leases.release(
            self.store,
            key="impl:5",
            owner="claude-2",
            generation=rec["generation"],
            outcome="abandoned",
        )
        again = self.claim(5, files=["a/**"])
        [kept] = again["handoffs"]
        self.assertEqual(kept["worktree"], str(self.worktree))
        self.assertIn("blob.bin", kept["status"])
        with self.assertRaises(ValueError):
            leases.clear_recovery(self.store, key="impl:5", index=1)
        cleared = leases.clear_recovery(self.store, key="impl:5", index=0)
        self.assertEqual(cleared["handoffs"], [])

    def test_no_provider_or_fixture_provider_refuses(self) -> None:
        self.assert_refused_and_unchanged(None)
        self.assert_refused_and_unchanged(FakeProvider(live=False))
        with mock.patch.dict(os.environ, {leases.PROVIDER_ENV: ""}):
            self.assertIsNone(leases.load_provider())

    def test_evidence_of_an_earlier_session_refuses(self) -> None:
        # Tick 2 renewed without evidence: the recorded PIDs say nothing
        # about the session running now.
        leases.renew(self.store, owner="claude")
        provider = FakeProvider()
        self.assert_refused_and_unchanged(provider)
        self.assertEqual(provider.calls, [])

    def test_work_without_recorded_worktree_refuses(self) -> None:
        rec = leases.acquire(
            self.store,
            action="adopt",
            owner="claude",
            pr=60,
            files=["q/**"],
            evidence=evidence(),
        )
        leases.renew(
            self.store,
            owner="claude",
            key="impl:pr:60",
            generation=rec["generation"],
            work=True,
            evidence=evidence(),
        )
        with self.assertRaises(Refused):
            leases.takeover(
                self.store,
                key="impl:pr:60",
                to="claude-2",
                provider=FakeProvider(),
                locks=self.locks,
            )

    def test_failing_provider_refuses(self) -> None:
        class Broken(FakeProvider):
            def stop(self, evidence: ProcessEvidence) -> StopReport:
                raise PermissionError("EPERM")

        self.assert_refused_and_unchanged(Broken())
        bad = self.tmp / "provider.py"
        bad.write_text("raise RuntimeError('boom')\n")
        with self.assertRaises(Refused):
            leases.load_provider({leases.PROVIDER_ENV: str(bad)})

    def test_legacy_record_without_evidence_refuses(self) -> None:
        rec = self.claim(6, files=["z/**"], worktree=str(self.worktree))
        provider = FakeProvider()
        with self.assertRaises(Refused):
            leases.takeover(
                self.store,
                key="impl:6",
                to="claude-2",
                provider=provider,
                locks=self.locks,
            )
        self.assertEqual(provider.calls, [])  # nothing signalled
        self.assertEqual(rec["evidence"], None)

    def test_each_unproved_stop_refuses(self) -> None:
        cases = {
            "no admission block": FakeProvider(blocked=False),
            "live wrapper": FakeProvider(wrapper="alive"),
            "killed wrapper, live child group": FakeProvider(groups={4002: "alive"}),
            "EPERM on the group": FakeProvider(groups={4002: "eperm"}),
            "reused process identity": FakeProvider(wrapper="reused"),
            "descendant not checked": FakeProvider(descendants={}),
            "escaped child": FakeProvider(escaped=(ProcessIdentity(5000, "x"),)),
            "probe error": FakeProvider(descendants={4100: "error"}),
        }
        for name, provider in cases.items():
            with self.subTest(name):
                self.assert_refused_and_unchanged(provider)

    def test_admission_is_checked_before_any_stop(self) -> None:
        provider = FakeProvider(blocked=False)
        with self.assertRaises(Refused):
            self.take(provider)
        self.assertEqual(provider.calls, ["admission claude"])

    def test_uncertain_publication_refuses_until_reconciled(self) -> None:
        leases.renew(
            self.store,
            owner="claude",
            key="impl:5",
            generation=self.gen,
            uncertain="comment on PR 50",
            evidence=evidence(),
        )
        self.assert_refused_and_unchanged(FakeProvider())
        leases.renew(
            self.store,
            owner="claude",
            key="impl:5",
            generation=self.gen,
            reconciled="comment on PR 50",
            evidence=evidence(),
        )
        self.assertEqual(self.take(FakeProvider())["owner"], "claude-2")

    def test_busy_target_lock_refuses(self) -> None:
        self.locks.mkdir(parents=True, exist_ok=True)
        with (self.locks / "50.lock").open("a") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            self.assert_refused_and_unchanged(FakeProvider())

    def test_missing_worktree_refuses(self) -> None:
        leases.renew(
            self.store,
            owner="claude",
            key="impl:5",
            generation=self.gen,
            worktree=str(self.tmp / "gone"),
        )
        self.assert_refused_and_unchanged(FakeProvider())

    def test_takeover_to_other_kind_or_self_is_invalid(self) -> None:
        for to in ("codex", "claude"):
            with self.subTest(to=to), self.assertRaises(ValueError):
                leases.takeover(
                    self.store,
                    key="impl:5",
                    to=to,
                    provider=FakeProvider(),
                    locks=self.locks,
                )

    def test_git_runs_outside_the_store_lock(self) -> None:
        def probe(args: list[str]) -> str:
            with self.store.lock_file.open("a") as handle:
                fcntl.flock(
                    handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                )  # raises if held
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return leases.run_git(args)

        self.take(FakeProvider(), git=probe)

    def test_owner_renewing_during_takeover_refuses(self) -> None:
        test = self

        class Racing(FakeProvider):
            def stop(self, evidence: ProcessEvidence) -> StopReport:
                test.clock.now += 1
                leases.renew(
                    test.store, owner="claude", key="impl:5", generation=test.gen
                )
                return super().stop(evidence)

        with self.assertRaises(Refused):
            self.take(Racing())
        rec = leases.check(
            self.store, key="impl:5", owner="claude", generation=self.gen
        )
        self.assertEqual(rec["handoffs"], [])


class EvidenceTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        ev = evidence()
        self.assertEqual(ProcessEvidence.from_json(ev.to_json()), ev)

    def test_rejects_incomplete_evidence(self) -> None:
        good = evidence().to_json()
        broken = [
            {k: v for k, v in good.items() if k != "groups"},
            {**good, "owner": "nobody"},
            {**good, "wrapper": {"pid": 4001}},
            {**good, "wrapper": {"pid": 4001, "start": ""}},
            {**good, "groups": [{"pid": 1, "start": "x"}]},
            {**good, "descendants": "none"},
            {**good, "extra": 1},
        ]
        for data in broken:
            with self.subTest(data=data), self.assertRaises(ValueError):
                ProcessEvidence.from_json(data)

    def test_acquire_refuses_someone_elses_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "v1", Path(tmp) / "ptr", 0.3, Clock())
            leases.init(store)
            with self.assertRaises(ValueError):
                leases.acquire(
                    store,
                    action="claim",
                    owner="claude-2",
                    issue=1,
                    evidence=evidence(),
                )


if __name__ == "__main__":
    unittest.main()
