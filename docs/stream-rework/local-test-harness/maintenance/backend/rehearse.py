"""Rehearse the deployed backend with isolated, synthetic data only."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

IMAGE = "ghcr.io/phaabe/live.moafunk.de-backend@sha256:77c304243b6b7dd048574f6abfea40d9fc053b2a6f9b9aa771bfdedcb4e080fe"
PYTHON = "python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c"
FIXTURE = Path(__file__).resolve().parent
WORK = Path(tempfile.mkdtemp(prefix="moafunk-backend-rehearsal-"))
NAME = WORK.name
API = NAME + "-api"
STORAGE = NAME + "-storage"
EVENTS: list[dict[str, object]] = []
CREATED: list[str] = []


def run(*args: str) -> str:
    result = subprocess.run(
        args, check=True, text=True, capture_output=True, timeout=120
    )
    return result.stdout.strip()


def docker(*args: str) -> str:
    return run("docker", "--context", "desktop-linux", *args)


def event(name: str, **details: object) -> None:
    row = {"utc": datetime.now(timezone.utc).isoformat(), "event": name, **details}
    EVENTS.append(row)
    (WORK / "events.json").write_text(json.dumps(EVENTS, indent=2) + "\n")
    print(json.dumps(row), flush=True)


def api(method: str, path: str, body: str = "") -> dict[str, object]:
    return json.loads(
        docker("exec", STORAGE, "python", "/fixture/client.py", method, path, body)
    )


def wait_health() -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            api("GET", "/health")
            return
        except subprocess.CalledProcessError:
            time.sleep(0.5)
    raise RuntimeError("Backend health did not become ready")


def sql(statement: str, values: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    with sqlite3.connect(WORK / "data/unheard.db") as connection:
        return connection.execute(statement, values).fetchall()


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def launch() -> None:
    uid, gid = str(os.getuid()), str(os.getgid())
    docker(
        "run",
        "-d",
        "--name",
        STORAGE,
        "--network",
        "none",
        "--read-only",
        "--user",
        f"{uid}:{gid}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--sysctl",
        "net.ipv4.ip_unprivileged_port_start=0",
        "--mount",
        f"type=bind,src={WORK},dst=/work",
        "--mount",
        f"type=bind,src={FIXTURE},dst=/fixture,readonly",
        PYTHON,
        "python",
        "/fixture/storage.py",
    )
    CREATED.append(STORAGE)
    deadline = time.monotonic() + 10
    while not (WORK / "storage-ready").exists():
        check(time.monotonic() < deadline, "Fixture storage did not start")
        time.sleep(0.1)
    launch_api()


def launch_api() -> None:
    uid, gid = str(os.getuid()), str(os.getgid())
    args = [
        "run",
        "-d",
        "--name",
        API,
        "--platform",
        "linux/amd64",
        "--network",
        f"container:{STORAGE}",
        "--read-only",
        "--user",
        f"{uid}:{gid}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=256m",
        "--mount",
        f"type=bind,src={WORK / 'data'},dst=/app/data",
        "--mount",
        f"type=bind,src={WORK},dst=/work",
        "--mount",
        f"type=bind,src={WORK / 'hosts'},dst=/etc/hosts,readonly",
        "--mount",
        f"type=bind,src={WORK / 'ca.pem'},dst=/etc/ssl/certs/ca-certificates.crt,readonly",
    ]
    env = {
        "SECRET_KEY": "fixture-only-not-a-secret",
        "SUPERADMIN_PASSWORD_HASH": "fixture-no-password-login",
        "DATABASE_URL": "sqlite:/app/data/unheard.db?mode=rwc",
        "APP_ENV": "production",
        "R2_ACCOUNT_ID": "fixture",
        "R2_ACCESS_KEY_ID": "fixture",
        "R2_SECRET_ACCESS_KEY": "fixture",
        "R2_BUCKET_NAME": "fixture-raw",
        "R2_SHOWS_BUCKET_NAME": "fixture-archive",
        "STREAM_OUTPUT": "icecast",
        "ICECAST_URL": "icecast://source:fixture@127.0.0.1:8010/live",
        "ICECAST_TEST_URL": "icecast://source:fixture@127.0.0.1:8010/test",
        "TELEGRAM_ENABLED": "false",
        "SOUNDCLOUD_AUTO_UPLOAD": "false",
        "RUST_LOG": "info",
    }
    for key, value in env.items():
        args.extend(["-e", f"{key}={value}"])
    docker(*args, IMAGE)
    CREATED.append(API)
    wait_health()
    check(
        "Ensured multipart-abort lifecycle rule" in docker("logs", API),
        "Backend did not reach fixture storage with verified TLS",
    )
    event(
        "started",
        image=IMAGE,
        source="7093fe53488c21030c725f11370e96186a027836",
        network="none; shared namespace; no published ports",
        app_env="production",
        container_id=docker("inspect", API, "--format", "{{.Id}}"),
    )


def remove_api(label: str) -> None:
    (WORK / f"api-{label}.log").write_text(docker("logs", API))
    docker("rm", API)
    CREATED.remove(API)


def idle() -> dict[str, object]:
    recording = api("GET", "/api/recording/status")
    stream = api("GET", "/api/stream/status")
    check(recording["active"] is False, "Recording remains active")
    check(stream["active"] is False, "Producer remains active")
    check(stream["recording"] is False, "Capture remains active")
    return {"recording": recording, "stream": stream}


def manifest(directory: Path) -> dict[str, str]:
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def rehearsal() -> None:
    fixture_hashes = manifest(FIXTURE)
    event("fixture_frozen", fixture_hashes=fixture_hashes)
    (WORK / "data").mkdir()
    (WORK / "hosts").write_text(
        "127.0.0.1 localhost fixture.r2.cloudflarestorage.com\n"
    )
    run(
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(WORK / "ca-key.pem"),
        "-out",
        str(WORK / "ca.pem"),
        "-days",
        "1",
        "-subj",
        "/CN=Disposable maintenance fixture CA",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
    )
    run(
        "openssl",
        "req",
        "-new",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(WORK / "key.pem"),
        "-out",
        str(WORK / "server.csr"),
        "-subj",
        "/CN=fixture.r2.cloudflarestorage.com",
    )
    (WORK / "server.ext").write_text(
        "subjectAltName=DNS:fixture.r2.cloudflarestorage.com\nbasicConstraints=critical,CA:FALSE\nextendedKeyUsage=serverAuth\n"
    )
    run(
        "openssl",
        "x509",
        "-req",
        "-in",
        str(WORK / "server.csr"),
        "-CA",
        str(WORK / "ca.pem"),
        "-CAkey",
        str(WORK / "ca-key.pem"),
        "-CAcreateserial",
        "-out",
        str(WORK / "cert.pem"),
        "-days",
        "1",
        "-extfile",
        str(WORK / "server.ext"),
    )
    launch()
    sql(
        "INSERT INTO sessions(token,user_id,expires_at) SELECT 'fixture-session',id,datetime('now','+1 day') FROM users LIMIT 1"
    )
    today = datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat()
    sql(
        "INSERT INTO shows(id,title,date,show_type,stream_mode) VALUES (1,'Synthetic maintenance capture',?,'external','live')",
        (today,),
    )
    docker(
        "exec",
        API,
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=65",
        "-c:a",
        "libopus",
        "/work/tone.webm",
    )
    (WORK / "hold").touch()
    docker("exec", STORAGE, "python", "/fixture/client.py", "produce")
    check(
        json.loads((WORK / "capture-active.json").read_text())["active"] is True,
        "Synthetic capture was not active",
    )
    event(
        "producer_disconnected",
        recording=api("GET", "/api/recording/status"),
        stream=api("GET", "/api/stream/status"),
    )
    deadline = time.monotonic() + 60
    while not (WORK / "pending").exists():
        check(time.monotonic() < deadline, "Grace finalization did not reach storage")
        time.sleep(0.25)
    recording = api("GET", "/api/recording/status")
    check(
        recording["active"] is False,
        "Recording did not become idle during pending upload",
    )
    check(
        sql("SELECT count(*) FROM recording_versions")[0][0] == 0,
        "Version persisted before held raw upload",
    )
    image_before = docker("inspect", API, "--format", "{{.State.StartedAt}}")
    event(
        "replacement_withheld",
        reason="manual runbook gate: pending raw upload despite idle status",
        recording=recording,
        pending_object=(WORK / "pending").read_text(),
        container_started_at=image_before,
    )
    raw_artifacts = list((WORK / "data/recordings-temp").glob("recording_*.webm"))
    check(len(raw_artifacts) == 1, "Expected one pending raw artifact")
    raw_bytes = raw_artifacts[0].read_bytes()
    raw_digest = hashlib.sha256(raw_bytes).hexdigest()
    (WORK / "hold").unlink()
    deadline = time.monotonic() + 60
    while not sql("SELECT id FROM recording_versions WHERE archive_key IS NOT NULL"):
        check(time.monotonic() < deadline, "Recording archive did not settle")
        time.sleep(0.25)
    while "Auto-finalized recording for show 1" not in docker("logs", API):
        check(
            time.monotonic() < deadline, "Recording lacks terminal completion evidence"
        )
        time.sleep(0.1)
    check(
        not list((WORK / "data/recordings-temp").glob("recording_*")),
        "Recording artifacts remain unresolved",
    )
    check(
        image_before == docker("inspect", API, "--format", "{{.State.StartedAt}}"),
        "Backend replaced during pending finalization",
    )
    rows = sql(
        "SELECT show_id,version,status,raw_key,archive_key,duration_ms FROM recording_versions"
    )
    check(rows[0][2] == "raw" and rows[0][5] >= 60000, "Capture is incomplete")
    raw_path = f"/fixture-raw/{rows[0][3]}"
    archived_path = f"/fixture-archive/{rows[0][4]}"
    raw_object = WORK / "objects" / hashlib.sha256(raw_path.encode()).hexdigest()
    archive_object = (
        WORK / "objects" / hashlib.sha256(archived_path.encode()).hexdigest()
    )
    marker_key = sql("SELECT markers_key FROM recording_versions")[0][0]
    marker_object = (
        WORK
        / "objects"
        / hashlib.sha256(f"/fixture-raw/{marker_key}".encode()).hexdigest()
    )
    check(
        json.loads(marker_object.read_text()) == [],
        "Persisted markers differ from capture",
    )
    check(
        raw_object.read_bytes() == raw_bytes,
        "Stored raw bytes differ from pending artifact",
    )
    archive_duration = float(
        docker(
            "exec",
            API,
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            f"/work/objects/{archive_object.name}",
        )
    )
    check(archive_duration >= 60, "Archived MP3 is incomplete")
    event(
        "finalization_complete",
        versions=rows,
        raw_bytes=len(raw_bytes),
        raw_sha256=raw_digest,
        archive_bytes=archive_object.stat().st_size,
        archive_sha256=hashlib.sha256(archive_object.read_bytes()).hexdigest(),
        archive_duration_seconds=archive_duration,
    )
    selected = sql(
        "SELECT id FROM shows WHERE stream_mode='prerecorded' AND prerecorded_confirmed_at IS NOT NULL AND prerecorded_started_at IS NULL AND start_time IS NOT NULL AND date >= date('now','-2 days')"
    )
    check(not selected, "Bounded path has scheduler candidates")
    event(
        "bounded_path_drained",
        eligible_count=0,
        **idle(),
    )
    docker("stop", "--time", "15", API)
    remove_api("original")
    (WORK / "data/persisted-fixture.txt").write_text("retain across rollback\n")
    shutil.copytree(WORK / "data", WORK / "snapshot")
    with (
        sqlite3.connect(WORK / "snapshot/unheard.db") as source,
        sqlite3.connect(WORK / "snapshot.sqlite") as backup,
    ):
        source.backup(backup)
        check(
            backup.execute("PRAGMA integrity_check").fetchone() == ("ok",),
            "Snapshot integrity failed",
        )
    snapshot_manifest = manifest(WORK / "snapshot")
    (WORK / "snapshot-manifest.json").write_text(
        json.dumps(snapshot_manifest, indent=2) + "\n"
    )
    sql("CREATE TABLE failed_candidate_fixture(id INTEGER)")
    sql("UPDATE shows SET title='candidate change' WHERE id=1")
    launch_api()
    event(
        "candidate_started",
        candidate="same exact binary; simulated data/schema change, not a new release",
    )
    docker("stop", "--time", "15", API)
    remove_api("failed-candidate")
    shutil.move(WORK / "data", WORK / "failed-candidate")
    shutil.copytree(WORK / "snapshot", WORK / "data")
    check(
        manifest(WORK / "data") == snapshot_manifest,
        "Restored filesystem differs from snapshot",
    )
    check(
        sql("SELECT title FROM shows WHERE id=1")[0][0]
        == "Synthetic maintenance capture",
        "Restore lost original show",
    )
    check(
        not sql("SELECT name FROM sqlite_master WHERE name='failed_candidate_fixture'"),
        "Candidate schema survived rollback",
    )
    check(sql("PRAGMA integrity_check")[0][0] == "ok", "Restored DB integrity failed")
    launch_api()
    event(
        "bounded_path_rollback_complete",
        image_id=docker("inspect", API, "--format", "{{.Image}}"),
        versions=sql(
            "SELECT show_id,version,status,archive_key FROM recording_versions"
        ),
        integrity="ok",
        failed_candidate_retained=True,
        restored_manifest=snapshot_manifest,
        **idle(),
    )
    # A real clock boundary: seed while stopped, then restart before its due time.
    docker("stop", "--time", "15", API)
    due = (datetime.now(ZoneInfo("Europe/Berlin")) + timedelta(minutes=2)).replace(
        second=0, microsecond=0
    )
    sql(
        "INSERT INTO shows(id,title,date,start_time,end_time,stream_mode,prerecorded_confirmed_at,host_user_id) SELECT 2,'Fixture due during outage',?,?,?,'prerecorded',datetime('now'),id FROM users LIMIT 1",
        (
            due.date().isoformat(),
            due.strftime("%H:%M"),
            (due + timedelta(hours=1)).strftime("%H:%M"),
        ),
    )
    docker("start", API)
    wait_health()
    check(
        datetime.now(ZoneInfo("Europe/Berlin")) < due,
        "Missed safe pre-due shutdown interval",
    )
    event(
        "outage_path_short_boundary",
        due_utc=due.astimezone(timezone.utc).isoformat(),
        **idle(),
    )
    docker("stop", "--time", "15", API)
    check(
        datetime.now(ZoneInfo("Europe/Berlin")) < due,
        "API stop completed after the safe interval",
    )
    event("outage_path_api_stopped", due_utc=due.astimezone(timezone.utc).isoformat())
    while datetime.now(ZoneInfo("Europe/Berlin")) < due + timedelta(seconds=2):
        time.sleep(0.5)
    check(
        docker("inspect", API, "--format", "{{.State.Running}}") == "false",
        "API revived during outage",
    )
    check(
        sql("SELECT prerecorded_started_at FROM shows WHERE id=2")[0][0] is None,
        "Show started during stopped API",
    )
    event(
        "restart_withheld",
        reason="manual restart check found a due catch-up candidate",
        candidate_id=2,
        api_running=False,
    )
    # Deliberate diagnostic restart on the disposable fixture only. No media is
    # attached: the real scheduler logs its attempt and refuses missing audio.
    restart_utc = datetime.now(timezone.utc).isoformat()
    docker("start", API)
    wait_health()
    deadline = time.monotonic() + 40
    while "Prerecorded auto-start: starting show 2" not in docker(
        "logs", "--since", restart_utc, API
    ):
        check(time.monotonic() < deadline, "Startup did not attempt the due candidate")
        time.sleep(0.25)
    event(
        "diagnostic_restart_catchup_observed",
        candidate_id=2,
        note="isolated negative test; missing audio prevents completed playback; production procedure would keep API stopped",
    )
    docker("stop", "--time", "15", API)
    remove_api("outage-candidate")
    shutil.move(WORK / "data", WORK / "outage-candidate")
    shutil.copytree(WORK / "snapshot", WORK / "data")
    check(
        manifest(WORK / "data") == snapshot_manifest,
        "Outage restore differs from snapshot",
    )
    launch_api()
    check(
        not sql("SELECT id FROM shows WHERE id=2"),
        "Snapshot restore retained due candidate",
    )
    check(
        sql("PRAGMA integrity_check")[0][0] == "ok", "Outage restore integrity failed"
    )
    event(
        "outage_path_restored",
        integrity=sql("PRAGMA integrity_check")[0][0],
        candidate_count=0,
        note="Synthetic schedule restored only; no authority to delete a real scheduled show",
        **idle(),
    )
    # Prove why the pre-restart backup is mandatory for this exact legacy image.
    docker("stop", "--time", "15", API)
    residual = WORK / "data/recordings-temp/recording_99_fixture.webm"
    residual.parent.mkdir(exist_ok=True)
    shutil.copyfile(WORK / "tone.webm", residual)
    old = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
    os.utime(residual, (old, old))
    shutil.copytree(WORK / "data", WORK / "before-stale-cleanup")
    docker("start", API)
    wait_health()
    deadline = time.monotonic() + 10
    while residual.exists():
        check(time.monotonic() < deadline, "Legacy stale-file cleanup was not observed")
        time.sleep(0.1)
    preserved = WORK / "before-stale-cleanup/recordings-temp/recording_99_fixture.webm"
    check(preserved.exists(), "Pre-restart backup lost the residual")
    event(
        "legacy_stale_artifact_deleted_on_boot",
        age_days=2,
        backup_retained=True,
        backup_sha256=hashlib.sha256(preserved.read_bytes()).hexdigest(),
        implication="Do not restart this legacy image on the only copy of unresolved artifacts",
    )
    check(
        manifest(FIXTURE) == fixture_hashes, "Fixture changed during rehearsal; rerun"
    )
    event(
        "complete",
        work=str(WORK),
        fixture_hashes=fixture_hashes,
    )


if __name__ == "__main__":
    print(f"Evidence directory: {WORK}", flush=True)
    try:
        rehearsal()
    finally:
        cleanup_failures: list[Exception] = []
        for container in reversed(CREATED):
            try:
                (WORK / f"{container}.log").write_text(docker("logs", container))
            except (subprocess.SubprocessError, OSError) as error:
                cleanup_failures.append(error)
            try:
                docker("rm", "-f", container)
            except (subprocess.SubprocessError, OSError) as error:
                cleanup_failures.append(error)
        if cleanup_failures:
            raise ExceptionGroup("Fixture cleanup failed", cleanup_failures)
