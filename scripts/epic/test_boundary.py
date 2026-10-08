"""V2 boundary evidence tests. Run: python3 -m unittest discover -s scripts/epic"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import copy
from dataclasses import replace
import hashlib
from typing import Any
import unittest

import boundary
from boundary import (
    ACCEPTED_PROFILES,
    LEGACY_V1,
    BindingRef,
    BoundaryStopReportV2,
    Compatibility,
    LeaseIdentity,
    LegacyEvidence,
    NotProved,
    canonical,
    digest,
    preflight,
    preparation_is_none,
    retirement_blockers,
)
from leases import ProcessEvidence, ProcessIdentity, StopReport

FLOOR = 7 << 32
TEST_PROFILE = "test-offline-v1"
TEST_TABLE = {TEST_PROFILE: True}
ONLINE_PROFILE = "test-online-v1"
ONLINE_TABLE = {ONLINE_PROFILE: False}
BINDING_DIGEST = "b" * 64


def lease_json() -> dict[str, Any]:
    return {
        "floor": FLOOR,
        "repo": "phaabe/live.moafunk.de",
        "key": "impl:683",
        "owner": "claude-1",
        "generation": FLOOR + 3,
    }


def report_json() -> dict[str, Any]:
    """A report that proves retirement under TEST_TABLE."""
    return {
        "schema_version": 2,
        "provider": "fake-boundary",
        "capability_profile": TEST_PROFILE,
        "lease_identity": lease_json(),
        "boundary_identity": {
            "boundary_id": "bnd-1",
            "binding_digest": BINDING_DIGEST,
            "host_id": "host-1",
            "boot_id": "boot-1",
            "daemon_id": "daemon-1",
            "container_id": "c" * 64,
            "cgroup_id": "cg-1",
            "volume_id": "vol-1",
        },
        "evidence_ref": {"record_id": "rec-1", "sha256": "d" * 64},
        "journal_revision": 12,
        "controller_id": "ctl-1",
        "admission": {
            "execution": "closed",
            "publication": "closed",
            "in_flight": [{"operation_id": "op-1", "resolved": True}],
        },
        "local_stop": {
            "state": "stopped",
            "container_terminal": True,
            "parent_populated": False,
        },
        "workspace_recovery": {
            "state": "recovered",
            "artifact_id": "art-1",
            "artifact_digest": "e" * 64,
            "manifest_digest": "f" * 64,
            "recovery_record": "rr-1",
        },
        "publication": {
            "state": "disabled_offline",
            "ledger_revision": None,
            "ledger_complete": False,
            "operations": [],
        },
        "refusal_reasons": [],
        "observed_at": 1_790_000_000_000,
    }


def reconciled(data: dict[str, Any], profile: str = ONLINE_PROFILE) -> dict[str, Any]:
    data["capability_profile"] = profile
    data["publication"] = {
        "state": "reconciled",
        "ledger_revision": 4,
        "ledger_complete": True,
        "operations": [
            {"operation_id": "pub-1", "result": "succeeded"},
            {"operation_id": "pub-2", "result": "failed"},
        ],
    }
    return data


def mutated(mutate: Any) -> dict[str, Any]:
    data = report_json()
    mutate(data)
    return data


LEASE = LeaseIdentity.from_json(lease_json())
BINDING = BindingRef("bnd-1", BINDING_DIGEST)


def compat_json(profile: str = TEST_PROFILE) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "provider": "fake-boundary",
        "capability_profile": profile,
        "controller_id": "ctl-1",
    }


COMPAT = Compatibility.from_json(compat_json())
ONLINE_COMPAT = Compatibility.from_json(compat_json(ONLINE_PROFILE))


def blockers(
    data: dict[str, Any],
    table: dict[str, bool] = TEST_TABLE,
    compat: Compatibility = COMPAT,
) -> list[str]:
    return retirement_blockers(
        BoundaryStopReportV2.from_json(data), LEASE, BINDING, compat, table
    )


# Every nested object of the report, by path, for missing/extra-key cases.
GROUPS = [
    (),
    ("lease_identity",),
    ("boundary_identity",),
    ("evidence_ref",),
    ("admission",),
    ("admission", "in_flight", 0),
    ("local_stop",),
    ("workspace_recovery",),
    ("publication",),
]


def at(data: Any, path: tuple[Any, ...]) -> Any:
    for step in path:
        data = data[step]
    return data


class CanonicalTest(unittest.TestCase):
    def test_exact_digest_of_a_fixed_record(self) -> None:
        record = {"b": [1, True, None], "a": "ü", "c": {"y": 2, "x": "z"}}
        expected = '{"a":"ü","b":[1,true,null],"c":{"x":"z","y":2}}'.encode("utf-8")
        self.assertEqual(canonical(record), expected)
        self.assertEqual(digest(record), hashlib.sha256(expected).hexdigest())
        self.assertEqual(
            digest({"a": 1}),
            "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862",
        )

    def test_float_is_refused_anywhere(self) -> None:
        for record in (1.0, {"a": 0.5}, {"a": [1, {"b": 2.0}]}, float("nan")):
            with self.subTest(record=record), self.assertRaises(ValueError):
                digest(record)

    def test_other_types_and_non_string_keys_are_refused(self) -> None:
        for record in ((1, 2), {1: "a"}, {"a": b"x"}, {"a": {1, 2}}):
            with self.subTest(record=record), self.assertRaises(ValueError):
                canonical(record)

    def test_report_digest_is_stable_across_round_trip(self) -> None:
        data = report_json()
        report = BoundaryStopReportV2.from_json(data)
        self.assertEqual(report.to_json(), data)
        self.assertEqual(digest(report.to_json()), digest(data))

    def test_evidence_ref_matches_only_the_hashed_record(self) -> None:
        record = {"proof": "x", "n": 1}
        ref = boundary.EvidenceRef("rec-1", digest(record))
        self.assertTrue(ref.matches(record))
        self.assertFalse(ref.matches({"proof": "x", "n": 2}))
        self.assertFalse(ref.matches({"proof": 1.5}))


class DecoderTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        for data in (report_json(), reconciled(report_json())):
            self.assertEqual(BoundaryStopReportV2.from_json(data).to_json(), data)
        self.assertEqual(LeaseIdentity.from_json(lease_json()).to_json(), lease_json())
        self.assertEqual(BindingRef.from_json(BINDING.to_json()), BINDING)

    def test_every_group_missing_or_extra_key(self) -> None:
        for path in GROUPS:
            base = report_json()
            for key in list(at(base, path)):
                data = copy.deepcopy(base)
                del at(data, path)[key]
                with self.subTest(missing=path + (key,)), self.assertRaises(ValueError):
                    BoundaryStopReportV2.from_json(data)
            data = copy.deepcopy(base)
            at(data, path)["extra"] = None
            with self.subTest(extra=path), self.assertRaises(ValueError):
                BoundaryStopReportV2.from_json(data)

    def test_every_field_with_a_wrong_type(self) -> None:
        wrong = {str: 5, int: "5", bool: 1, list: {}, dict: []}
        for path in GROUPS:
            for key, value in at(report_json(), path).items():
                kind = type(value) if value is not None else int  # ledger_revision
                for bad in (wrong[kind], 1.5, True if kind is not bool else "true"):
                    data = report_json()
                    at(data, path)[key] = bad
                    with (
                        self.subTest(field=path + (key,), bad=bad),
                        self.assertRaises(ValueError),
                    ):
                        BoundaryStopReportV2.from_json(data)

    def test_null_only_where_allowed(self) -> None:
        nullable = {
            ("local_stop", "parent_populated"),
            ("workspace_recovery", "artifact_id"),
            ("workspace_recovery", "artifact_digest"),
            ("workspace_recovery", "manifest_digest"),
            ("workspace_recovery", "recovery_record"),
            ("publication", "ledger_revision"),
        }
        for path in GROUPS:
            for key in at(report_json(), path):
                data = report_json()
                at(data, path)[key] = None
                if path + (key,) in nullable:
                    BoundaryStopReportV2.from_json(data)
                    continue
                with self.subTest(field=path + (key,)), self.assertRaises(ValueError):
                    BoundaryStopReportV2.from_json(data)

    def test_unknown_enum_values(self) -> None:
        for path, key in (
            (("admission",), "execution"),
            (("admission",), "publication"),
            (("local_stop",), "state"),
            (("workspace_recovery",), "state"),
            (("publication",), "state"),
        ):
            for bad in ("gone", "STOPPED", ""):
                data = report_json()
                at(data, path)[key] = bad
                with (
                    self.subTest(field=path + (key,), bad=bad),
                    self.assertRaises(ValueError),
                ):
                    BoundaryStopReportV2.from_json(data)
        data = reconciled(report_json())
        data["publication"]["operations"][0]["result"] = "pending"
        with self.assertRaises(ValueError):
            BoundaryStopReportV2.from_json(data)

    def test_unknown_schema_versions(self) -> None:
        for version in (0, 3, "2", 2.0, True, None):
            data = report_json()
            data["schema_version"] = version
            with self.subTest(version=version), self.assertRaises(ValueError):
                BoundaryStopReportV2.from_json(data)

    def test_bad_identity_values(self) -> None:
        for path, key, bad in (
            (("lease_identity",), "floor", FLOOR + 1),
            (("lease_identity",), "floor", 0),
            (("lease_identity",), "generation", FLOOR),
            (("lease_identity",), "generation", FLOOR + (1 << 32)),
            (("lease_identity",), "repo", "no-slash"),
            (("lease_identity",), "owner", "Bad Owner"),
            (("lease_identity",), "key", " "),
            (("boundary_identity",), "container_id", "C" * 64),
            (("boundary_identity",), "container_id", "c" * 12),
            (("boundary_identity",), "binding_digest", "b" * 63),
            (("evidence_ref",), "sha256", "sha256:" + "d" * 57),
            ((), "journal_revision", 0),
            ((), "observed_at", -1),
            (("workspace_recovery",), "artifact_digest", "x" * 64),
            (("publication",), "ledger_revision", -1),
            ((), "refusal_reasons", [""]),
            ((), "refusal_reasons", [1]),
        ):
            data = report_json()
            at(data, path)[key] = bad
            with (
                self.subTest(field=path + (key,), bad=bad),
                self.assertRaises(ValueError),
            ):
                BoundaryStopReportV2.from_json(data)

    def test_v1_record_is_legacy(self) -> None:
        v1 = ProcessEvidence(
            "claude-1", "epic-process-v1", ProcessIdentity(100, "s"), (), (), 1.0
        ).to_json()
        with self.assertRaisesRegex(LegacyEvidence, LEGACY_V1):
            BoundaryStopReportV2.from_json(v1)
        data = report_json()
        data["schema_version"] = 1
        with self.assertRaises(LegacyEvidence):
            BoundaryStopReportV2.from_json(data)

    def test_non_object_inputs(self) -> None:
        for bad in (None, [], "x", 2):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                BoundaryStopReportV2.from_json(bad)
        for bad in (
            {"boundary_id": "b"},
            {"boundary_id": "", "binding_digest": "b" * 64},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                BindingRef.from_json(bad)


class FakeProvider:
    def __init__(
        self, live: Any = True, compat: Any = None, name: str = "fake-boundary"
    ) -> None:
        self.name = name
        self.live = live
        self.compat = compat or compat_json()
        self.prep: Any = "none"

    def compatibility(self) -> Any:
        if isinstance(self.compat, Exception):
            raise self.compat
        return self.compat

    def preparation(self, lease: LeaseIdentity) -> Any:
        if isinstance(self.prep, Exception):
            raise self.prep
        return self.prep


class PreflightTest(unittest.TestCase):
    def reasons(self, provider: Any, **kw: Any) -> list[str]:
        with self.assertRaises(NotProved) as ctx:
            preflight(provider, **kw)
        return ctx.exception.reasons

    def test_production_table_is_empty_and_read_only(self) -> None:
        self.assertEqual(dict(ACCEPTED_PROFILES), {})
        with self.assertRaises(TypeError):
            ACCEPTED_PROFILES["x"] = True  # type: ignore[index]

    def test_production_table_refuses_every_profile(self) -> None:
        for profile in (TEST_PROFILE, "docker-runc-cgroupv2", ""):
            compat = dict(FakeProvider().compat, capability_profile=profile or "p")
            with self.subTest(profile=profile):
                self.assertIn(
                    f"profile_not_accepted:{compat['capability_profile']}",
                    self.reasons(FakeProvider(compat=compat)),
                )

    def test_injected_table_accepts_only_its_profile(self) -> None:
        compat = preflight(FakeProvider(), accepted=TEST_TABLE)
        self.assertEqual(
            compat, Compatibility(2, "fake-boundary", TEST_PROFILE, "ctl-1")
        )
        other = dict(FakeProvider().compat, capability_profile="other")
        self.assertEqual(
            self.reasons(FakeProvider(compat=other), accepted=TEST_TABLE),
            ["profile_not_accepted:other"],
        )

    def test_live_must_be_exactly_true(self) -> None:
        for live in (False, 1, "true", None):
            with self.subTest(live=live):
                self.assertEqual(
                    self.reasons(FakeProvider(live=live), accepted=TEST_TABLE),
                    ["provider_not_live"],
                )

    def test_schema_one_refuses(self) -> None:
        compat = dict(FakeProvider().compat, schema_version=1)
        self.assertEqual(
            self.reasons(FakeProvider(compat=compat), accepted=TEST_TABLE),
            ["unsupported_schema:1"],
        )

    def test_provider_name_must_match(self) -> None:
        self.assertEqual(
            self.reasons(FakeProvider(name="other"), accepted=TEST_TABLE),
            ["provider_name_mismatch"],
        )

    def test_bad_compatibility_or_error_refuses(self) -> None:
        for compat in (
            RuntimeError("down"),
            {"schema_version": 2},
            dict(FakeProvider().compat, extra=1),
            dict(FakeProvider().compat, schema_version="2"),
        ):
            with self.subTest(compat=compat):
                reasons = self.reasons(FakeProvider(compat=compat), accepted=TEST_TABLE)
                self.assertEqual(len(reasons), 1)
                self.assertTrue(reasons[0].startswith("provider_error:"))

    def test_preparation_none_only_when_exact(self) -> None:
        provider = FakeProvider()
        self.assertTrue(preparation_is_none(provider, LEASE))
        for prep in ("intent", "None", None, "", RuntimeError("journal missing")):
            provider.prep = prep
            with self.subTest(prep=prep):
                self.assertFalse(preparation_is_none(provider, LEASE))


class RetirementTest(unittest.TestCase):
    def test_complete_offline_proof_passes(self) -> None:
        self.assertEqual(blockers(report_json()), [])

    def test_complete_reconciled_proof_passes(self) -> None:
        self.assertEqual(
            blockers(reconciled(report_json()), ONLINE_TABLE, ONLINE_COMPAT), []
        )
        offline_profile = reconciled(report_json(), profile=TEST_PROFILE)
        self.assertEqual(blockers(offline_profile, table=TEST_TABLE), [])

    def test_each_condition_fails_on_its_own(self) -> None:
        def lease(key: str, value: Any) -> Any:
            return lambda d: d["lease_identity"].__setitem__(key, value)

        def set_(path: tuple[str, ...], key: str, value: Any) -> Any:
            return lambda d: at(d, path).__setitem__(key, value)

        def other_floor(d: dict[str, Any]) -> None:
            # Same counter in another store incarnation. The generation holds
            # the floor bits, so it differs too.
            d["lease_identity"].update(floor=8 << 32, generation=(8 << 32) + 3)

        self.assertEqual(
            blockers(mutated(other_floor)),
            ["lease_mismatch:floor", "lease_mismatch:generation"],
        )
        cases = [
            ("generation", lease("generation", FLOOR + 4), "lease_mismatch:generation"),
            ("key", lease("key", "impl:684"), "lease_mismatch:key"),
            ("owner", lease("owner", "codex-1"), "lease_mismatch:owner"),
            ("repo", lease("repo", "other/repo"), "lease_mismatch:repo"),
            (
                "boundary id",
                set_(("boundary_identity",), "boundary_id", "bnd-2"),
                "binding_mismatch:boundary_id",
            ),
            (
                "binding digest",
                set_(("boundary_identity",), "binding_digest", "a" * 64),
                "binding_mismatch:binding_digest",
            ),
            (
                "execution open",
                set_(("admission",), "execution", "open"),
                "admission_execution:open",
            ),
            (
                "publication unknown",
                set_(("admission",), "publication", "unknown"),
                "admission_publication:unknown",
            ),
            (
                "in flight",
                lambda d: d["admission"]["in_flight"].append(
                    {"operation_id": "op-2", "resolved": False}
                ),
                "in_flight_unresolved:op-2",
            ),
            (
                "running",
                set_(("local_stop",), "state", "running"),
                "local_stop:running",
            ),
            (
                "stop unknown",
                set_(("local_stop",), "state", "unknown"),
                "local_stop:unknown",
            ),
            (
                "container",
                set_(("local_stop",), "container_terminal", False),
                "container_not_terminal",
            ),
            (
                "populated",
                set_(("local_stop",), "parent_populated", True),
                "parent_populated",
            ),
            (
                "parent unread",
                set_(("local_stop",), "parent_populated", None),
                "parent_unreadable",
            ),
            (
                "recovery failed",
                set_(("workspace_recovery",), "state", "failed"),
                "workspace_recovery:failed",
            ),
            (
                "recovery unknown",
                set_(("workspace_recovery",), "state", "unknown"),
                "workspace_recovery:unknown",
            ),
            (
                "artifact id",
                set_(("workspace_recovery",), "artifact_id", None),
                "recovery_missing:artifact_id",
            ),
            (
                "artifact digest",
                set_(("workspace_recovery",), "artifact_digest", None),
                "recovery_missing:artifact_digest",
            ),
            (
                "manifest digest",
                set_(("workspace_recovery",), "manifest_digest", None),
                "recovery_missing:manifest_digest",
            ),
            (
                "recovery record",
                set_(("workspace_recovery",), "recovery_record", None),
                "recovery_missing:recovery_record",
            ),
            (
                "publication unknown",
                set_(("publication",), "state", "unknown"),
                "publication:unknown",
            ),
            (
                "offline with operations",
                set_(
                    ("publication",),
                    "operations",
                    [{"operation_id": "pub-9", "result": "succeeded"}],
                ),
                "disabled_offline_with_operations",
            ),
            (
                "refusal",
                set_((), "refusal_reasons", ["journal_gap"]),
                "provider_refusal:journal_gap",
            ),
            (
                "profile",
                set_((), "capability_profile", "unknown-profile"),
                "profile_mismatch",
            ),
            ("provider", set_((), "provider", "other-boundary"), "provider_mismatch"),
            ("controller", set_((), "controller_id", "ctl-2"), "controller_mismatch"),
        ]
        for name, mutate, reason in cases:
            data = report_json()
            mutate(data)
            with self.subTest(name):
                self.assertEqual(blockers(data), [reason])

    def test_reconciled_publication_conditions(self) -> None:
        cases = [
            ("ledger_revision", None, "publication_ledger_incomplete"),
            ("ledger_complete", False, "publication_ledger_incomplete"),
            (
                "operations",
                [{"operation_id": "pub-3", "result": "unknown"}],
                "publication_unresolved:pub-3",
            ),
        ]
        for key, value, reason in cases:
            data = reconciled(report_json())
            data["publication"][key] = value
            with self.subTest(key):
                self.assertEqual(blockers(data, ONLINE_TABLE, ONLINE_COMPAT), [reason])

    def test_empty_operation_list_needs_a_complete_ledger(self) -> None:
        data = reconciled(report_json())
        data["publication"].update(operations=[], ledger_complete=False)
        self.assertEqual(
            blockers(data, ONLINE_TABLE, ONLINE_COMPAT),
            ["publication_ledger_incomplete"],
        )

    def test_disabled_offline_needs_a_profile_that_allows_it(self) -> None:
        data = report_json()
        data["capability_profile"] = ONLINE_PROFILE
        self.assertEqual(
            blockers(data, ONLINE_TABLE, ONLINE_COMPAT),
            ["disabled_offline_not_allowed"],
        )

    def test_conflicting_or_repeated_operation_ids_are_refused(self) -> None:
        for second in ("failed", "succeeded"):
            data = reconciled(report_json())
            data["publication"]["operations"] = [
                {"operation_id": "same-operation", "result": "succeeded"},
                {"operation_id": "same-operation", "result": second},
            ]
            with self.subTest(second), self.assertRaises(ValueError):
                BoundaryStopReportV2.from_json(data)
        report = BoundaryStopReportV2.from_json(reconciled(report_json()))
        op = report.publication.operations[0]
        with self.assertRaises(ValueError):
            replace(report.publication, operations=(op, replace(op, result="failed")))

    def test_production_table_refuses_a_complete_proof(self) -> None:
        self.assertEqual(
            blockers(report_json(), table=dict(ACCEPTED_PROFILES)),
            [f"profile_not_accepted:{TEST_PROFILE}"],
        )
        report = BoundaryStopReportV2.from_json(report_json())
        self.assertEqual(
            retirement_blockers(report, LEASE, BINDING, COMPAT),
            [f"profile_not_accepted:{TEST_PROFILE}"],
        )

    def test_report_must_match_the_preflight_compatibility(self) -> None:
        both = {ONLINE_PROFILE: False, TEST_PROFILE: True}
        online = preflight(FakeProvider(compat=compat_json(ONLINE_PROFILE)), both)
        offline = preflight(FakeProvider(), both)
        # An online provider's proof can not be an offline report.
        self.assertEqual(
            blockers(report_json(), both, online),
            ["profile_mismatch", "disabled_offline_not_allowed"],
        )
        # Nor the other way round, though both profiles are accepted.
        self.assertEqual(
            blockers(reconciled(report_json()), both, offline), ["profile_mismatch"]
        )
        foreign = report_json()
        foreign["provider"] = "other-boundary"
        self.assertEqual(blockers(foreign, both, offline), ["provider_mismatch"])
        foreign["controller_id"] = "ctl-2"
        self.assertEqual(
            blockers(foreign, both, online),
            [
                "provider_mismatch",
                "profile_mismatch",
                "controller_mismatch",
                "disabled_offline_not_allowed",
            ],
        )
        stale = replace(COMPAT, schema_version=1)
        self.assertEqual(
            blockers(report_json(), compat=stale), ["compat_unsupported_schema:1"]
        )

    def test_v1_evidence_is_legacy(self) -> None:
        v1 = ProcessEvidence(
            "claude-1", "epic-process-v1", ProcessIdentity(100, "s"), (), (), 1.0
        )
        self.assertEqual(
            retirement_blockers(v1, LEASE, BINDING, COMPAT, TEST_TABLE), [LEGACY_V1]
        )
        stop = StopReport("gone", {}, {})
        self.assertEqual(
            retirement_blockers(stop, LEASE, BINDING, COMPAT, TEST_TABLE), [LEGACY_V1]
        )

    def test_raw_json_is_not_a_report(self) -> None:
        self.assertEqual(
            retirement_blockers(report_json(), LEASE, BINDING, COMPAT, TEST_TABLE),
            ["not_v2_report:dict"],
        )


if __name__ == "__main__":
    unittest.main()
