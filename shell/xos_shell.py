#!/usr/bin/env python3
"""
xOS Shell
=========

A dependency-free terminal (curses) cognitive shell for a local Ollama server.

Design constraints:
  * Python 3 standard library only (no pip packages at runtime).
  * Never blocks the UI thread: every network call runs on a worker thread and
    reports back through a queue.
  * Talks to Ollama over 127.0.0.1 only.
  * Runs on a plain Linux console (no Xorg, no display server required).

Environment:
  XOS_OLLAMA_URL       default: http://127.0.0.1:11434
  XOS_MODEL            default: phi3:mini
  XOS_NUM_CTX          default: 4096
  XOS_NUM_PREDICT      default: 768
  XOS_TEMPERATURE      default: 0.4
  XOS_LANGUAGE         UI language: en (default) or id
  XOS_ALLOW_QUIT=1     enable Ctrl+C / /quit
  XOS_SYSTEM_PROMPT_FILE
                       default: /etc/xos/system-prompt.txt
"""

from __future__ import annotations

import curses
import datetime
import json
import logging
import os
import queue
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib import error, request


# --------------------------------------------------------------------- config
def get_env(primary: str, secondary: str | None = None, default: str = "") -> str:
    if primary in os.environ:
        return os.environ[primary]
    if secondary and secondary in os.environ:
        return os.environ[secondary]
    return default


def is_loopback(url: str) -> bool:
    """Verify if the URL points to a local loopback address."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = parsed.hostname
        if host is None:
            # Handle port-only URLs (e.g. http://:11434) or raw loopback strings
            if parsed.netloc.startswith(":") or not parsed.netloc:
                if not parsed.netloc and not url.startswith(("127.0.0.1", "localhost", "::1")):
                    return False
                return True
            return False
        return host in ("localhost", "127.0.0.1", "::1")
    except Exception:
        return False


APP_NAME = "xOS"
OLLAMA_URL = get_env("XOS_OLLAMA_URL", "NALAR_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
STRICT_LOCAL = get_env("XOS_STRICT_LOCAL", None, "1") == "1"
MODEL = get_env("XOS_MODEL", "NALAR_MODEL", "phi3:mini")
ALLOW_QUIT = get_env("XOS_ALLOW_QUIT", "NALAR_ALLOW_QUIT", "0") == "1"
SYSTEM_PROMPT_FILE = Path(
    get_env("XOS_SYSTEM_PROMPT_FILE", "NALAR_SYSTEM_PROMPT_FILE", "/etc/xos/system-prompt.txt")
)

DEFAULT_SYSTEM_PROMPT = """\
You are xOS, a cognitive interface running entirely offline on the user's \
computer. You are not a real AGI; you are a local language model that helps \
the user think, write, explain, and solve problems.

Rules:
- Reply in the language the user uses. Default: English.
- Be concise, concrete, and honest. Distinguish facts from guesses.
- You have no access to the internet, camera, microphone, or user data except \
what is provided in this conversation.
- Never claim you have performed an action when you have not. If you need \
data, ask for it.
- For risky instructions (medical, legal, security, destructive commands), \
give a proportionate warning and recommend human verification.
"""

SUPPORTED_LANGUAGES: dict[str, str] = {"en": "English", "id": "Bahasa Indonesia"}

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "connecting": "connecting",
        "ready": "ready",
        "offline": "ollama offline",
        "model_missing": "model missing",
        "generating": "generating",
        "stopped": "stopped",
        "error": "error",
        "brand": "OFFLINE COGNITIVE SHELL",
        "alpha": "ALPHA 0.1",
        "sidebar_title": "SYSTEM",
        "lbl_time": "TIME",
        "lbl_model": "MODEL",
        "lbl_lang": "LANG",
        "lbl_state": "STATE",
        "lbl_keys": "KEYS",
        "hint": "Enter send  ·  Esc stop  ·  ^L clear  ·  /help",
        "input_prompt": "› ",
        "key_send": "send",
        "key_stop": "stop",
        "key_clear": "clear",
        "key_commands": "commands",
        "you": "YOU",
        "assistant": "xOS",
        "system": "SYSTEM",
        "welcome_kicker": "OFFLINE COGNITIVE SHELL",
        "welcome_title": "What shall we explore today?",
        "welcome_sub": "Think aloud. The model runs entirely on this machine.",
        "no_response": "(no response)",
        "stopped_note": "[stopped]",
        "help": (
            "/clear                reset conversation\n"
            "/models               list installed models\n"
            "/model                show active model info\n"
            "/model switch <name>  switch active model\n"
            "/model rm <name>      delete an installed model\n"
            "/model pull <name>    pull a new model\n"
            "/language [code]      change UI language\n"
            "/help                 show this help\n"
        ),
        "models_title": "Installed models ({count}):",
        "models_empty": "No models installed in Ollama.",
        "models_hint": "Use /model switch <name> to switch, /model rm <name> to delete.",
        "model_active": "[ACTIVE]",
        "model_switched": "Active model switched to '{model}'.",
        "model_not_found": "Model '{model}' is not installed. Installed: {available}",
        "model_deleted": "Model '{model}' deleted.",
        "model_delete_fail": "Failed to delete '{model}': {detail}",
        "model_pull_start": "Pulling '{model}'...",
        "model_pull_done": "Model '{model}' pulled.",
        "model_pull_fail": "Failed to pull '{model}': {detail}",
        "model_info": "Model: {model}\nEndpoint: {url}\nLanguage: {language} ({code})",
        "language_current": "Language: {language} ({code}). Available: {available}. Usage: /language <code>",
        "language_changed": "Language set to {language} ({code}).",
        "language_invalid": "Unsupported language '{code}'. Available: {available}.",
        "unknown_command": "Unknown command: {name}. Try /help",
        "usage_switch": "Usage: /model switch <name>",
        "usage_rm": "Usage: /model rm <name>",
        "usage_pull": "Usage: /model pull <name>",
        "error_no_model": "Model '{model}' is not installed.\nRun: ollama pull {model}",
        "error_http": "Ollama HTTP {code}: {detail}",
        "error_connect": "Cannot reach Ollama at {url}.\nCheck: systemctl status ollama\nDetail: {reason}",
        "error_timeout": "Ollama did not respond in time. The model may still be loading.",
        "error_generic": "Something went wrong: {detail}",
    },
    "id": {
        "connecting": "menghubungkan",
        "ready": "siap",
        "offline": "ollama offline",
        "model_missing": "model belum dipasang",
        "generating": "menghasilkan",
        "stopped": "dihentikan",
        "error": "error",
        "brand": "OFFLINE COGNITIVE SHELL",
        "alpha": "ALPHA 0.1",
        "sidebar_title": "SISTEM",
        "lbl_time": "WAKTU",
        "lbl_model": "MODEL",
        "lbl_lang": "BAHASA",
        "lbl_state": "STATUS",
        "lbl_keys": "TOMBOL",
        "hint": "Enter kirim  ·  Esc stop  ·  ^L reset  ·  /help",
        "input_prompt": "› ",
        "key_send": "kirim",
        "key_stop": "stop",
        "key_clear": "reset",
        "key_commands": "perintah",
        "you": "ANDA",
        "assistant": "xOS",
        "system": "SISTEM",
        "welcome_kicker": "OFFLINE COGNITIVE SHELL",
        "welcome_title": "Apa yang ingin Anda jelajahi hari ini?",
        "welcome_sub": "Berpikirlah dengan lantang. Model berjalan sepenuhnya di mesin ini.",
        "no_response": "(tidak ada respons)",
        "stopped_note": "[dihentikan]",
        "help": (
            "/clear                mulai percakapan baru\n"
            "/models               daftar model lokal terpasang\n"
            "/model                info model aktif\n"
            "/model switch <nama>  ganti model aktif\n"
            "/model rm <nama>      hapus model terpasang\n"
            "/model pull <nama>    unduh model baru\n"
            "/language [kode]      tampilkan atau ganti bahasa\n"
            "/help                 tampilkan bantuan ini\n"
        ),
        "models_title": "Model terpasang ({count}):",
        "models_empty": "Belum ada model terpasang di Ollama.",
        "models_hint": "Gunakan /model switch <nama> untuk berganti, /model rm <nama> untuk menghapus.",
        "model_active": "[AKTIF]",
        "model_switched": "Model aktif diubah ke '{model}'.",
        "model_not_found": "Model '{model}' belum terpasang. Terpasang: {available}",
        "model_deleted": "Model '{model}' dihapus.",
        "model_delete_fail": "Gagal menghapus '{model}': {detail}",
        "model_pull_start": "Mengunduh '{model}'...",
        "model_pull_done": "Model '{model}' selesai diunduh.",
        "model_pull_fail": "Gagal mengunduh '{model}': {detail}",
        "model_info": "Model: {model}\nEndpoint: {url}\nBahasa: {language} ({code})",
        "language_current": "Bahasa: {language} ({code}). Tersedia: {available}. Gunakan: /language <kode>",
        "language_changed": "Bahasa diubah ke {language} ({code}).",
        "language_invalid": "Bahasa tidak didukung '{code}'. Tersedia: {available}.",
        "unknown_command": "Perintah tidak dikenal: {name}. Coba /help",
        "usage_switch": "Gunakan: /model switch <nama>",
        "usage_rm": "Gunakan: /model rm <nama>",
        "usage_pull": "Gunakan: /model pull <nama>",
        "error_no_model": "Model '{model}' tidak ditemukan.\nJalankan: ollama pull {model}",
        "error_http": "Ollama HTTP {code}: {detail}",
        "error_connect": "Tidak dapat terhubung ke Ollama di {url}.\nPeriksa: systemctl status ollama\nDetail: {reason}",
        "error_timeout": "Ollama tidak merespons dalam batas waktu. Model mungkin sedang dimuat.",
        "error_generic": "Terjadi kesalahan: {detail}",
    },
}


def normalize_language(value: str) -> str | None:
    code = value.strip().lower().replace("_", "-").split("-", 1)[0]
    return code if code in SUPPORTED_LANGUAGES else None


def load_system_prompt() -> str:
    try:
        text = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip()
        return text or DEFAULT_SYSTEM_PROMPT
    except OSError:
        return DEFAULT_SYSTEM_PROMPT


def env_int(primary: str, secondary: str | None, default: int, lo: int, hi: int) -> int:
    try:
        value = int(get_env(primary, secondary, str(default)))
    except ValueError:
        return default
    return max(lo, min(hi, value))


def env_float(primary: str, secondary: str | None, default: float, lo: float, hi: float) -> float:
    try:
        value = float(get_env(primary, secondary, str(default)))
    except ValueError:
        return default
    return max(lo, min(hi, value))


SYSTEM_PROMPT = load_system_prompt()
NUM_CTX = env_int("XOS_NUM_CTX", "NALAR_NUM_CTX", 4096, 512, 131072)
NUM_PREDICT = env_int("XOS_NUM_PREDICT", "NALAR_NUM_PREDICT", 768, 32, 8192)
TEMPERATURE = env_float("XOS_TEMPERATURE", "NALAR_TEMPERATURE", 0.4, 0.0, 2.0)


def format_size(bytes_val: int) -> str:
    if bytes_val <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    val = float(bytes_val)
    while val >= 1024.0 and i < len(units) - 1:
        val /= 1024.0
        i += 1
    return f"{val:.1f} {units[i]}"


# ---------------------------------------------------------------------- shell
class Shell:
    SIDEBAR_W = 26
    TICK_MS = 80          # input poll interval
    REDRAW_S = 0.12       # minimum time between redraws

    def __init__(self, scr: "curses.window", headless: bool = False) -> None:
        self.scr = scr
        self.lock = threading.Lock()
        self.headless = headless

        self.language = normalize_language(get_env("XOS_LANGUAGE", "NALAR_LANGUAGE", "en")) or "en"
        self.system_prompt_base = SYSTEM_PROMPT

        self.language = normalize_language(get_env("XOS_LANGUAGE", "NALAR_LANGUAGE", "en")) or "en"
        self.system_prompt_base = SYSTEM_PROMPT

        self.messages: list[dict[str, Any]] = []
        self.history: list[dict[str, str]] = [{"role": "system", "content": self._system_prompt()}]
        self.input = ""
        self.scroll = 0
        self.busy = False
        self.generation = 0
        self.stop_event = threading.Event()
        self.response: Any | None = None
        self.events: queue.Queue[tuple[str, Any, int]] = queue.Queue()

        self.server_ok = False
        self.model_ok = False
        self.health_checked = False
        self.health_busy = False
        self.active_model = MODEL
        self.installed: set[str] = set()

        self.clock = ""
        self.running = True
        self.dirty = True
        self.last_draw = 0.0
        self.last_clock = 0.0
        self.last_health = 0.0

        if not self.headless:
            self._init_ui()
            self._health_check()
        logging.info("xOS shell started; model=%s url=%s lang=%s", MODEL, OLLAMA_URL, self.language)

    # ------------------------------------------------------------- lifecycle
    def _init_ui(self) -> None:
        curses.curs_set(0)
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        self.scr.keypad(True)
        self.scr.nodelay(True)
        self.scr.timeout(self.TICK_MS)
        self._init_colors()

    def _init_colors(self) -> None:
        self.has256 = curses.COLORS >= 256
        bg = -1
        if self.has256:
            mint, muted, warn, bad, accent = 121, 245, 221, 203, 87
        else:
            mint, muted, warn, bad, accent = curses.COLOR_CYAN, curses.COLOR_WHITE, \
                curses.COLOR_YELLOW, curses.COLOR_RED, curses.COLOR_CYAN
        try:
            curses.init_pair(1, mint, bg)     # mint / accent
            curses.init_pair(2, muted, bg)    # muted text
            curses.init_pair(3, warn, bg)     # warning
            curses.init_pair(4, bad, bg)      # error
            curses.init_pair(5, accent, bg)   # input / highlight
        except curses.error:
            pass
        self.C_MINT = curses.color_pair(1)
        self.C_MUTED = curses.color_pair(2)
        self.C_WARN = curses.color_pair(3)
        self.C_BAD = curses.color_pair(4)
        self.C_INPUT = curses.color_pair(5)
        self.A_BOLD = curses.A_BOLD

    # -------------------------------------------------------------- language
    def tr(self, key: str, **values: Any) -> str:
        template = STRINGS.get(self.language, {}).get(key) or STRINGS["en"].get(key, key)
        return template.format(**values)

    def _system_prompt(self) -> str:
        name = SUPPORTED_LANGUAGES[self.language]
        return (
            f"{self.system_prompt_base.rstrip()}\n\n"
            f"Interface language: {name} ({self.language}). "
            f"Default to {name} unless the user writes in another language."
        )

    def _available_languages(self) -> str:
        return ", ".join(f"{c} ({n})" for c, n in SUPPORTED_LANGUAGES.items())

    def _set_language(self, code: str) -> bool:
        if code not in SUPPORTED_LANGUAGES:
            return False
        self.language = code
        if self.history:
            self.history[0]["content"] = self._system_prompt()
        self.dirty = True
        return True

    # ---------------------------------------------------------------- status
    def _state(self) -> tuple[str, int]:
        if self.busy:
            return self.tr("generating"), self.C_MINT
        if not self.health_checked:
            return self.tr("connecting"), self.C_WARN
        if not self.server_ok:
            return self.tr("offline"), self.C_BAD
        if not self.model_ok:
            return self.tr("model_missing"), self.C_WARN
        return self.tr("ready"), self.C_MINT

    # ------------------------------------------------------------------- net
    def _health_check(self) -> None:
        if self.health_busy:
            return
        if STRICT_LOCAL and not is_loopback(OLLAMA_URL):
            self.events.put(("error", "Security Error: Remote endpoint rejected in strict local mode.", 0))
            self.health_checked = True
            self.server_ok = False
            return
        self.health_busy = True
        threading.Thread(target=self._health_worker, daemon=True).start()

    def _health_worker(self) -> None:
        try:
            with request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=4) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            names = {str(i.get("name", "")) for i in payload.get("models", []) if isinstance(i, dict)}
            self.events.put(("health", (True, names, ""), 0))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("health", (False, set(), str(exc)), 0))
        finally:
            self.health_busy = False

    def _model_present(self, names: set[str]) -> bool:
        if self.active_model in names:
            return True
        if ":" not in self.active_model:
            return any(n.startswith(self.active_model + ":") for n in names)
        return False

    @staticmethod
    def _decode_stream_line(line: str) -> list[dict[str, Any]]:
        try:
            decoder = json.JSONDecoder()
            objects: list[dict[str, Any]] = []
            pos = 0
            while pos < len(line):
                value, end = decoder.raw_decode(line, pos)
                if not isinstance(value, dict):
                    raise ValueError("stream chunk is not a JSON object")
                objects.append(value)
                pos = end
                while pos < len(line) and line[pos].isspace():
                    pos += 1
            return objects
        except Exception:
            logging.error("Malformed NDJSON chunk: %s", line)
            return []

    def _messages_for_api(self) -> list[dict[str, str]]:
        system = self.history[0]
        tail = self.history[1:]
        selected: list[dict[str, str]] = []
        budget = 12_000
        for message in reversed(tail[-16:]):
            content = message.get("content", "")
            if len(content) > budget and selected:
                break
            selected.append(message)
            budget -= len(content)
            if budget <= 0:
                break
        return [system] + list(reversed(selected))

    def _chat_worker(self, messages: list[dict[str, str]], gen: int) -> None:
        if STRICT_LOCAL and not is_loopback(OLLAMA_URL):
            self.events.put(("failed", "Security Error: Remote endpoint rejected in strict local mode.", gen))
            return

        payload = {
            "model": self.active_model,
            "messages": messages,
            "stream": True,
            "keep_alive": "30m",
            "options": {
                "temperature": TEMPERATURE,
                "num_ctx": NUM_CTX,
                "num_predict": NUM_PREDICT,
            },
        }
        req = request.Request(
            f"{OLLAMA_URL}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        parts: list[str] = []
        response = None
        try:
            response = request.urlopen(req, timeout=600)
            if gen != self.generation or self.stop_event.is_set():
                return
            self.response = response
            for raw_line in response:
                if self.stop_event.is_set():
                    break
                line = raw_line.decode("utf-8", "replace").strip()
                if not line:
                    continue
                for chunk in self._decode_stream_line(line):
                    if "error" in chunk:
                        raise RuntimeError(str(chunk["error"]))
                    content = (chunk.get("message") or {}).get("content") or ""
                    if content:
                        parts.append(content)
                        self.events.put(("token", content, gen))
                    if chunk.get("done"):
                        self.events.put(("done", "".join(parts), gen))
                        return
            self.events.put(("done", "".join(parts), gen))
        except Exception as exc:  # noqa: BLE001
            if self.stop_event.is_set():
                self.events.put(("done", "".join(parts), gen))
            else:
                self.events.put(("failed", self._format_error(exc), gen))
        finally:
            if self.response is response:
                self.response = None
            if response is not None:
                try:
                    response.close()
                except Exception:  # noqa: BLE001
                    pass

    def _abort_response(self) -> None:
        response = self.response
        if response is None:
            return
        self.response = None
        try:
            sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        except Exception:  # noqa: BLE001
            pass
        try:
            response.close()
        except Exception:  # noqa: BLE001
            pass

    def _format_error(self, exc: Exception) -> str:
        if isinstance(exc, error.HTTPError):
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")
                detail = json.loads(body).get("error", body)
            except Exception:  # noqa: BLE001
                detail = body or str(exc)
            if exc.code == 404:
                return self.tr("error_no_model", model=self.active_model)
            return self.tr("error_http", code=exc.code, detail=detail)
        if isinstance(exc, error.URLError):
            return self.tr("error_connect", url=OLLAMA_URL, reason=exc.reason)
        if isinstance(exc, TimeoutError):
            return self.tr("error_timeout")
        return self.tr("error_generic", detail=exc)

    # ------------------------------------------------------------- commands
    def _fetch_models(self) -> list[dict[str, Any]]:
        with request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=6) as resp:
            return json.loads(resp.read().decode("utf-8")).get("models", [])

    def _models_worker(self) -> None:
        try:
            models = self._fetch_models()
            lines = []
            if not models:
                lines.append(self.tr("models_empty"))
            else:
                lines.append(self.tr("models_title", count=len(models)))
                for item in models:
                    name = str(item.get("name", "unknown"))
                    is_active = name == self.active_model or (
                        ":" not in self.active_model and name.startswith(self.active_model + ":")
                    )
                    marker = "*" if is_active else " "
                    suffix = " " + self.tr("model_active") if is_active else ""
                    lines.append(f" {marker} {name}  ({format_size(item.get('size', 0))}){suffix}")
                lines.append("")
                lines.append(self.tr("models_hint"))
            self.events.put(("notice", "\n".join(lines), 0))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("error", self._format_error(exc), 0))

    def _switch_worker(self, target: str) -> None:
        try:
            names = [str(i.get("name", "")) for i in self._fetch_models()]
            match = None
            for n in names:
                if n == target or (":" not in target and n.startswith(target + ":")):
                    match = n
                    break
            if match:
                self.events.put(("switched", match, 0))
            else:
                self.events.put(("error", self.tr(
                    "model_not_found", model=target, available=", ".join(names) or "(none)"), 0))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("error", self._format_error(exc), 0))

    def _delete_worker(self, target: str) -> None:
        try:
            names = [str(i.get("name", "")) for i in self._fetch_models()]
            match = next((n for n in names if n == target or
                          (":" not in target and n.startswith(target + ":"))), None)
            if not match:
                self.events.put(("error", self.tr(
                    "model_not_found", model=target, available=", ".join(names) or "(none)"), 0))
                return
            req = request.Request(
                f"{OLLAMA_URL}/api/delete",
                data=json.dumps({"name": match, "model": match}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="DELETE",
            )
            with request.urlopen(req, timeout=20) as resp:
                resp.read()
            self.events.put(("deleted", match, 0))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("error", self.tr("model_delete_fail", model=target, detail=str(exc)), 0))

    def _pull_worker(self, target: str) -> None:
        try:
            req = request.Request(
                f"{OLLAMA_URL}/api/pull",
                data=json.dumps({"name": target, "stream": True}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            last = -1
            with request.urlopen(req, timeout=1800) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    chunk = json.loads(line)
                    if "error" in chunk:
                        raise RuntimeError(str(chunk["error"]))
                    total, done = chunk.get("total", 0), chunk.get("completed", 0)
                    status = chunk.get("status", "")
                    if total > 0:
                        pct = int(done / total * 100)
                        if pct != last and pct % 20 == 0:
                            last = pct
                            self.events.put(("progress", f"{status}: {pct}%", 0))
                    elif status and status not in ("downloading", "pulling"):
                        self.events.put(("progress", status, 0))
            self.events.put(("pulled", target, 0))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("error", self.tr("model_pull_fail", model=target, detail=str(exc)), 0))

    # ---------------------------------------------------------------- events
    def _push(self, role: str, text: str) -> None:
        self.messages.append({"role": role, "text": text})
        self.scroll = 0
        self.dirty = True

    def _drain(self) -> None:
        while True:
            try:
                kind, payload, gen = self.events.get_nowait()
            except queue.Empty:
                return

            if kind == "health":
                ok, names, detail = payload
                self.health_checked = True
                self.server_ok = ok
                self.installed = names
                self.model_ok = ok and self._model_present(names)
                if not ok:
                    logging.debug("health check failed: %s", detail)
                self.dirty = True
                continue
            if kind == "notice":
                self._push("system", str(payload))
                continue
            if kind == "error":
                self._push("error", str(payload))
                continue
            if kind == "progress":
                if self.messages and self.messages[-1]["role"] == "system":
                    self.messages[-1]["text"] = str(payload)
                else:
                    self._push("system", str(payload))
                self.dirty = True
                continue
            if kind == "switched":
                self.active_model = str(payload)
                self.model_ok = True
                self._push("system", self.tr("model_switched", model=self.active_model))
                self._health_check()
                continue
            if kind == "deleted":
                self._push("system", self.tr("model_deleted", model=str(payload)))
                self._health_check()
                continue
            if kind == "pulled":
                self._push("system", self.tr("model_pull_done", model=str(payload)))
                self._health_check()
                continue

            if gen != self.generation:
                continue
            if kind == "token":
                if self.messages and self.messages[-1]["role"] == "assistant":
                    self.messages[-1]["text"] += payload
                else:
                    self._push("assistant", payload)
                self.scroll = 0
                self.dirty = True
            elif kind == "done":
                self._finish(str(payload))
            elif kind == "failed":
                self._fail(str(payload))

    def _finish(self, full_text: str) -> None:
        stopped = self.stop_event.is_set()
        self.stop_event.clear()
        self.busy = False

        if self.messages and self.messages[-1]["role"] == "assistant":
            self.messages[-1]["text"] = full_text or self.messages[-1]["text"]
        if full_text:
            self.history.append({"role": "assistant", "content": full_text})
        elif not stopped:
            self._push("error", self.tr("no_response"))

        if stopped:
            self._push("system", self.tr("stopped_note"))
        elif full_text:
            self.server_ok = True
            self.health_checked = True
            self.model_ok = True
        self.dirty = True

    def _fail(self, message: str) -> None:
        self.stop_event.clear()
        self.busy = False
        if self.messages and self.messages[-1]["role"] == "assistant" and not self.messages[-1]["text"]:
            self.messages.pop()
        self._push("error", message)
        self.dirty = True

    # ------------------------------------------------------------------ draw
    def _wrap(self, text: str, width: int) -> list[str]:
        if width < 8:
            width = 8
        out: list[str] = []
        for para in text.split("\n"):
            if not para:
                out.append("")
                continue
            line = ""
            for word in para.split(" "):
                while len(word) > width:
                    if line:
                        out.append(line)
                        line = ""
                    out.append(word[:width])
                    word = word[width:]
                if not line:
                    line = word
                elif len(line) + 1 + len(word) <= width:
                    line += " " + word
                else:
                    out.append(line)
                    line = word
            out.append(line)
        return out

    def _lines(self, width: int) -> list[tuple[str, int, int]]:
        """Return (text, color_attr, bold) tuples for the whole transcript."""
        rows: list[tuple[str, int, int]] = []
        if not self.messages:
            rows.append(("", 0, 0))
            for txt, attr in (
                (self.tr("welcome_kicker"), self.C_MUTED),
                ("", 0),
                (self.tr("welcome_title"), self.C_INPUT),
                (self.tr("welcome_sub"), self.C_MUTED),
            ):
                rows.append((txt, attr, 0))
            return rows

        for msg in self.messages:
            role = msg["role"]
            if role == "user":
                label, color = self.tr("you"), self.C_MINT
            elif role == "assistant":
                label, color = self.tr("assistant"), self.C_MUTED
            elif role == "error":
                label, color = self.tr("error").upper(), self.C_BAD
            else:
                label, color = self.tr("system"), self.C_WARN
            rows.append((label, color, 1))
            text = msg["text"]
            if role == "assistant" and self.busy and msg is self.messages[-1]:
                text += "▌"
            for line in self._wrap(text, width - 2):
                rows.append(("  " + line, color if role in ("error", "system") else 0, 0))
            rows.append(("", 0, 0))
        return rows

    def _put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        limit = w - x - (1 if y == h - 1 else 0)
        if limit <= 0:
            return
        try:
            self.scr.addstr(y, x, text[:limit], attr)
        except curses.error:
            pass

    def _draw(self) -> None:
        h, w = self.scr.getmaxyx()
        self.scr.erase()
        if h < 12 or w < 50:
            self._put(0, 0, "xOS needs at least 50x12", self.C_BAD)
            self.scr.refresh()
            return

        side = self.SIDEBAR_W
        chat_w = w - side - 1
        body_h = h - 4

        # header
        self._put(0, 0, " " * w, self.C_MINT | curses.A_REVERSE)
        self._put(0, 1, f"{APP_NAME}", self.C_MINT | curses.A_BOLD | curses.A_REVERSE)
        self._put(0, 4, f" {self.tr('brand')}", self.C_MINT | curses.A_REVERSE)
        alpha = self.tr("alpha")
        self._put(0, w - len(alpha) - 2, alpha, self.C_MINT | curses.A_REVERSE)

        # body / transcript
        lines = self._lines(chat_w)
        visible = max(1, body_h)
        max_scroll = max(0, len(lines) - visible)
        self.scroll = min(self.scroll, max_scroll)
        start = max(0, len(lines) - visible - self.scroll)
        for i, (text, color, bold) in enumerate(lines[start:start + visible]):
            attr = color | (curses.A_BOLD if bold else 0)
            self._put(1 + i, 1, text, attr)

        # sidebar
        sx = w - side
        self._put(1, sx - 1, "│", self.C_MUTED)
        
        # status section
        self._put(1, sx + 1, self.tr("lbl_state"), self.C_MUTED | curses.A_BOLD)
        state, state_color = self._state()
        indicator = " ..." if self.busy else ""
        self._put(2, sx + 1, f"{state}{indicator}", state_color | curses.A_BOLD)
        
        # model section
        self._put(3, sx + 1, self.tr("lbl_model"), self.C_MUTED | curses.A_BOLD)
        self._put(4, sx + 1, self.active_model, self.C_MINT)
        
        # language section
        self._put(5, sx + 1, self.tr("lbl_lang"), self.C_MUTED | curses.A_BOLD)
        lang_name = SUPPORTED_LANGUAGES.get(self.language, "Unknown")
        lang_info = f"{lang_name} ({self.language})"
        self._put(6, sx + 1, lang_info, self.C_MINT)
        
        # clock section
        self._put(7, sx + 1, self.tr("lbl_time"), self.C_MUTED | curses.A_BOLD)
        self._put(8, sx + 1, self.clock or "--:--:--", self.C_MINT)
        
        # keys section
        self._put(9, sx + 1, self.tr("lbl_keys"), self.C_MUTED | curses.A_BOLD)
        help_text = self.tr("help")
        for i, line in enumerate(help_text.split("\n")):
            if i < 8: # cap to avoid overflow
                self._put(10 + i, sx + 1, line, self.C_MUTED)
            row += 3

        self._put(row, sx + 1, "─" * (side - 3), self.C_MUTED)
        self._put(row + 1, sx + 1, self.tr("lbl_keys"), self.C_MINT | curses.A_BOLD)
        keys = [
            ("Enter", self.tr("key_send")),
            ("Esc", self.tr("key_stop")),
            ("^L", self.tr("key_clear")),
            ("/help", self.tr("key_commands")),
        ]
        for i, (key, what) in enumerate(keys):
            self._put(row + 2 + i, sx + 1, f"{key:<6}{what}"[: side - 3], self.C_MUTED)

        # separator + input
        self._put(h - 4, 0, "─" * w, self.C_MUTED)
        prompt = self.tr("input_prompt")
        self._put(h - 3, 0, prompt, self.C_MINT | curses.A_BOLD)
        avail = w - len(prompt) - 1
        shown = self.input[-avail:] if len(self.input) > avail else self.input
        self._put(h - 3, len(prompt), shown, self.C_INPUT)

        # hint line
        self._put(h - 2, 0, self.tr("hint")[:w], self.C_MUTED)

        # cursor position in the input
        try:
            curses.curs_set(1)
            cx = min(w - 1, len(prompt) + len(shown))
            self.scr.move(h - 3, cx)
        except curses.error:
            pass

        self.scr.refresh()

    # ----------------------------------------------------------------- input
    def _key(self, key: int) -> None:
        if key == curses.KEY_RESIZE:
            self.dirty = True
            return
        if key in (curses.KEY_BACKSPACE, 127, 8):
            self.input = self.input[:-1]
            self.dirty = True
            return
        if key in (10, 13, curses.KEY_ENTER):
            self._submit()
            return
        if key == 27:  # Esc
            if self.busy:
                self._stop()
            elif self.input:
                self.input = ""
                self.dirty = True
            return
        if key == 12:  # Ctrl+L
            self._clear()
            return
        if key == 3:  # Ctrl+C
            if ALLOW_QUIT:
                self.running = False
            else:
                self._stop()
            return
        if key == 21:  # Ctrl+U
            self.input = ""
            self.dirty = True
            return
        if key == curses.KEY_UP:
            self.scroll += 1
            self.dirty = True
            return
        if key == curses.KEY_DOWN:
            self.scroll = max(0, self.scroll - 1)
            self.dirty = True
            return
        if key == curses.KEY_PPAGE:
            self.scroll += 10
            self.dirty = True
            return
        if key == curses.KEY_NPAGE:
            self.scroll = max(0, self.scroll - 10)
            self.dirty = True
            return
        if 32 <= key <= 0x10FFFF and key < 0x110000:
            try:
                self.input += chr(key)
                self.dirty = True
            except ValueError:
                pass

    def _submit(self) -> None:
        text = self.input.strip()
        if not text:
            return
        self.input = ""
        self.dirty = True
        if text.startswith("/"):
            self._command(text)
            return
        if len(text) > 6000:
            text = text[:6000]
        self._push("user", text)
        self.history.append({"role": "user", "content": text})
        self._push("assistant", "")
        self.generation += 1
        self.stop_event.clear()
        self.busy = True
        threading.Thread(
            target=self._chat_worker,
            args=(self._messages_for_api(), self.generation),
            daemon=True,
        ).start()

    def _stop(self) -> None:
        if not self.busy:
            return
        self.stop_event.set()
        self.generation += 1
        self._abort_response()
        self.busy = False
        self._push("system", self.tr("stopped_note"))
        self.dirty = True

    def _clear(self) -> None:
        self.generation += 1
        self.stop_event.set()
        self._abort_response()
        self.stop_event.clear()
        self.busy = False
        self.messages = []
        self.scroll = 0
        self.history = [{"role": "system", "content": self._system_prompt()}]
        self.dirty = True

    def _command(self, command: str) -> None:
        parts = command.split()
        name = parts[0].lower()

        if name in ("/clear", "/reset"):
            self._clear()
            return
        if name in ("/quit", "/exit"):
            if ALLOW_QUIT:
                self.running = False
            else:
                self._push("system", "Quit is disabled in this build (XOS_ALLOW_QUIT=0).")
            return

        if name in ("/help", "/?"):
            self._push("system", self.tr("help"))
            return
        if name == "/models":
            self._push("system", self.tr("connecting") + "...")
            threading.Thread(target=self._models_worker, daemon=True).start()
            return
        if name == "/model":
            if len(parts) == 1:
                self._push("system", self.tr(
                    "model_info", model=self.active_model, url=OLLAMA_URL,
                    language=SUPPORTED_LANGUAGES[self.language], code=self.language))
                return
            sub = parts[1].lower()
            arg = parts[2] if len(parts) > 2 else ""
            if sub in ("list", "ls"):
                self._push("system", self.tr("connecting") + "...")
                threading.Thread(target=self._models_worker, daemon=True).start()
            elif sub in ("switch", "use", "set"):
                if not arg:
                    self._push("system", self.tr("usage_switch"))
                else:
                    threading.Thread(target=self._switch_worker, args=(arg,), daemon=True).start()
            elif sub in ("rm", "delete", "del"):
                if not arg:
                    self._push("system", self.tr("usage_rm"))
                else:
                    threading.Thread(target=self._delete_worker, args=(arg,), daemon=True).start()
            elif sub in ("pull", "download", "get"):
                if not arg:
                    self._push("system", self.tr("usage_pull"))
                else:
                    self._push("system", self.tr("model_pull_start", model=arg))
                    threading.Thread(target=self._pull_worker, args=(arg,), daemon=True).start()
            else:
                self._push("system", self.tr("help"))
            return
        if name in ("/language", "/lang"):
            if len(parts) == 1:
                self._push("system", self.tr(
                    "language_current", language=SUPPORTED_LANGUAGES[self.language],
                    code=self.language, available=self._available_languages()))
            else:
                code = normalize_language(parts[1])
                if code and self._set_language(code):
                    self._push("system", self.tr(
                        "language_changed", language=SUPPORTED_LANGUAGES[code], code=code))
                else:
                    self._push("system", self.tr(
                        "language_invalid", code=parts[1], available=self._available_languages()))
            return

        self._push("system", self.tr("unknown_command", name=name))

    # ------------------------------------------------------------------ loop
    def run(self) -> None:
        # Set minimum draw interval to prevent CPU spikes and flicker during streaming
        self.MIN_DRAW_INTERVAL = 0.03  # 30ms (~33 FPS)

        while self.running:
            now = time.monotonic()
            self._drain()

            if now - self.last_clock >= 1.0:
                self.clock = datetime.datetime.now().strftime("%H:%M:%S")
                self.last_clock = now
                self.dirty = True
            if now - self.last_health >= 20.0:
                self.last_health = now
                self._health_check()

            if (self.dirty or (now - self.last_draw) >= 1.0) and \
               (now - self.last_draw >= self.MIN_DRAW_INTERVAL):
                self._draw()
                self.dirty = False
                self.last_draw = now

            try:
                key = self.scr.getch()
            except curses.error:
                key = -1
            if key != -1:
                self._key(key)


def main(stdscr: "curses.window") -> None:
    Shell(stdscr).run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if "--selftest" in sys.argv:
        # Headless render check: no TTY required.
        shell = Shell.__new__(Shell)
        shell.language = "en"
        shell.busy = False
        shell.C_MINT = shell.C_MUTED = shell.C_WARN = shell.C_BAD = shell.C_INPUT = 0
        shell.messages = [
            {"role": "user", "text": "Explain what makes this machine different from a cloud assistant."},
            {"role": "assistant", "text": "It has no outbound route. The model runs on loopback, so every "
                                          "prompt stays on this box. Reboot and the machine is clean again."},
        ]
        shell.busy = False
        for line in shell._lines(72):
            print(line[0])
        raise SystemExit(0)
    curses.wrapper(main)
