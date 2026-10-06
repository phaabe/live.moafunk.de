"""Real Codex controls. The only model endpoint is a disposable localhost server."""

from __future__ import annotations

import hashlib
import json
import queue
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

NATIVE = Path(
    "/opt/homebrew/lib/node_modules/@openai/codex/node_modules/"
    "@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex"
)


def native_env(home: Path, codex_home: Path, temporary: Path) -> dict[str, str]:
    """Do not inherit credentials, personal config, proxies or runner state."""
    return {
        "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "TMPDIR": str(temporary),
        "LANG": "en_US.UTF-8",
    }


def identity(env: dict[str, str]) -> dict[str, str]:
    if not NATIVE.is_file():
        raise AssertionError(f"Required native control binary missing: {NATIVE}")
    version = subprocess.run(
        [str(NATIVE), "--version"], env=env, capture_output=True, text=True, check=True
    ).stdout.strip()
    return {
        "path": str(NATIVE),
        "version": version,
        "sha256": hashlib.sha256(NATIVE.read_bytes()).hexdigest(),
    }


def list_hooks(cwd: Path, env: dict[str, str], output: Path) -> list[dict[str, Any]]:
    """Ask native Codex for the exact trust key/hash; no trust bypass."""
    messages: queue.Queue[str] = queue.Queue()
    with output.open("w") as errors:
        process = subprocess.Popen(
            [str(NATIVE), "app-server"],
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
        )
        assert process.stdin is not None and process.stdout is not None

        def receive() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                messages.put(line)

        reader = threading.Thread(target=receive, daemon=True)
        reader.start()
        try:
            for request in (
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {"name": "foundation-controls", "version": "1"},
                        "capabilities": {"experimentalApi": True},
                    },
                },
                {"method": "initialized"},
                {"id": 2, "method": "hooks/list", "params": {"cwds": [str(cwd)]}},
            ):
                process.stdin.write(json.dumps(request) + "\n")
                process.stdin.flush()
            for _ in range(100):
                response = json.loads(messages.get(timeout=15))
                if response.get("id") == 2:
                    entry = response["result"]["data"][0]
                    if entry["errors"]:
                        raise AssertionError(entry["errors"])
                    return entry["hooks"]
            raise AssertionError("hooks/list never replied")
        finally:
            process.terminate()
            process.wait(timeout=10)
            reader.join(timeout=2)
            process.stdin.close()
            process.stdout.close()


class Responses:
    """A deterministic Responses API that returns only the requested local tools."""

    def __init__(self, directory: Path, commands: list[str]) -> None:
        self.directory = directory
        self.commands = commands
        self.requests: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802 (HTTP handler API)
                request = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                owner.requests.append(request)
                number = len(owner.requests) - 1
                (directory / f"request-{number}.json").write_text(json.dumps(request))
                if number < len(commands):
                    item = {
                        "id": f"fc_{number}",
                        "type": "function_call",
                        "call_id": f"call_{number}",
                        "name": "exec_command",
                        "arguments": json.dumps(
                            {
                                "cmd": commands[number],
                                "max_output_tokens": 3000,
                                "yield_time_ms": 10000,
                            }
                        ),
                    }
                else:
                    item = {
                        "id": f"msg_{number}",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Controls complete.",
                                "annotations": [],
                            }
                        ],
                    }
                response = {
                    "id": f"resp_{number}",
                    "object": "response",
                    "status": "completed",
                    "output": [item],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                }
                events = (
                    {
                        "type": "response.created",
                        "response": {**response, "status": "in_progress", "output": []},
                    },
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": item,
                    },
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": item,
                    },
                    {"type": "response.completed", "response": response},
                )
                data = "".join(
                    f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                    for event in events
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> Responses:
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def config(self) -> str:
        return (
            'model = "foundation-fixture"\nmodel_provider = "foundation_fixture"\n'
            '[model_providers.foundation_fixture]\nname = "local control"\n'
            f'base_url = "http://127.0.0.1:{self.server.server_port}/v1"\n'
            'wire_api = "responses"\nrequires_openai_auth = false\n'
        )

    def launch(
        self,
        cwd: Path,
        env: dict[str, str],
        extra_roots: tuple[Path, ...] = (),
        *,
        skip_git_check: bool = True,
        source_profile: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [
                str(NATIVE),
                "exec",
                *(["--skip-git-repo-check"] if skip_git_check else []),
                *([] if source_profile else ["--sandbox", "workspace-write"]),
                "--json",
                "--cd",
                str(cwd),
                *(arg for root in extra_roots for arg in ("--add-dir", str(root))),
                "Run only the local fixture actions.",
            ],
            env=env,
            input="",
            capture_output=True,
            text=True,
            timeout=90,
        )
        (self.directory / "stdout.jsonl").write_text(result.stdout)
        (self.directory / "stderr.txt").write_text(result.stderr)
        return result
