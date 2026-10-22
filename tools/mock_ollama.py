#!/usr/bin/env python3
"""Fake Ollama API for UI smoke tests with multi-model support. Not used by the ISO."""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("MOCK_PORT", "11435"))

MODELS: dict[str, int] = {
    "phi3:mini": 2_300_000_000,
    "qwen2.5:3b": 1_900_000_000,
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/api/tags":
            models_list = [
                {"name": name, "model": name, "size": size}
                for name, size in MODELS.items()
            ]
            body = json.dumps({"models": models_list}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/version":
            body = b'{"version":"0.0-mock"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_DELETE(self):
        if self.path == "/api/delete":
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            target = payload.get("name") or payload.get("model") or ""
            if target in MODELS:
                del MODELS[target]
            elif ":" not in target:
                for k in list(MODELS.keys()):
                    if k.startswith(target + ":"):
                        del MODELS[k]
                        break
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length)

        if self.path == "/api/pull":
            payload = json.loads(raw_body.decode("utf-8") or "{}")
            name = payload.get("name", "unknown")
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            total_bytes = 2_100_000_000
            for pct in range(0, 101, 20):
                completed = int(total_bytes * (pct / 100))
                chunk = (
                    json.dumps(
                        {
                            "status": "downloading",
                            "digest": "sha256:mock",
                            "total": total_bytes,
                            "completed": completed,
                        }
                    )
                    + "\n"
                ).encode()
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
                time.sleep(0.05)

            MODELS[name] = total_bytes
            success_chunk = (json.dumps({"status": "success"}) + "\n").encode()
            self.wfile.write(f"{len(success_chunk):x}\r\n".encode() + success_chunk + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return

        if self.path != "/api/chat":
            self.send_error(404)
            return

        payload = json.loads(raw_body.decode("utf-8") or "{}")
        model = payload.get("model", "phi3:mini")

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        answer = (
            f"xOS berjalan lokal menggunakan model [{model}]. Ini adalah respons "
            f"streaming dari server mock: model dan antarmuka kiosk terhubung."
        )
        for word in answer.split(" "):
            chunk = (
                json.dumps(
                    {
                        "model": model,
                        "message": {"role": "assistant", "content": word + " "},
                        "done": False,
                    }
                )
                + "\n"
            ).encode()
            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            self.wfile.flush()
            time.sleep(0.03)

        done = (
            json.dumps(
                {
                    "model": model,
                    "message": {"role": "assistant", "content": ""},
                    "done": True,
                }
            )
            + "\n"
        ).encode()
        self.wfile.write(f"{len(done):x}\r\n".encode() + done + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
