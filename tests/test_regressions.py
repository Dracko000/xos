"""Regression tests for bugs found while building the xOS shell.

Each test locks in a specific failure that was observed in this repository:

  * ``curses.halfcursor()`` was called but does not exist -> instant crash.
  * The event loop used a blocking ``getch()`` -> clock and streaming froze.
  * The chat request was sent with ``stream: False`` -> no token streaming.
  * ``Shell(headless=True)`` still spawned a urllib worker -> segfault on
    interpreter shutdown.
  * Cancelling a response left the visible transcript and the model history in
    disagreement.
"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import shell.xos_shell as xos

SHELL_SOURCE = (Path(__file__).resolve().parent.parent / "shell" / "xos_shell.py").read_text()


class _CaptureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    last_payload: dict | None = None

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        _CaptureHandler.last_payload = json.loads(self.rfile.read(length) or b"{}")
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for obj in (
            {"model": "m", "message": {"role": "assistant", "content": "partial "}, "done": False},
            {"model": "m", "message": {"role": "assistant", "content": "answer"}, "done": False},
            {"model": "m", "message": {"role": "assistant", "content": ""}, "done": True},
        ):
            data = (json.dumps(obj) + "\n").encode()
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


class _NullWindow:
    def getmaxyx(self):
        return (30, 100)

    def __getattr__(self, name):
        return lambda *a, **k: None


class TestRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self._orig_url = xos.OLLAMA_URL
        xos.OLLAMA_URL = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        xos.OLLAMA_URL = self._orig_url

    # -- bug: curses.halfcursor() does not exist -----------------------------
    def test_no_nonexistent_curses_calls(self):
        self.assertNotIn("halfcursor", SHELL_SOURCE)
        for attr in ("halfcursor", "curs_set", "curses.A_BOLD"):
            if attr.startswith("curses."):
                self.assertIn(attr, SHELL_SOURCE)

    # -- bug: blocking getch() froze the UI ---------------------------------
    def test_input_is_non_blocking(self):
        calls = []

        class RecordingWindow(_NullWindow):
            def nodelay(self, value):
                calls.append(("nodelay", value))

            def timeout(self, value):
                calls.append(("timeout", value))

        shell = xos.Shell(RecordingWindow(), headless=True)
        shell.scr = RecordingWindow()
        # _init_ui touches curses globals; assert the intent from source and
        # from the configured poll interval instead of executing it headless.
        self.assertIn("self.scr.nodelay(True)", SHELL_SOURCE)
        self.assertIn("self.scr.timeout(self.TICK_MS)", SHELL_SOURCE)
        self.assertLessEqual(xos.Shell.TICK_MS, 200)

    # -- bug: stream: False meant no streaming ------------------------------
    def test_chat_request_streams(self):
        shell = xos.Shell(_NullWindow(), headless=True)
        shell._chat_worker([{"role": "user", "content": "hi"}], shell.generation)

        self.assertIsNotNone(_CaptureHandler.last_payload)
        self.assertIs(_CaptureHandler.last_payload["stream"], True)

        shell._drain()
        assistant = [m for m in shell.messages if m["role"] == "assistant"]
        self.assertEqual(len(assistant), 1)
        self.assertEqual(assistant[0]["text"], "partial answer")
        self.assertEqual(
            shell.history[-1], {"role": "assistant", "content": "partial answer"}
        )

    # -- bug: headless construction spawned a thread and segfaulted ---------
    def test_headless_does_not_start_background_work(self):
        before = threading.active_count()
        shell = xos.Shell(_NullWindow(), headless=True)
        self.assertFalse(shell.health_busy)
        self.assertLessEqual(threading.active_count(), before)
        self.assertIsNone(shell.response)

    # -- cancellation consistency ------------------------------------------
    def test_cancel_keeps_partial_visible_and_out_of_history(self):
        shell = xos.Shell(_NullWindow(), headless=True)
        shell._push("user", "hi")
        shell._push("assistant", "")
        shell.busy = True

        # Simulate a stream that has already emitted tokens.
        shell.messages[-1]["text"] = "partial answer"

        shell._stop()

        visible = [m for m in shell.messages if m["role"] == "assistant"]
        self.assertEqual(visible[0]["text"], "partial answer")
        self.assertIn("system", [m["role"] for m in shell.messages])

        # The truncated turn must not be replayed to the model.
        self.assertFalse(
            any(m["role"] == "assistant" for m in shell.history),
            "cancelled partial response leaked into model history",
        )


if __name__ == "__main__":
    unittest.main()
