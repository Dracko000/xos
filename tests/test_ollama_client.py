"""Integration tests for the Ollama client paths in the xOS shell.

These run against the real ``tools/mock_ollama.py`` handler (bound to an
ephemeral port) so the HTTP, streaming, and NDJSON parsing code is exercised
end to end without a real model or network access.

Covered:
  * health check success and failure
  * model listing, switching (exact and family name), and deletion
  * pull progress streaming
  * HTTP 404 / 500 and connection-refused error mapping
  * concurrent chats from two shell instances
"""
import importlib.util
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import shell.xos_shell as xos

ROOT = Path(__file__).resolve().parent.parent


def _load_mock():
    spec = importlib.util.spec_from_file_location("mock_ollama", ROOT / "tools" / "mock_ollama.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mock = _load_mock()


class _NullWindow:
    def getmaxyx(self):
        return (30, 100)

    def __getattr__(self, name):
        return lambda *a, **k: None


class _BoomHandler(BaseHTTPRequestHandler):
    """Always answers 500 so the HTTP error path can be asserted."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.dumps({"error": "boom"}).encode()
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


def _shutdown(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


class OllamaClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.thread, cls.url = _serve(mock.Handler)
        cls.boom, cls.boom_thread, cls.boom_url = _serve(_BoomHandler)

    @classmethod
    def tearDownClass(cls):
        _shutdown(cls.server, cls.thread)
        _shutdown(cls.boom, cls.boom_thread)

    def setUp(self):
        self._orig_url = xos.OLLAMA_URL
        self._orig_strict = xos.STRICT_LOCAL
        xos.OLLAMA_URL = self.url
        xos.STRICT_LOCAL = True
        self._models = dict(mock.MODELS)
        mock.MODELS.clear()
        mock.MODELS.update({"phi3:mini": 2_300_000_000, "qwen2.5:3b": 1_900_000_000})

    def tearDown(self):
        xos.OLLAMA_URL = self._orig_url
        xos.STRICT_LOCAL = self._orig_strict
        mock.MODELS.clear()
        mock.MODELS.update(self._models)

    def _shell(self, model="phi3:mini"):
        shell = xos.Shell(_NullWindow(), headless=True)
        shell.language = "en"
        shell.active_model = model
        return shell

    def _pump(self, shell, timeout=8.0):
        """Drain events until background health checks have finished."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            shell._drain()
            if not shell.health_busy:
                shell._drain()
                return
            time.sleep(0.01)
        shell._drain()

    # ---------------------------------------------------------------- health
    def test_health_reports_installed_models(self):
        shell = self._shell()
        shell._health_worker()
        shell._drain()

        self.assertTrue(shell.health_checked)
        self.assertTrue(shell.server_ok)
        self.assertTrue(shell.model_ok)
        self.assertIn("phi3:mini", shell.installed)

    def test_health_failure_is_reported(self):
        shell = self._shell()
        xos.OLLAMA_URL = "http://127.0.0.1:1"  # nothing listens here
        shell._health_worker()
        shell._drain()

        self.assertTrue(shell.health_checked)
        self.assertFalse(shell.server_ok)
        self.assertFalse(shell.model_ok)

    def test_recovers_after_server_returns(self):
        shell = self._shell()
        xos.OLLAMA_URL = "http://127.0.0.1:1"
        shell._health_worker()
        shell._drain()
        self.assertFalse(shell.server_ok)

        # Simulate `systemctl restart ollama`: the next probe must recover.
        xos.OLLAMA_URL = self.url
        shell._health_worker()
        shell._drain()
        self.assertTrue(shell.server_ok)
        self.assertTrue(shell.model_ok)
        self.assertIn("phi3:mini", shell.installed)

    def test_strict_local_rejects_remote_endpoint_without_connecting(self):
        shell = self._shell()
        xos.OLLAMA_URL = "http://203.0.113.9:11434"
        shell._health_check()
        shell._drain()

        self.assertTrue(shell.health_checked)
        self.assertFalse(shell.server_ok)
        self.assertEqual(shell.messages[-1]["role"], "error")
        self.assertIn("Remote endpoint rejected", shell.messages[-1]["text"])

    # ---------------------------------------------------------------- models
    def test_models_listing_marks_active_model(self):
        shell = self._shell()
        shell._models_worker()
        shell._drain()

        text = shell.messages[-1]["text"]
        self.assertIn("Installed models (2)", text)
        self.assertIn("phi3:mini", text)
        self.assertIn("qwen2.5:3b", text)
        self.assertIn("* phi3:mini", text)

    def test_switch_accepts_family_name(self):
        shell = self._shell(model="qwen2.5:3b")
        shell._switch_worker("phi3")
        self._pump(shell)

        self.assertEqual(shell.active_model, "phi3:mini")
        self.assertTrue(shell.model_ok)

    def test_switch_missing_model_reports_available(self):
        shell = self._shell()
        shell._switch_worker("ghost:7b")
        shell._drain()

        self.assertEqual(shell.messages[-1]["role"], "error")
        self.assertIn("ghost:7b", shell.messages[-1]["text"])
        self.assertIn("phi3:mini", shell.messages[-1]["text"])
        self.assertEqual(shell.active_model, "phi3:mini")

    def test_delete_removes_model_from_server(self):
        shell = self._shell()
        shell._delete_worker("qwen2.5:3b")
        self._pump(shell)

        self.assertNotIn("qwen2.5:3b", mock.MODELS)
        self.assertTrue(any("deleted" in m["text"].lower() for m in shell.messages))

    def test_delete_missing_model_does_not_call_server(self):
        shell = self._shell()
        shell._delete_worker("ghost:7b")
        shell._drain()

        self.assertIn("ghost:7b", shell.messages[-1]["text"])
        self.assertEqual(len(mock.MODELS), 2)

    def test_pull_streams_progress_and_installs_model(self):
        shell = self._shell()
        shell._pull_worker("tinyllama:1b")

        events = []
        while not shell.events.empty():
            events.append(shell.events.get_nowait())
        progress = [payload for kind, payload, _ in events if kind == "progress"]
        self.assertGreaterEqual(len(progress), 5, f"too few progress events: {progress}")
        self.assertEqual(progress[-1], "success")
        self.assertTrue(any(kind == "pulled" for kind, _, _ in events))
        self.assertIn("tinyllama:1b", mock.MODELS)

    # ----------------------------------------------------------------- errors
    def test_chat_with_missing_model_maps_to_no_model(self):
        shell = self._shell(model="ghost:7b")
        shell._chat_worker([{"role": "user", "content": "hi"}], shell.generation)
        shell._drain()

        self.assertEqual(shell.messages[-1]["role"], "error")
        self.assertIn("not installed", shell.messages[-1]["text"])

    def test_chat_http_500_maps_to_http_error(self):
        shell = self._shell()
        xos.OLLAMA_URL = self.boom_url
        shell._chat_worker([{"role": "user", "content": "hi"}], shell.generation)
        shell._drain()

        self.assertEqual(shell.messages[-1]["role"], "error")
        self.assertIn("HTTP 500", shell.messages[-1]["text"])

    def test_chat_connection_refused_maps_to_connect_error(self):
        shell = self._shell()
        xos.OLLAMA_URL = "http://127.0.0.1:1"
        shell._chat_worker([{"role": "user", "content": "hi"}], shell.generation)
        shell._drain()

        self.assertEqual(shell.messages[-1]["role"], "error")
        self.assertIn("Cannot reach Ollama", shell.messages[-1]["text"])

    def test_timeout_has_dedicated_message(self):
        shell = self._shell()
        message = shell._format_error(TimeoutError())
        self.assertIn("did not respond in time", message)

    # ----------------------------------------------------------- concurrency
    def test_concurrent_chats_stay_isolated(self):
        first, second = self._shell(), self._shell()

        def run(shell, prompt):
            shell._chat_worker([{"role": "user", "content": prompt}], shell.generation)

        threads = [
            threading.Thread(target=run, args=(first, "alpha")),
            threading.Thread(target=run, args=(second, "beta")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertFalse(any(t.is_alive() for t in threads), "chat worker hung")

        first._drain()
        second._drain()
        for shell in (first, second):
            assistant = [m for m in shell.messages if m["role"] == "assistant"]
            self.assertEqual(len(assistant), 1)
            self.assertIn("xOS berjalan lokal", assistant[0]["text"])
            # history[0] is the system prompt; the answer is the only other turn.
            self.assertEqual(shell.history[0]["role"], "system")
            self.assertEqual(len(shell.history), 2)
            self.assertEqual(shell.history[1]["role"], "assistant")
            self.assertIn("xOS berjalan lokal", shell.history[1]["content"])


if __name__ == "__main__":
    unittest.main()
