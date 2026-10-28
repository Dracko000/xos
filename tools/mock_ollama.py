#!/usr/bin/env python3
"""Fake Ollama API for UI smoke tests with multi-model support. Not used by the ISO."""
import json
import os
import time
import random
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("MOCK_PORT", "11435"))

# Configuration for simulating failures
SIMULATE_ERRORS = os.environ.get("SIMULATE_ERRORS", "0") == "1"
SIMULATE_LATENCY = os.environ.get("SIMULATE_LATENCY", "0") == "1"

MODELS: dict[str, int] = {
    "phi3:mini": 2_300_000_000,
    "qwen2.5:3b": 1_900_000_000,
}

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send_json(self, data, status=200, content_type="application/json"):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if SIMULATE_ERRORS and random.random() < 0.1:
            self.send_error(500, "Internal Server Error (Simulated)")
            return

        if self.path == "/api/tags":
            models_list = [
                {"name": name, "model": name, "size": size}
                for name, size in MODELS.items()
            ]
            self._send_json({"models": models_list})
        elif self.path == "/api/version":
            self._send_json({"version": "0.0-mock"})
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
            self._send_json({}, status=200)
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

        # Simulate "Model Not Found" error
        if model not in MODELS and not any(n.startswith(model + ":") for n in MODELS):
            self._send_json({"error": f"model {model} not found"}, status=404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        answer = (
            f"xOS berjalan lokal menggunakan model [{model}]. Ini adalah respons "
            f"streaming dari server mock: model dan antarmuka kiosk terhubung."
        )
        
        # Simulation of a corrupted or interrupted stream
        stream_fail = SIMULATE_ERRORS and random.random() < 0.05

        for word in answer.split(" "):
            if stream_fail:
                # Send a malformed JSON chunk and close connection
                self.wfile.write(b"zzzz\r\n{invalid_json\r\n")
                self.wfile.flush()
                return

            if SIMULATE_LATENCY:
                time.sleep(random.uniform(0.01, 0.2))
            
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
