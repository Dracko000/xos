"""Headless end-to-end tests for the xOS TUI.

The shell is spawned in a real PTY and driven with keystrokes. Because ncurses
only emits *changed* screen cells, assertions target text that is painted in a
single pass (a fresh transcript line, a full first paint) rather than strings
that appear via a one-character diff.

Requires a mock Ollama server on port 11435 (started by tools/ci_check.sh).
"""
import os
import pty
import re
import select
import subprocess
import time
import unittest

ANSI_CSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
ANSI_OTHER = re.compile(r"\x1b[@-_]")


def strip_ansi(data: str) -> str:
    return ANSI_OTHER.sub("", ANSI_CSI.sub("", data))


class TuiHarness(unittest.TestCase):
    extra_env: dict[str, str] = {}

    def setUp(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "TERM": "xterm-256color",
                "XOS_OLLAMA_URL": "http://127.0.0.1:11435",
                "XOS_STRICT_LOCAL": "1",
                "XOS_ALLOW_QUIT": "1",
            }
        )
        env.update(self.extra_env)

        self.master_fd, slave_fd = pty.openpty()
        self.proc = subprocess.Popen(
            ["python3", "shell/xos_shell.py"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            close_fds=True,
        )
        os.close(slave_fd)
        self._buffer = ""

    def tearDown(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        os.close(self.master_fd)

    def send(self, text: str) -> None:
        os.write(self.master_fd, (text + "\r").encode())

    def expect(self, pattern: str, timeout: float = 8.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pattern in self._buffer:
                return True
            ready, _, _ = select.select([self.master_fd], [], [], 0.1)
            if not ready:
                continue
            try:
                chunk = os.read(self.master_fd, 8192).decode("utf-8", "ignore")
            except OSError:
                break
            self._buffer += strip_ansi(chunk)
        return pattern in self._buffer


class TestTUIEnglish(TuiHarness):
    def test_boot_paints_welcome(self) -> None:
        self.assertTrue(
            self.expect("What would you like to think about?"),
            "welcome screen not painted",
        )
        self.assertTrue(self.expect("SYSTEM"), "sidebar not painted")

    def test_help_command(self) -> None:
        self.assertTrue(self.expect("What would you like to think about?"))
        self.send("/help")
        self.assertTrue(self.expect("/model switch <name>"), "help body missing")
        self.assertTrue(self.expect("/language [code]"), "help body missing")

    def test_chat_streams_response(self) -> None:
        self.assertTrue(self.expect("What would you like to think about?"))
        self.send("Hello xOS")
        self.assertTrue(
            self.expect("xOS berjalan lokal"), "streamed model response missing"
        )

    def test_unknown_command_is_reported(self) -> None:
        self.assertTrue(self.expect("What would you like to think about?"))
        self.send("/nonsense")
        self.assertTrue(self.expect("Unknown command"), "unknown command not reported")


class TestTUIIndonesian(TuiHarness):
    extra_env = {"XOS_LANGUAGE": "id"}

    def test_boot_is_indonesian(self) -> None:
        # First paint emits the whole sidebar, so "SISTEM" is a reliable signal.
        self.assertTrue(self.expect("SISTEM"), "sidebar not localised to Indonesian")
        self.assertTrue(
            self.expect("Apa yang ingin Anda pikirkan?"), "welcome not localised"
        )

    def test_language_switch_notice(self) -> None:
        self.assertTrue(self.expect("SISTEM"))
        self.send("/language en")
        self.assertTrue(
            self.expect("Language set to English"), "switch notice not shown"
        )


if __name__ == "__main__":
    unittest.main()
