#!/usr/bin/env python3
"""Mock OpenAI-compatible speech-to-text server for the custom build's live emulator test.

The emulator reaches it at http://10.0.2.2:<port>/v1. No real provider, key or audio leaves the runner.

  POST .../audio/transcriptions  -> waits DELAY seconds, then {"text": ONLINE_TEXT}
  POST .../chat/completions      -> echoes the last user message (in case rewording runs)
  GET  .../models                -> one model
  GET  /control?delay=<seconds>  -> sets the delay for the following transcription requests
  GET  /control?mode=reset       -> drops every transcription connection without a reply (network error)
  GET  /requests                 -> JSON log of the transcription requests seen so far

Each request is logged with its outcome: "answered" after the delay, or "client-closed" when the app
cancelled the call (that is what the offline switch must do at its deadline).
"""

import argparse
import json
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ONLINE_TEXT = "Mock online transcript from the test server."

state = {"delay": 0.0, "mode": "normal", "requests": []}
lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: N802 - stdlib name
        print(f"[mock {time.strftime('%H:%M:%S')}] " + fmt % args, flush=True)

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            return self.rfile.read(length)
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            data = b""
            while True:
                size = int(self.rfile.readline().strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    return data
                data += self.rfile.read(size)
                self.rfile.readline()
        return b""

    def _client_closed(self):
        """True when the app has closed (cancelled) the connection while we wait."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if readable:
                return self.connection.recv(1, 2) == b""  # MSG_PEEK; empty read = closed
        except OSError:
            return True
        return False

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/control":
            q = parse_qs(url.query)
            with lock:
                if "delay" in q:
                    state["delay"] = float(q["delay"][0])
                if "mode" in q:
                    state["mode"] = q["mode"][0]
                self._json(200, {"delay": state["delay"], "mode": state["mode"]})
            return
        if url.path == "/requests":
            with lock:
                self._json(200, state["requests"])
            return
        if url.path.endswith("/models"):
            self._json(200, {"object": "list", "data": [{"id": "whisper-1", "object": "model"}]})
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):  # noqa: N802
        url = urlparse(self.path)
        body = self._read_body()
        if url.path.endswith("/audio/transcriptions"):
            with lock:
                delay = state["delay"]
                entry = {"path": url.path, "bytes": len(body), "delay": delay,
                         "received": time.time(), "outcome": "pending"}
                state["requests"].append(entry)
            if state["mode"] == "reset":
                # A broken connection: the request is read, then the socket is dropped without a reply.
                entry["outcome"] = "reset"
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                print("[mock] transcription connection dropped (mode=reset)", flush=True)
                return
            started = time.monotonic()
            while time.monotonic() - started < delay:
                if self._client_closed():
                    entry["outcome"] = "client-closed"
                    entry["afterSeconds"] = round(time.monotonic() - started, 2)
                    print(f"[mock] transcription cancelled by the app after {entry['afterSeconds']} s", flush=True)
                    return
                time.sleep(0.05)
            try:
                self._json(200, {"text": ONLINE_TEXT})
                entry["outcome"] = "answered"
            except OSError:
                entry["outcome"] = "client-closed"
            entry["afterSeconds"] = round(time.monotonic() - started, 2)
            print(f"[mock] transcription {entry['outcome']} after {entry['afterSeconds']} s", flush=True)
            return
        if url.path.endswith("/chat/completions"):
            try:
                msgs = json.loads(body or b"{}").get("messages", [])
                content = next((m.get("content") for m in reversed(msgs) if m.get("role") == "user"), "")
                if isinstance(content, list):
                    content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            except ValueError:
                content = ""
            self._json(200, {"id": "mock", "object": "chat.completion", "choices": [
                {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}]})
            return
        self._json(404, {"error": {"message": "not found"}})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    server.daemon_threads = True
    print(f"[mock] listening on :{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
