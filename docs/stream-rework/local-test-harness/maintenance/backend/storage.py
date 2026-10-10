"""Local S3 boundary and Icecast sink. Never use with real credentials."""

from __future__ import annotations

import hashlib
import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path("/work")
OBJECTS = ROOT / "objects"
OBJECTS.mkdir(exist_ok=True)


class Storage(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        pass

    def reply(self, status: int, body: bytes = b"", **headers: str) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key.replace("_", "-"), value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def handle_object(self) -> None:
        url = urlsplit(self.path)
        query = parse_qs(url.query, keep_blank_values=True)
        key = hashlib.sha256(url.path.encode()).hexdigest()
        target = OBJECTS / key
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        with (ROOT / "storage.jsonl").open("a") as log:
            log.write(
                json.dumps(
                    {
                        "method": self.command,
                        "path": url.path,
                        "query": url.query,
                        "bytes": len(body),
                    }
                )
                + "\n"
            )
        if "lifecycle" in query:
            self.reply(200)
        elif self.command == "POST" and "uploads" in query:
            if (ROOT / "hold").exists():
                (ROOT / "pending").write_text(url.path)
                deadline = time.monotonic() + 90
                while (ROOT / "hold").exists():
                    if time.monotonic() > deadline:
                        self.reply(503)
                        return
                    time.sleep(0.05)
            self.reply(
                200,
                b"<InitiateMultipartUploadResult><UploadId>fixture-upload</UploadId></InitiateMultipartUploadResult>",
                Content_Type="application/xml",
            )
        elif self.command == "POST" and "uploadId" in query:
            target.write_bytes(target.with_suffix(".part").read_bytes())
            self.reply(
                200,
                b'<CompleteMultipartUploadResult><ETag>"fixture"</ETag></CompleteMultipartUploadResult>',
                Content_Type="application/xml",
            )
        elif self.command == "PUT":
            if "uploadId" in query:
                if query.get("partNumber") != ["1"]:
                    self.reply(501, b"Fixture supports a single small upload part")
                    return
                target.with_suffix(".part").write_bytes(body)
            else:
                target.write_bytes(body)
            target.with_suffix(".path").write_text(url.path)
            self.reply(200, ETag='"fixture"')
        elif self.command in {"GET", "HEAD"}:
            if target.exists():
                self.reply(200, target.read_bytes(), ETag='"fixture"')
            else:
                self.reply(404)
        else:
            self.reply(501)

    do_PUT = handle_object
    do_POST = handle_object
    do_GET = handle_object
    do_HEAD = handle_object


class Sink(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        data = b'{"icestats":{"source":[]}}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_PUT(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.flush()
        size = 0
        while chunk := self.rfile.read(4096):
            size += len(chunk)
        with (ROOT / "sink.jsonl").open("a") as log:
            log.write(json.dumps({"path": self.path, "bytes": size}) + "\n")

    do_SOURCE = do_PUT


if __name__ == "__main__":
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ROOT / "cert.pem", ROOT / "key.pem")
    storage = ThreadingHTTPServer(("127.0.0.1", 443), Storage)
    storage.socket = context.wrap_socket(storage.socket, server_side=True)
    sink = ThreadingHTTPServer(("127.0.0.1", 8010), Sink)
    threading.Thread(target=sink.serve_forever, daemon=True).start()
    (ROOT / "storage-ready").touch()
    storage.serve_forever()
