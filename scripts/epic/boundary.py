"""V2 boundary evidence for the lease consumer: types, strict decoders,
canonical digests, the V2 provider interface, the profile preflight and the
retirement check.

Contract: the body of https://github.com/phaabe/live.moafunk.de/issues/674 at
digest `2e608943f070`, accepted in
https://github.com/phaabe/live.moafunk.de/issues/674#issuecomment-6049036580.
Not wired into leases.py or the runners yet.

Decoders are strict: an unknown version, a missing or extra key, a wrong type
or an unknown enum value is a ValueError. They never fill a default.

Digests: SHA-256 (lowercase hex) of canonical JSON: UTF-8, sorted keys, no
spaces. Hashed records hold only strings, integers, booleans, lists, objects
and null; a float is refused. Times are integer milliseconds. A digest checks
integrity only; provenance comes from reading through the provider.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from agents import ID

Json = dict[str, Any]

SCHEMA_VERSION = 2
GENERATION_BITS = 32  # same split as leases.py: floor = incarnation << 32
LEGACY_V1 = "legacy_v1_evidence"
HEX64 = re.compile(r"[0-9a-f]{64}")
REPO = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")

STOP_STATES = ("stopped", "running", "unknown")
RECOVERY_STATES = ("recovered", "failed", "unknown")
PUBLICATION_STATES = ("disabled_offline", "reconciled", "unknown")
ADMISSION_STATES = ("closed", "open", "unknown")
OPERATION_RESULTS = ("succeeded", "failed", "unknown")

# Capability profile -> whether `publication = disabled_offline` is valid.
# Empty in production. Adding a profile needs a reviewed PR and Anton's
# approval. Only Python callers (the disposable test harness) can pass another
# table; no environment variable, CLI flag or file changes it.
ACCEPTED_PROFILES: Mapping[str, bool] = MappingProxyType({})


class NotProved(Exception):
    """Preflight failed. `reasons` are machine-readable."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__(", ".join(reasons))
        self.reasons = reasons


# --- Canonical JSON ------------------------------------------------------------


def _check_hashable(value: Any, where: str) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if type(value) is int:
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _check_hashable(item, f"{where}[{i}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{where}: key {key!r} is not a string")
            _check_hashable(item, f"{where}.{key}")
        return
    raise ValueError(
        f"{where}: {type(value).__name__} is not allowed in a hashed record"
    )


def canonical(record: Any) -> bytes:
    """UTF-8, sorted keys, separators (",", ":"). Floats are refused."""
    _check_hashable(record, "record")
    return json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def digest(record: Any) -> str:
    return hashlib.sha256(canonical(record)).hexdigest()


# --- Field decoders ------------------------------------------------------------


def _obj(data: Any, what: str, keys: set[str]) -> Json:
    if not isinstance(data, dict):
        raise ValueError(f"{what} must be an object")
    if set(data) != keys:
        missing, extra = sorted(keys - set(data)), sorted(set(data) - keys)
        raise ValueError(f"{what} keys: missing {missing}, extra {extra}")
    return data


def _str(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be a non-empty string")
    return value


def _int(value: Any, what: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{what} must be an integer >= {minimum}")
    return value


def _bool(value: Any, what: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{what} must be a boolean")
    return value


def _hex(value: Any, what: str) -> str:
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise ValueError(f"{what} must be 64 lowercase hex characters")
    return value


def _enum(value: Any, what: str, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{what} must be one of {list(allowed)}")
    return value


def _opt(value: Any, decode: Any, what: str) -> Any:
    return None if value is None else decode(value, what)


def _list(value: Any, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{what} must be a list")
    return value


# --- Types ---------------------------------------------------------------------


@dataclass(frozen=True)
class LeaseIdentity:
    """One ownership generation. `floor` is the existing store incarnation."""

    floor: int
    repo: str
    key: str
    owner: str
    generation: int

    @classmethod
    def from_json(cls, data: Any) -> LeaseIdentity:
        d = _obj(
            data, "lease_identity", {"floor", "repo", "key", "owner", "generation"}
        )
        floor = _int(d["floor"], "lease_identity.floor", 1)
        if floor % (1 << GENERATION_BITS):
            raise ValueError("lease_identity.floor is not a store floor")
        gen = _int(d["generation"], "lease_identity.generation", 1)
        if not floor < gen < floor + (1 << GENERATION_BITS):
            raise ValueError("lease_identity.generation is outside its floor")
        repo = _str(d["repo"], "lease_identity.repo")
        if not REPO.fullmatch(repo):
            raise ValueError("lease_identity.repo must be owner/name")
        owner = _str(d["owner"], "lease_identity.owner")
        if not ID.fullmatch(owner):
            raise ValueError(f"bad lease_identity.owner {owner!r}")
        return cls(floor, repo, _str(d["key"], "lease_identity.key"), owner, gen)

    def to_json(self) -> Json:
        return {
            "floor": self.floor,
            "repo": self.repo,
            "key": self.key,
            "owner": self.owner,
            "generation": self.generation,
        }


@dataclass(frozen=True)
class BindingRef:
    """Written once per generation, before admission; never changes."""

    boundary_id: str
    binding_digest: str

    @classmethod
    def from_json(cls, data: Any) -> BindingRef:
        d = _obj(data, "binding", {"boundary_id", "binding_digest"})
        return cls(
            _str(d["boundary_id"], "binding.boundary_id"),
            _hex(d["binding_digest"], "binding.binding_digest"),
        )

    def to_json(self) -> Json:
        return {"boundary_id": self.boundary_id, "binding_digest": self.binding_digest}


@dataclass(frozen=True)
class BoundaryIdentity:
    """Names or paths alone are not identity: all of these together are."""

    boundary_id: str
    binding_digest: str
    host_id: str
    boot_id: str
    daemon_id: str
    container_id: str
    cgroup_id: str
    volume_id: str

    KEYS = (
        "boundary_id",
        "binding_digest",
        "host_id",
        "boot_id",
        "daemon_id",
        "container_id",
        "cgroup_id",
        "volume_id",
    )

    @classmethod
    def from_json(cls, data: Any) -> BoundaryIdentity:
        d = _obj(data, "boundary_identity", set(cls.KEYS))
        values = {k: _str(d[k], f"boundary_identity.{k}") for k in cls.KEYS}
        _hex(values["binding_digest"], "boundary_identity.binding_digest")
        _hex(values["container_id"], "boundary_identity.container_id")
        return cls(**values)

    def to_json(self) -> Json:
        return {k: getattr(self, k) for k in self.KEYS}


@dataclass(frozen=True)
class EvidenceRef:
    record_id: str
    sha256: str

    @classmethod
    def from_json(cls, data: Any) -> EvidenceRef:
        d = _obj(data, "evidence_ref", {"record_id", "sha256"})
        return cls(
            _str(d["record_id"], "evidence_ref.record_id"),
            _hex(d["sha256"], "evidence_ref.sha256"),
        )

    def to_json(self) -> Json:
        return {"record_id": self.record_id, "sha256": self.sha256}

    def matches(self, record: Any) -> bool:
        """Integrity only: the caller must have read `record` via the provider."""
        try:
            return digest(record) == self.sha256
        except ValueError:
            return False


@dataclass(frozen=True)
class InFlight:
    operation_id: str
    resolved: bool

    @classmethod
    def from_json(cls, data: Any) -> InFlight:
        d = _obj(data, "admission.in_flight[]", {"operation_id", "resolved"})
        return cls(
            _str(d["operation_id"], "admission.in_flight[].operation_id"),
            _bool(d["resolved"], "admission.in_flight[].resolved"),
        )

    def to_json(self) -> Json:
        return {"operation_id": self.operation_id, "resolved": self.resolved}


@dataclass(frozen=True)
class Admission:
    """Execution and publication admission for this generation, and every
    start/exec/restart admitted before closure."""

    execution: str
    publication: str
    in_flight: tuple[InFlight, ...]

    @classmethod
    def from_json(cls, data: Any) -> Admission:
        d = _obj(data, "admission", {"execution", "publication", "in_flight"})
        return cls(
            _enum(d["execution"], "admission.execution", ADMISSION_STATES),
            _enum(d["publication"], "admission.publication", ADMISSION_STATES),
            tuple(
                InFlight.from_json(x)
                for x in _list(d["in_flight"], "admission.in_flight")
            ),
        )

    def to_json(self) -> Json:
        return {
            "execution": self.execution,
            "publication": self.publication,
            "in_flight": [x.to_json() for x in self.in_flight],
        }


@dataclass(frozen=True)
class LocalStop:
    """`container_terminal`: terminal runtime evidence for the exact
    container. `parent_populated`: `cgroup.events` of the retained protected
    parent; null when it could not be read."""

    state: str
    container_terminal: bool
    parent_populated: bool | None

    @classmethod
    def from_json(cls, data: Any) -> LocalStop:
        d = _obj(
            data, "local_stop", {"state", "container_terminal", "parent_populated"}
        )
        return cls(
            _enum(d["state"], "local_stop.state", STOP_STATES),
            _bool(d["container_terminal"], "local_stop.container_terminal"),
            _opt(d["parent_populated"], _bool, "local_stop.parent_populated"),
        )

    def to_json(self) -> Json:
        return {
            "state": self.state,
            "container_terminal": self.container_terminal,
            "parent_populated": self.parent_populated,
        }


@dataclass(frozen=True)
class WorkspaceRecovery:
    state: str
    artifact_id: str | None
    artifact_digest: str | None
    manifest_digest: str | None
    recovery_record: str | None

    KEYS = (
        "state",
        "artifact_id",
        "artifact_digest",
        "manifest_digest",
        "recovery_record",
    )

    @classmethod
    def from_json(cls, data: Any) -> WorkspaceRecovery:
        d = _obj(data, "workspace_recovery", set(cls.KEYS))
        w = "workspace_recovery."
        return cls(
            _enum(d["state"], w + "state", RECOVERY_STATES),
            _opt(d["artifact_id"], _str, w + "artifact_id"),
            _opt(d["artifact_digest"], _hex, w + "artifact_digest"),
            _opt(d["manifest_digest"], _hex, w + "manifest_digest"),
            _opt(d["recovery_record"], _str, w + "recovery_record"),
        )

    def to_json(self) -> Json:
        return {k: getattr(self, k) for k in self.KEYS}


@dataclass(frozen=True)
class Operation:
    operation_id: str
    result: str

    @classmethod
    def from_json(cls, data: Any) -> Operation:
        d = _obj(data, "publication.operations[]", {"operation_id", "result"})
        return cls(
            _str(d["operation_id"], "publication.operations[].operation_id"),
            _enum(d["result"], "publication.operations[].result", OPERATION_RESULTS),
        )

    def to_json(self) -> Json:
        return {"operation_id": self.operation_id, "result": self.result}


@dataclass(frozen=True)
class Publication:
    """`ledger_complete`: the provider proved the trusted ledger at
    `ledger_revision` lists every admitted operation."""

    state: str
    ledger_revision: int | None
    ledger_complete: bool
    operations: tuple[Operation, ...]

    @classmethod
    def from_json(cls, data: Any) -> Publication:
        keys = {"state", "ledger_revision", "ledger_complete", "operations"}
        d = _obj(data, "publication", keys)
        return cls(
            _enum(d["state"], "publication.state", PUBLICATION_STATES),
            _opt(d["ledger_revision"], _int, "publication.ledger_revision"),
            _bool(d["ledger_complete"], "publication.ledger_complete"),
            tuple(
                Operation.from_json(x)
                for x in _list(d["operations"], "publication.operations")
            ),
        )

    def to_json(self) -> Json:
        return {
            "state": self.state,
            "ledger_revision": self.ledger_revision,
            "ledger_complete": self.ledger_complete,
            "operations": [x.to_json() for x in self.operations],
        }


REPORT_KEYS = frozenset(
    {
        "schema_version",
        "provider",
        "capability_profile",
        "lease_identity",
        "boundary_identity",
        "evidence_ref",
        "journal_revision",
        "controller_id",
        "admission",
        "local_stop",
        "workspace_recovery",
        "publication",
        "refusal_reasons",
        "observed_at",
    }
)
# Top-level keys of a v1 ProcessEvidence record (leases.py).
V1_KEYS = frozenset(
    {"owner", "provider", "wrapper", "groups", "descendants", "recorded_at"}
)


class LegacyEvidence(ValueError):
    """A v1 record where V2 is required. Reason: `legacy_v1_evidence`."""


@dataclass(frozen=True)
class BoundaryStopReportV2:
    schema_version: int
    provider: str
    capability_profile: str
    lease_identity: LeaseIdentity
    boundary_identity: BoundaryIdentity
    evidence_ref: EvidenceRef
    journal_revision: int
    controller_id: str
    admission: Admission
    local_stop: LocalStop
    workspace_recovery: WorkspaceRecovery
    publication: Publication
    refusal_reasons: tuple[str, ...]
    observed_at: int  # milliseconds; diagnostic only, never grants ownership

    @classmethod
    def from_json(cls, data: Any) -> BoundaryStopReportV2:
        if isinstance(data, dict) and (
            set(data) == V1_KEYS or data.get("schema_version") == 1
        ):
            raise LegacyEvidence(LEGACY_V1)
        d = _obj(data, "stop report", set(REPORT_KEYS))
        version = d["schema_version"]
        if type(version) is not int or version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {version!r}")
        reasons = tuple(
            _str(r, "refusal_reasons[]")
            for r in _list(d["refusal_reasons"], "refusal_reasons")
        )
        return cls(
            version,
            _str(d["provider"], "provider"),
            _str(d["capability_profile"], "capability_profile"),
            LeaseIdentity.from_json(d["lease_identity"]),
            BoundaryIdentity.from_json(d["boundary_identity"]),
            EvidenceRef.from_json(d["evidence_ref"]),
            _int(d["journal_revision"], "journal_revision", 1),
            _str(d["controller_id"], "controller_id"),
            Admission.from_json(d["admission"]),
            LocalStop.from_json(d["local_stop"]),
            WorkspaceRecovery.from_json(d["workspace_recovery"]),
            Publication.from_json(d["publication"]),
            reasons,
            _int(d["observed_at"], "observed_at"),
        )

    def to_json(self) -> Json:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "capability_profile": self.capability_profile,
            "lease_identity": self.lease_identity.to_json(),
            "boundary_identity": self.boundary_identity.to_json(),
            "evidence_ref": self.evidence_ref.to_json(),
            "journal_revision": self.journal_revision,
            "controller_id": self.controller_id,
            "admission": self.admission.to_json(),
            "local_stop": self.local_stop.to_json(),
            "workspace_recovery": self.workspace_recovery.to_json(),
            "publication": self.publication.to_json(),
            "refusal_reasons": list(self.refusal_reasons),
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class Compatibility:
    schema_version: int
    provider: str
    capability_profile: str
    controller_id: str

    @classmethod
    def from_json(cls, data: Any) -> Compatibility:
        keys = {"schema_version", "provider", "capability_profile", "controller_id"}
        d = _obj(data, "compatibility", keys)
        version = d["schema_version"]
        if type(version) is not int:
            raise ValueError("compatibility.schema_version must be an integer")
        return cls(
            version,
            _str(d["provider"], "compatibility.provider"),
            _str(d["capability_profile"], "compatibility.capability_profile"),
            _str(d["controller_id"], "compatibility.controller_id"),
        )


# --- V2 provider interface (method names are agreed in the Codex review) -------


class BoundaryProviderV2(Protocol):
    """Reads only protected controller journals. Records cross this interface
    as JSON objects; the consumer decodes them strictly. Any exception counts
    as "not proved"."""

    name: str
    live: bool

    def compatibility(self) -> Json:
        """schema_version, provider, capability_profile, controller_id."""
        ...

    def binding(self, lease: LeaseIdentity) -> Json | None:
        """The immutable binding of this generation, or None when unbound."""
        ...

    def preparation(self, lease: LeaseIdentity) -> str:
        """ "none" only when the complete journal shows no preparation intent,
        no binding and no admitted execution or publication."""
        ...

    def close_admission(self, lease: LeaseIdentity, binding: BindingRef) -> None:
        """Durably close execution and publication admission (supersession)."""
        ...

    def retire(self, lease: LeaseIdentity, binding: BindingRef) -> Json:
        """Phase A. Idempotent: an already retired binding returns its stored
        proof."""
        ...

    def proof(self, lease: LeaseIdentity, binding: BindingRef) -> Json | None:
        """The stored proof, or None. Phase B revalidation and release."""
        ...

    def inventory(self) -> Json:
        """Every unretired boundary and open preparation intent. Raises when
        it cannot prove the list is complete."""
        ...


# --- Preflight -----------------------------------------------------------------


def preflight(
    provider: BoundaryProviderV2, accepted: Mapping[str, bool] = ACCEPTED_PROFILES
) -> Compatibility:
    """Run before preparation or binding and again before a proof permits
    release or takeover. Raises NotProved unless the provider is live, speaks
    schema 2 and its profile is accepted. `live` alone is never approval."""
    try:
        live = provider.live
        name = provider.name
        compat = Compatibility.from_json(provider.compatibility())
    except Exception as err:  # any provider failure: not proved
        raise NotProved([f"provider_error:{type(err).__name__}"]) from None
    reasons = []
    if live is not True:
        reasons.append("provider_not_live")
    if compat.schema_version != SCHEMA_VERSION:
        reasons.append(f"unsupported_schema:{compat.schema_version}")
    if compat.provider != name:
        reasons.append("provider_name_mismatch")
    if compat.capability_profile not in accepted:
        reasons.append(f"profile_not_accepted:{compat.capability_profile}")
    if reasons:
        raise NotProved(reasons)
    return compat


def preparation_is_none(provider: BoundaryProviderV2, lease: LeaseIdentity) -> bool:
    """True only for an exact "none"; anything else or an error is False."""
    try:
        return provider.preparation(lease) == "none"
    except Exception:
        return False


# --- Retirement check -------------------------------------------------------------


def retirement_blockers(
    report: Any,
    lease: LeaseIdentity,
    binding: BindingRef,
    accepted: Mapping[str, bool] = ACCEPTED_PROFILES,
    controller_id: str | None = None,
) -> list[str]:
    """Why `report` does not prove this generation retired; empty when it
    does. `controller_id`, when given, must match the report's authority."""
    if not isinstance(report, BoundaryStopReportV2):
        from leases import ProcessEvidence, StopReport

        if isinstance(report, (ProcessEvidence, StopReport)):
            return [LEGACY_V1]
        return [f"not_v2_report:{type(report).__name__}"]

    problems = []
    if report.schema_version != SCHEMA_VERSION:
        problems.append(f"unsupported_schema:{report.schema_version}")
    offline_ok = accepted.get(report.capability_profile)
    if offline_ok is None:
        problems.append(f"profile_not_accepted:{report.capability_profile}")
    if controller_id is not None and report.controller_id != controller_id:
        problems.append("controller_mismatch")

    held, seen = lease.to_json(), report.lease_identity.to_json()
    problems.extend(f"lease_mismatch:{k}" for k in held if held[k] != seen[k])
    ident = report.boundary_identity
    if ident.boundary_id != binding.boundary_id:
        problems.append("binding_mismatch:boundary_id")
    if ident.binding_digest != binding.binding_digest:
        problems.append("binding_mismatch:binding_digest")

    adm = report.admission
    for kind, state in (("execution", adm.execution), ("publication", adm.publication)):
        if state != "closed":
            problems.append(f"admission_{kind}:{state}")
    problems.extend(
        f"in_flight_unresolved:{op.operation_id}"
        for op in adm.in_flight
        if not op.resolved
    )

    stop = report.local_stop
    if stop.state != "stopped":
        problems.append(f"local_stop:{stop.state}")
    if not stop.container_terminal:
        problems.append("container_not_terminal")
    if stop.parent_populated is not False:
        problems.append(
            "parent_unreadable" if stop.parent_populated is None else "parent_populated"
        )

    rec = report.workspace_recovery
    if rec.state != "recovered":
        problems.append(f"workspace_recovery:{rec.state}")
    for name in (
        "artifact_id",
        "artifact_digest",
        "manifest_digest",
        "recovery_record",
    ):
        if rec.state == "recovered" and getattr(rec, name) is None:
            problems.append(f"recovery_missing:{name}")

    pub = report.publication
    if pub.state == "unknown":
        problems.append("publication:unknown")
    elif pub.state == "disabled_offline":
        if offline_ok is False:  # None is already profile_not_accepted
            problems.append("disabled_offline_not_allowed")
        if pub.operations:
            problems.append("disabled_offline_with_operations")
    else:  # reconciled
        if pub.ledger_revision is None or not pub.ledger_complete:
            problems.append("publication_ledger_incomplete")
        problems.extend(
            f"publication_unresolved:{op.operation_id}"
            for op in pub.operations
            if op.result == "unknown"
        )

    problems.extend(f"provider_refusal:{r}" for r in report.refusal_reasons)
    return problems
