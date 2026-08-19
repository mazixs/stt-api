#!/usr/bin/env python3
"""Test double for the `gigastt` binary.

Implements just enough of the upstream CLI contract for the console's tests:

    fake_gigastt.py download --model-dir D --model-variant V --progress json
    fake_gigastt.py serve --host H --port P --model-variant V ...

Behaviour is steered by environment variables:

    FAKE_DOWNLOAD_FAIL=network|checksum|disk   download fails with the matching exit code
    FAKE_DOWNLOAD_FAIL_TIMES=<n>               first n download attempts fail, then it works
    FAKE_DOWNLOAD_SLOW=<seconds>               delay between progress events
    FAKE_STARTUP_DELAY=<seconds>               serve waits before binding the port
    FAKE_UNHEALTHY=1                           /health keeps reporting model "loading"
    FAKE_UNHEALTHY_VARIANT=<head>              only that head never becomes ready
    FAKE_CRASH_AFTER=<seconds>                 serve dies with exit code 1
    FAKE_TRANSCRIPT=<text>                     text returned by transcription endpoints
"""

import argparse
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HEAD_FILES = {
    "rnnt": (
        "v3_rnnt_encoder_int8.onnx",
        "v3_rnnt_decoder.onnx",
        "v3_rnnt_joint.onnx",
        "v3_vocab.txt",
    ),
    "e2e_rnnt": (
        "v3_e2e_rnnt_encoder_int8.onnx",
        "v3_e2e_rnnt_decoder.onnx",
        "v3_e2e_rnnt_joint.onnx",
        "v3_e2e_rnnt_vocab.txt",
    ),
    "ml_ctc": ("multilingual_ctc.int8.onnx", "multilingual_vocab.txt"),
    "ml_ctc_large": ("multilingual_large_ctc.int8.onnx", "multilingual_vocab.txt"),
}

EXIT_CODES = {"checksum": 65, "network": 69, "disk": 74}


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def cmd_download(args: argparse.Namespace) -> int:
    variant = args.model_variant or "rnnt"
    files = HEAD_FILES[variant]
    delay = float(os.environ.get("FAKE_DOWNLOAD_SLOW", "0"))
    failure = os.environ.get("FAKE_DOWNLOAD_FAIL")

    fail_times = int(os.environ.get("FAKE_DOWNLOAD_FAIL_TIMES", "0"))
    if fail_times:
        os.makedirs(args.model_dir, exist_ok=True)
        counter = os.path.join(args.model_dir, ".fake_attempts")
        attempts = int(open(counter).read()) if os.path.exists(counter) else 0
        attempts += 1
        with open(counter, "w") as fh:
            fh.write(str(attempts))
        if attempts <= fail_times:
            failure = failure or "network"
        else:
            failure = None

    os.makedirs(args.model_dir, exist_ok=True)
    total = 1000
    for name in files:
        for done in (total // 2, total):
            emit({"phase": "download", "file": name, "bytes_done": done, "bytes_total": total})
            if delay:
                time.sleep(delay)
        if failure:
            emit({"phase": "error", "kind": failure, "message": f"fake {failure} failure"})
            return EXIT_CODES.get(failure, 1)
        with open(os.path.join(args.model_dir, name), "wb") as fh:
            fh.write(b"fake-weights")
        emit({"phase": "verify", "file": name})
    emit({"phase": "quantize", "file": files[0]})
    emit({"phase": "done", "model_dir": args.model_dir})
    return 0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    variant = "rnnt"
    argv: list[str] = []
    reloads = 0
    last_upload: dict = {}

    def log_message(self, fmt, *args):  # keep test output quiet
        sys.stderr.write("fake-engine " + (fmt % args) + "\n")

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode(), "application/json")

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            loading = os.environ.get("FAKE_UNHEALTHY") == "1" or (
                os.environ.get("FAKE_UNHEALTHY_VARIANT") == Handler.variant
            )
            self._json(
                200,
                {
                    "status": "ok",
                    "model": "loading" if loading else f"gigaam-v3-{Handler.variant}",
                    "variant": Handler.variant,
                    "punctuation": True,
                    "itn": True,
                },
            )
        elif self.path == "/debug/reloads":
            self._json(200, {"count": Handler.reloads})
        elif self.path == "/debug/argv":
            self._json(200, {"argv": Handler.argv})
        elif self.path == "/debug/last_upload":
            self._json(200, Handler.last_upload)
        else:
            self._json(404, {"error": {"message": "not found", "code": "not_found"}})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.path == "/v1/admin/reload" or self.path.startswith("/v1/admin/reload?"):
            Handler.reloads += 1
            self._json(200, {"status": "ok"})
            return
        if self.path.startswith("/v1/audio/transcriptions") or self.path.startswith(
            "/v1/transcribe"
        ):
            self._transcribe(body)
            return
        self._json(404, {"error": {"message": "not found", "code": "not_found"}})

    def _form_field(self, body: bytes, name: str) -> str | None:
        match = re.search(
            rb'name="' + re.escape(name.encode()) + rb'"\r\n\r\n(.*?)\r\n--',
            body,
            re.DOTALL,
        )
        return match.group(1).decode("utf-8", "ignore").strip() if match else None

    def _remember_upload(self, body: bytes) -> None:
        """What arrived in the `file` part, so tests can check it was not mangled."""
        match = re.search(
            rb'name="file"; filename="([^"]*)"\r\nContent-Type: ([^\r\n]+)\r\n\r\n(.*?)\r\n--',
            body,
            re.DOTALL,
        )
        Handler.last_upload = (
            {
                "filename": match.group(1).decode("utf-8", "ignore"),
                "content_type": match.group(2).decode("utf-8", "ignore"),
                "magic": match.group(3)[:4].decode("latin-1"),
                "size": len(match.group(3)),
            }
            if match
            else {}
        )

    def _transcribe(self, body: bytes) -> None:
        self._remember_upload(body)
        text = os.environ.get("FAKE_TRANSCRIPT", "привет мир")
        fmt = self._form_field(body, "response_format") or "json"
        stream = (self._form_field(body, "stream") or "").lower() == "true"

        if stream:
            if fmt not in ("json", "text"):
                self._json(
                    400,
                    {"error": {"message": "invalid stream options", "code": "invalid_stream_options"}},
                )
                return
            words = text.split()
            chunks = [f'data: {{"type":"transcript.text.delta","delta":"{w}"}}\n\n' for w in words]
            chunks.append(
                "data: "
                + json.dumps({"type": "transcript.text.done", "text": text}, ensure_ascii=False)
                + "\n\n"
            )
            chunks.append("data: [DONE]\n\n")
            payload = "".join(chunks).encode()
            self._send(200, payload, "text/event-stream")
            return

        if fmt == "text":
            self._send(200, text.encode(), "text/plain; charset=utf-8")
        elif fmt == "srt":
            self._send(200, f"1\n00:00:00,000 --> 00:00:02,000\n{text}\n".encode(), "text/plain")
        elif fmt == "vtt":
            self._send(
                200, f"WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n{text}\n".encode(), "text/plain"
            )
        elif fmt == "verbose_json":
            self._json(
                200,
                {
                    "task": "transcribe",
                    "language": "ru",
                    "duration": 2.0,
                    "text": text,
                    "segments": [{"id": 0, "start": 0.0, "end": 2.0, "text": text}],
                },
            )
        elif fmt == "json":
            self._json(200, {"text": text})
        else:
            self._json(
                400,
                {"error": {"message": "invalid response format", "code": "invalid_response_format"}},
            )


def cmd_serve(args: argparse.Namespace, argv: list[str]) -> int:
    Handler.variant = args.model_variant or "rnnt"
    Handler.argv = argv

    # The crash timer starts with the process, so FAKE_CRASH_AFTER can fire before,
    # during, or after FAKE_STARTUP_DELAY depending on the values under test.
    crash_after = os.environ.get("FAKE_CRASH_AFTER")
    if crash_after:

        def crash():
            time.sleep(float(crash_after))
            os._exit(1)

        threading.Thread(target=crash, daemon=True).start()

    delay = float(os.environ.get("FAKE_STARTUP_DELAY", "0"))
    if delay:
        time.sleep(delay)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    sys.stderr.write(f"fake-engine listening on {args.host}:{args.port}\n")
    sys.stderr.flush()
    server.serve_forever()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    download = sub.add_parser("download")
    download.add_argument("--model-dir", default=".")
    download.add_argument("--model-variant", default="rnnt")
    download.add_argument("--progress", default="human")

    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=9876)
    serve.add_argument("--model-dir", default=".")
    serve.add_argument("--model-variant", default="rnnt")

    args, _unknown = parser.parse_known_args()
    if args.command == "download":
        return cmd_download(args)
    return cmd_serve(args, sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
