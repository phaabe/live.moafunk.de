"""Exercise real runner delivery with isolated Git and GitHub boundaries."""

from __future__ import annotations

import fcntl
import json
from pathlib import Path
import shutil
import subprocess
import unittest

import test_codex_tick as tick


class DeliveryRunnerTests(tick.TickTests):
    def setUp(self) -> None:
        super().setUp()
        for name, value in vars(self).copy().items():
            if isinstance(value, Path):
                setattr(self, name, value.resolve())
        for name, value in self.env.items():
            if value.startswith("/tmp/epic-tick-"):
                self.env[name] = "/private" + value
        self.quota_clock()
        python = self.bin / "python3"
        python.write_text(
            python.read_text().replace(
                "sys.argv = sys.argv[1:]",
                "sys.argv = sys.argv[1:]\nif sys.argv[0] == '-I': sys.argv.pop(0)",
            )
        )
        selector = self.repo / "scripts/epic/next_action.py"
        selector.write_text(
            selector.read_text().replace(
                "from selector_contract import BASES, EPIC, PROJECT_API, REPO, body_digest, issue_url, other",
                "from selector_contract import *",
            )
        )
        for name in (
            "review_delivery.py",
            "review_worktree.py",
            "feature_worktree.py",
            "feature_git.py",
            "assignment.py",
        ):
            shutil.copyfile(tick.ROOT / name, self.runner.parent / name)
        lanes = self.repo / ".github/epic-lanes.yml"
        lanes.parent.mkdir()
        shutil.copyfile(tick.ROOT.parent / ".github/epic-lanes.yml", lanes)
        original = self.runner.parent / "review_worktree.py"
        original.rename(self.runner.parent / "real_review_worktree.py")
        original.write_text(
            "import os, pathlib, sys\n"
            "import real_review_worktree as implementation\n"
            "implementation.TEMP_ROOT = pathlib.Path(os.environ['TEST_REVIEW_ROOT'])\n"
            "from real_review_worktree import *\n"
            "if __name__ == '__main__': sys.exit(implementation.main())\n"
        )
        self.env["TEST_REVIEW_ROOT"] = str(self.root)
        self.git("init", "-q")
        self.git("config", "user.name", "Runner test")
        self.git("config", "user.email", "runner@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.sha = self.git("rev-parse", "HEAD").strip()
        self.git(
            "remote", "add", "origin", "https://github.com/phaabe/live.moafunk.de.git"
        )
        self.env["TEST_DECISION"] = json.dumps(
            {"action": "review", "pr": 406, "sha": self.sha}
        )
        git = self.bin / "git"
        git.write_text(
            git.read_text().replace(
                "if sys.argv[1:] != ['pull', '--ff-only']:",
                "if 'fetch' in sys.argv[1:]:\n"
                "    os.execv(REAL_GIT, [REAL_GIT, '-C', os.environ['TEST_REPO'], 'update-ref', 'FETCH_HEAD', sys.argv[-1]])\n"
                "if sys.argv[1:] != ['pull', '--ff-only']:",
            )
        )
        cleanup = self.home / ".local/libexec/codex-cleanup-git.py"
        cleanup.parent.mkdir(parents=True)
        cleanup.write_text(
            "import os, subprocess, sys\n"
            "if os.environ.get('TEST_REVIEW_CLEANUP_EXIT'): sys.exit(7)\n"
            "sys.exit(subprocess.call(['git', '-C', sys.argv[2], 'worktree', 'remove', sys.argv[4]]))\n"
        )
        self.remote = self.root / "remote.json"
        self.remote.write_text(
            json.dumps(
                {
                    "pull": {
                        "state": "open",
                        "draft": False,
                        "body": "Executor: Claude\n",
                        "title": "Review fixture",
                        "labels": [],
                        "head": {
                            "ref": "feat/review-fixture",
                            "sha": self.sha,
                            "repo": {"full_name": "phaabe/live.moafunk.de"},
                        },
                        "base": {
                            "ref": "dev/312-interim",
                            "sha": self.sha,
                            "repo": {"full_name": "phaabe/live.moafunk.de"},
                        },
                    },
                    "comments": [],
                    "posts": [],
                }
            )
        )
        self.env["TEST_DELIVERY_REMOTE"] = str(self.remote)
        gh = self.bin / "gh"
        existing = gh.read_text()
        marker = "if sys.argv[1:3] == ['api', '-i']:"
        boundary = """remote_path = pathlib.Path(os.environ['TEST_DELIVERY_REMOTE'])
remote = json.loads(remote_path.read_text())
if len(sys.argv) > 2 and '/pulls/406/reviews' in sys.argv[2]:
    print('[[]]')
    sys.exit(0)
if len(sys.argv) > 2 and sys.argv[2] == 'repos/phaabe/live.moafunk.de/pulls/406' and '--jq' not in sys.argv:
    print(json.dumps(remote['pull']))
    sys.exit(0)
if len(sys.argv) > 2 and '/issues/406/comments' in sys.argv[2]:
    if '--method' not in sys.argv:
        print(json.dumps([[] if os.environ.get('TEST_HIDE_COMMENTS') else remote['comments']]))
        sys.exit(0)
    body = sys.argv[sys.argv.index('--raw-field') + 1].removeprefix('body=')
    if os.environ.get('TEST_POST_QUOTA') and body.startswith('Review:'):
        print(json.dumps({'errors': [{'type': 'RATE_LIMITED'}]}))
        sys.exit(0)
    number = len(remote['comments']) + 1
    comment = {'id': number, 'body': body, 'html_url': f'https://github.com/phaabe/live.moafunk.de/pull/406#issuecomment-{number}', 'created_at': '2033-05-18T03:33:21Z', 'updated_at': '2033-05-18T03:33:21Z', 'user': {'login': 'phaabe'}}
    remote['comments'].append(comment)
    remote['posts'].append(body)
    remote_path.write_text(json.dumps(remote))
    if os.environ.get('TEST_POST_LOST'):
        print('response lost', file=sys.stderr)
        sys.exit(7)
    print(json.dumps(comment))
    sys.exit(0)
"""
        gh.write_text(existing.replace(marker, boundary + marker))
        codex = self.bin / "codex"
        codex.write_text(
            codex.read_text().replace(
                "if not os.environ.get('TEST_RESULT_MISSING'):",
                "artifact = pathlib.Path(os.environ['EPIC_REVIEW_DIR'])\n"
                "bundle = json.loads((artifact / 'bundle.json').read_text())\n"
                "bundle.update(status='complete', verdict='CHANGES REQUESTED', findings=['Finding one'])\n"
                "if os.environ.get('TEST_MODEL_DRAFT'): bundle['status'] = 'draft'\n"
                "bundle['comments'] = [{'body': 'Finding one', 'url': None}, {'body': 'Review: CHANGES REQUESTED by Codex at ' + bundle['sha'], 'url': None}]\n"
                "(artifact / 'bundle.json').write_text(json.dumps(bundle))\n"
                "if not os.environ.get('TEST_RESULT_MISSING'):",
            )
        )

    def git(self, *args: str) -> str:
        return subprocess.check_output(
            [tick.REAL_GIT, "-C", str(self.repo), *args],
            env=self.env,
            text=True,
            stderr=subprocess.STDOUT,
        )

    def bundle(self) -> dict[str, object]:
        return json.loads(
            (self.state / "reviews" / "406" / self.sha / "bundle.json").read_text()
        )

    def remote_state(self) -> dict[str, object]:
        return json.loads(self.remote.read_text())

    def assert_tick(self, code: int) -> None:
        result = self.run_tick()
        self.assertEqual(
            result.returncode, code, (self.state / "codex.log").read_text()
        )

    def assert_one_model(self) -> None:
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_quota_between_findings_and_verdict_resumes_without_model(self) -> None:
        self.env["TEST_POST_QUOTA"] = "1"
        self.assert_tick(75)
        self.assertEqual(self.remote_state()["posts"], ["Finding one"])
        self.assertFalse(self.record.exists())
        self.assertFalse((self.state / "codex-backoff.json").exists())
        self.assertEqual(self.bundle()["status"], "complete")
        self.assertFalse((self.root / f"moafunk-review-406-{self.sha}").exists())
        self.assertEqual(
            self.git("rev-parse", f"refs/remotes/codex-review/406/{self.sha}").strip(),
            self.sha,
        )
        self.assert_tick(0)
        self.assert_one_model()
        del self.env["TEST_POST_QUOTA"]
        self.clock.write_text("2000000900")
        self.assert_tick(0)
        self.assert_one_model()
        self.assertEqual(len(self.remote_state()["posts"]), 2)
        self.assertEqual(self.bundle()["status"], "published")
        requests = [json.loads(line) for line in self.gh_calls.read_text().splitlines()]
        self.assertFalse(any(request[:2] == ["api", "graphql"] for request in requests))

    def test_lost_post_response_reconciles_after_process_restart(self) -> None:
        self.env["TEST_POST_LOST"] = "1"
        self.assert_tick(75)
        self.assertIsNone(self.bundle()["comments"][0]["url"])
        self.assertEqual(self.remote_state()["posts"], ["Finding one"])
        del self.env["TEST_POST_LOST"]
        self.assert_tick(0)
        self.assert_one_model()
        self.assertEqual(len(self.remote_state()["posts"]), 2)
        self.assertEqual(self.bundle()["status"], "published")
        self.assert_tick(0)
        self.assert_one_model()
        self.assertEqual(len(self.remote_state()["posts"]), 2)

    def test_lost_post_waits_for_remote_visibility_without_duplicate(self) -> None:
        self.env["TEST_POST_LOST"] = "1"
        self.assert_tick(75)
        del self.env["TEST_POST_LOST"]
        self.env["TEST_HIDE_COMMENTS"] = "1"
        self.assert_tick(75)
        self.assert_one_model()
        self.assertEqual(self.remote_state()["posts"], ["Finding one"])
        self.assertFalse(self.record.exists())
        self.assertFalse((self.state / "codex-backoff.json").exists())
        del self.env["TEST_HIDE_COMMENTS"]
        self.assert_tick(0)
        self.assert_one_model()
        self.assertEqual(len(self.remote_state()["posts"]), 2)
        self.assertEqual(self.bundle()["status"], "published")

    def test_newer_conflicting_verdict_blocks_pending_delivery(self) -> None:
        self.env["TEST_POST_LOST"] = "1"
        self.assert_tick(75)
        del self.env["TEST_POST_LOST"]
        remote = self.remote_state()
        remote["comments"].append(
            {
                "id": 2,
                "body": f"Review: APPROVED by Codex at {self.sha}",
                "html_url": "https://github.com/phaabe/live.moafunk.de/pull/406#issuecomment-2",
                "created_at": "2099-01-01T00:00:00Z",
                "updated_at": "2099-01-01T00:00:00Z",
                "user": {"login": "phaabe"},
            }
        )
        self.remote.write_text(json.dumps(remote))
        self.assert_tick(75)
        self.assert_one_model()
        self.assertEqual(self.remote_state()["posts"], ["Finding one"])
        self.assertFalse(self.record.exists())

    def test_changed_head_does_not_publish_old_verdict(self) -> None:
        self.env["TEST_POST_LOST"] = "1"
        self.assert_tick(75)
        del self.env["TEST_POST_LOST"]
        remote = self.remote_state()
        remote["pull"]["head"]["sha"] = "b" * 40
        self.remote.write_text(json.dumps(remote))
        self.assert_tick(75)
        self.assert_one_model()
        self.assertEqual(self.remote_state()["posts"], ["Finding one"])
        self.assertEqual(self.bundle()["status"], "complete")
        self.assertFalse(self.record.exists())

    def test_changed_base_archives_old_bundle_and_requires_another_model(self) -> None:
        self.env["TEST_POST_LOST"] = "1"
        self.assert_tick(75)
        del self.env["TEST_POST_LOST"]
        remote = self.remote_state()
        remote["pull"]["base"]["sha"] = "b" * 40
        self.remote.write_text(json.dumps(remote))
        self.assert_tick(0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)
        self.assertEqual(self.bundle()["base"]["sha"], "b" * 40)
        archive = self.state / "reviews" / "406" / self.sha / "archive"
        [previous] = list(archive.glob("*/bundle.json"))
        self.assertEqual(json.loads(previous.read_text())["status"], "complete")

    def test_successful_model_without_complete_bundle_is_not_completed(self) -> None:
        self.env["TEST_MODEL_DRAFT"] = "1"
        self.assert_tick(75)
        self.assert_one_model()
        self.assertEqual(self.remote_state()["posts"], [])
        self.assertEqual(self.bundle()["status"], "draft")
        self.assertFalse(self.record.exists())
        self.assertFalse((self.state / "codex-backoff.json").exists())

    def test_target_lock_contention_blocks_publication_and_model(self) -> None:
        self.env["TEST_POST_LOST"] = "1"
        self.assert_tick(75)
        del self.env["TEST_POST_LOST"]
        with (self.target_locks / "406.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_tick(0)
        self.assert_one_model()
        self.assertEqual(self.remote_state()["posts"], ["Finding one"])
        self.assert_tick(0)
        self.assert_one_model()
        self.assertEqual(len(self.remote_state()["posts"]), 2)

    def test_cleanup_refusal_retains_published_review_without_repeating_model(
        self,
    ) -> None:
        self.env["TEST_REVIEW_CLEANUP_EXIT"] = "7"
        self.assert_tick(0)
        self.assertEqual(self.bundle()["status"], "published")
        self.assertTrue(self.record.exists())
        self.assertTrue((self.root / f"moafunk-review-406-{self.sha}").exists())
        self.assert_tick(0)
        self.assert_one_model()
        self.assertEqual(len(self.remote_state()["posts"]), 2)


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    return unittest.TestSuite(
        DeliveryRunnerTests(name)
        for name in DeliveryRunnerTests.__dict__
        if name.startswith("test_")
    )


if __name__ == "__main__":
    unittest.main()
