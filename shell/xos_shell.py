#!/usr/bin/env python3
"""
xOS Shell
=========

A dependency-free Tkinter kiosk frontend for a local Ollama server.

Design constraints:
  * Python 3 + Tk only (no pip packages at runtime).
  * Never blocks the Tk main loop; HTTP streaming happens on a worker thread.
  * Talks to Ollama over 127.0.0.1 only.
  * Fullscreen by default, with a windowed escape hatch for development.

Environment:
  XOS_OLLAMA_URL       default: http://127.0.0.1:11434
  XOS_MODEL            default: phi3:mini
  XOS_NUM_CTX          default: 4096
  XOS_NUM_PREDICT      default: 768
  XOS_TEMPERATURE      default: 0.4
  XOS_LANGUAGE         UI language: en (default)
  XOS_WINDOWED=1       run in a 1200x760 window (for development)
  XOS_ALLOW_QUIT=1     enable Ctrl+Shift+Q
  XOS_SYSTEM_PROMPT_FILE
                       default: /etc/xos/system-prompt.txt
"""

from __future__ import annotations

import json
import logging
import os
import queue
import socket
import threading
import datetime
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from typing import Any
from urllib import error, request


def get_env(primary: str, secondary: str | None = None, default: str = "") -> str:
    if primary in os.environ:
        return os.environ[primary]
    if secondary and secondary in os.environ:
        return os.environ[secondary]
    return default


APP_NAME = "xOS"
OLLAMA_URL = get_env("XOS_OLLAMA_URL", "NALAR_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL = get_env("XOS_MODEL", "NALAR_MODEL", "phi3:mini")
WINDOWED = get_env("XOS_WINDOWED", "NALAR_WINDOWED", "0") == "1"
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

# UI and assistant language support. English is the default. The dictionary is
# intentionally flat and easy to extend: add a locale code here, then copy the
# English keys and translate the values. Missing keys fall back to English.
SUPPORTED_LANGUAGES: dict[str, str] = {
    "en": "English",
    "id": "Bahasa Indonesia",
}

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "connecting": "connecting",
        "ready": "ready",
        "ready_model": "ready · {model}",
        "offline": "ollama offline",
        "model_missing": "model not installed",
        "generating": "generating...",
        "stopping": "stopping...",
        "stopped": "stopped",
        "error_status": "error · see message",
        "send": "Send",
        "stop": "Stop",
        "footer_hint": "Enter to send  ·  /clear reset",
        "quit_hint": "  ·  Ctrl+Shift+Q quit",
        "offline_badge": "LOCAL  ·  OFFLINE",
        "welcome_kicker": "OFFLINE COGNITIVE SHELL",
        "welcome_title": "What would you like to think about?",
        "welcome_sub": "Type an instruction below. The model runs locally on this machine.",
        "label_you": "YOU",
        "label_assistant": "xOS",
        "label_nalar": "xOS",
        "label_system": "SYSTEM",
        "help": (
            "/clear               start a new conversation\n"
            "/models              list all installed local models\n"
            "/model               show active model info\n"
            "/model switch <name> switch active model\n"
            "/model rm <name>     delete an installed model\n"
            "/model pull <name>   pull a new model from registry\n"
            "/language [code]     show or change UI language \n"
            "/help                show this help\n"
        ),
        "model_help": (
            "Model commands:\n"
            "  /models              list installed models\n"
            "  /model switch <name> switch active model\n"
            "  /model rm <name>     delete a model\n"
            "  /model pull <name>   pull a new model\n"
        ),
        "models_title": "Installed Models ({count}):\n",
        "models_empty": "No models currently installed in Ollama.\n",
        "models_hint": "Use '/model switch <name>' to switch, or '/model rm <name>' to delete.\n",
        "model_active_badge": "[ACTIVE]",
        "model_switched": "Active model switched to '{model}'.\n",
        "model_not_found": "Model '{model}' is not installed.\nInstalled: {available}\n",
        "model_deleted": "Model '{model}' successfully deleted.\n",
        "model_delete_fail": "Failed to delete model '{model}': {detail}\n",
        "model_pull_start": "Pulling model '{model}'...\n",
        "model_pull_done": "Model '{model}' successfully pulled!\n",
        "model_pull_fail": "Failed to pull model '{model}': {detail}\n",
        "model_rm_usage": "Usage: /model rm <model_name>\n",
        "model_switch_usage": "Usage: /model switch <model_name>\n",
        "model_pull_usage": "Usage: /model pull <model_name>\n",
        "model_info": "Model: {model}\nEndpoint: {url}\nLanguage: {language} ({code})\n",
        "unknown_command": "Unknown command: {name}\nTry /help\n",
        "language_current": (
            "Language: {language} ({code})\n"
            "Available: {available}\n"
            "Usage: /language <code>\n"
        ),
        "language_changed": "Language set to {language} ({code}).\n",
        "language_invalid": "Unsupported language: {code}\nAvailable: {available}\n",
        "error_no_model": (
            "Model '{model}' is not installed in Ollama.\n"
            "Run: ollama pull {model}"
        ),
        "error_http": "Ollama HTTP {code}: {detail}",
        "error_connect": (
            "Cannot reach Ollama at {url}.\n"
            "Check: systemctl status ollama\nDetail: {reason}"
        ),
        "error_timeout": (
            "Ollama did not respond in time.\n"
            "The model may still be loading, or the VM has too little RAM."
        ),
        "error_generic": "Something went wrong: {detail}",
        "no_response": "(no response)",
        "stopped_note": "[stopped]",
    },
    "id": {
        "connecting": "menghubungkan",
        "ready": "siap",
        "ready_model": "siap · {model}",
        "offline": "ollama offline",
        "model_missing": "model belum dipasang",
        "generating": "menghasilkan...",
        "stopping": "menghentikan...",
        "stopped": "dihentikan",
        "error_status": "error · lihat pesan",
        "send": "Kirim",
        "stop": "Stop",
        "footer_hint": "Enter kirim  ·  /clear reset",
        "quit_hint": "  ·  Ctrl+Shift+Q keluar",
        "offline_badge": "LOKAL  ·  OFFLINE",
        "welcome_kicker": "OFFLINE COGNITIVE SHELL",
        "welcome_title": "Apa yang ingin Anda pikirkan?",
        "welcome_sub": "Ketik instruksi di bawah. Model berjalan lokal di mesin ini.",
        "label_you": "ANDA",
        "label_assistant": "xOS",
        "label_nalar": "xOS",
        "label_system": "SISTEM",
        "help": (
            "/clear               mulai percakapan baru\n"
            "/models              tampilkan semua model lokal yang terpasang\n"
            "/model               tampilkan info model aktif\n"
            "/model switch <nama> ganti model yang aktif\n"
            "/model rm <nama>     hapus model dari penyimpanan\n"
            "/model pull <nama>   unduh model baru dari registry\n"
            "/language [kode]     tampilkan atau ganti bahasa UI (/language en)\n"
            "/help                tampilkan bantuan ini\n"
        ),
        "model_help": (
            "Perintah model:\n"
            "  /models              daftar model terpasang\n"
            "  /model switch <nama> ganti model aktif\n"
            "  /model rm <nama>     hapus model\n"
            "  /model pull <nama>   unduh model baru\n"
        ),
        "models_title": "Model Terpasang ({count}):\n",
        "models_empty": "Belum ada model yang terpasang di Ollama.\n",
        "models_hint": "Gunakan '/model switch <nama>' untuk berganti, atau '/model rm <nama>' untuk menghapus.\n",
        "model_active_badge": "[AKTIF]",
        "model_switched": "Model aktif diubah menjadi '{model}'.\n",
        "model_not_found": "Model '{model}' belum terpasang.\nModel terpasang: {available}\n",
        "model_deleted": "Model '{model}' berhasil dihapus.\n",
        "model_delete_fail": "Gagal menghapus model '{model}': {detail}\n",
        "model_pull_start": "Mulai mengunduh model '{model}'...\n",
        "model_pull_done": "Model '{model}' berhasil diunduh!\n",
        "model_pull_fail": "Gagal mengunduh model '{model}': {detail}\n",
        "model_rm_usage": "Gunakan: /model rm <nama_model>\n",
        "model_switch_usage": "Gunakan: /model switch <nama_model>\n",
        "model_pull_usage": "Gunakan: /model pull <nama_model>\n",
        "model_info": "Model: {model}\nEndpoint: {url}\nBahasa: {language} ({code})\n",
        "unknown_command": "Perintah tidak dikenal: {name}\nCoba /help\n",
        "language_current": (
            "Bahasa: {language} ({code})\n"
            "Tersedia: {available}\n"
            "Gunakan: /language <kode>\n"
        ),
        "language_changed": "Bahasa diubah ke {language} ({code}).\n",
        "language_invalid": "Bahasa tidak didukung: {code}\nTersedia: {available}\n",
        "error_no_model": (
            "Model '{model}' tidak ditemukan di Ollama.\n"
            "Jalankan: ollama pull {model}"
        ),
        "error_http": "Ollama HTTP {code}: {detail}",
        "error_connect": (
            "Tidak dapat terhubung ke Ollama di {url}.\n"
            "Periksa: systemctl status ollama\nDetail: {reason}"
        ),
        "error_timeout": (
            "Ollama tidak merespons dalam batas waktu.\n"
            "Model mungkin sedang dimuat atau RAM VM terlalu kecil."
        ),
        "error_generic": "Terjadi kesalahan: {detail}",
        "no_response": "(tidak ada respons)",
        "stopped_note": "[dihentikan]",
    },
    
}


def normalize_language(value: str) -> str | None:
    """Return a supported locale code, accepting en-US/en_US variants."""
    code = value.strip().lower().replace("_", "-").split("-", 1)[0]
    return code if code in SUPPORTED_LANGUAGES else None


def load_system_prompt() -> str:
    try:
        text = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip()
        return text or DEFAULT_SYSTEM_PROMPT
    except OSError:
        return DEFAULT_SYSTEM_PROMPT


def env_int(primary: str, secondary: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(get_env(primary, secondary, str(default)))
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def env_float(primary: str, secondary: str | None, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(get_env(primary, secondary, str(default)))
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


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


class XOSShell:
    # Palette
    BG = "#080a0d"
    PANEL = "#0f131a"
    PANEL_2 = "#141924"
    BORDER = "#222a38"
    FG = "#e8ebf1"
    ASSISTANT = "#cbd2df"
    MUTED = "#7f899b"
    ACCENT = "#67e8f9"
    ACCENT_SOFT = "#164e63"
    GREEN = "#5eead4"
    AMBER = "#fbbf24"
    RED = "#fb7185"
    ERROR = "#fda4af"
    BUTTON_TEXT = "#041014"

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_NAME)
        self.root.configure(bg=self.BG)
        self.root.minsize(860, 560)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

        self.ui_font, self.mono_font = self._pick_fonts()
        self.scale = self._ui_scale()
        self.fs = lambda size: max(9, int(round(size * self.scale)))

        requested_language = get_env("XOS_LANGUAGE", "NALAR_LANGUAGE", "en")
        self.language = normalize_language(requested_language) or "en"
        if normalize_language(requested_language) is None:
            logging.warning("Unsupported language=%r; using en", requested_language)
        self.system_prompt_base = SYSTEM_PROMPT

        self.history: list[dict[str, str]] = [
            {"role": "system", "content": self._system_prompt()}
        ]
        self.events: queue.Queue[tuple[str, Any, int]] = queue.Queue()
        self.busy = False
        self.stop_event = threading.Event()
        self.response: Any | None = None
        self.generation = 0
        self.health_busy = False
        self.health_checked = False
        self.server_ok = False
        self.model_ok = False
        self.active_model = MODEL
        self.installed_model_names: set[str] = set()
        self.welcome_visible = True
        self.status_var = tk.StringVar(value=self.tr("connecting"))

        self._build_ui()
        self._bind_keys()

        if WINDOWED:
            self.root.geometry("1200x760")
        else:
            self.root.attributes("-fullscreen", True)

        self.root.after(50, self._drain_events)
        self.root.after(200, self._health_check)
        self.root.after(250, self._assert_fullscreen)
        self.root.after(20_000, self._periodic_health)
        self.root.after(300, self.entry.focus_set)
        logging.info(
            "xOS shell started; model=%s url=%s language=%s",
            MODEL,
            OLLAMA_URL,
            self.language,
        )

    # ------------------------------------------------------------- language
    def tr(self, key: str, **values: Any) -> str:
        """Translate a UI key, falling back to English."""
        template = STRINGS.get(self.language, {}).get(key)
        if template is None:
            template = STRINGS["en"][key]
        return template.format(**values)

    def _system_prompt(self) -> str:
        name = SUPPORTED_LANGUAGES[self.language]
        return (
            f"{self.system_prompt_base.rstrip()}\n\n"
            f"Interface language: {name} ({self.language}). "
            f"Default to {name} unless the user writes in another language."
        )

    def _available_languages(self) -> str:
        return ", ".join(
            f"{code} ({name})" for code, name in SUPPORTED_LANGUAGES.items()
        )

    def _set_language(self, code: str) -> bool:
        if code not in SUPPORTED_LANGUAGES:
            return False
        self.language = code
        if self.history:
            self.history[0]["content"] = self._system_prompt()
        self._refresh_language_ui()
        return True

    def _refresh_language_ui(self) -> None:
        self._set_busy(self.busy)
        quit_hint = self.tr("quit_hint") if ALLOW_QUIT else ""
        self.footer_hint_label.configure(text=self.tr("footer_hint") + quit_hint)
        self.offline_badge.configure(text=self.tr("offline_badge"))
        self.welcome_kicker_label.configure(text=self.tr("welcome_kicker"))
        self.welcome_title_label.configure(text=self.tr("welcome_title"))
        self.welcome_sub_label.configure(text=self.tr("welcome_sub"))
        self._refresh_status()

    def _refresh_status(self) -> None:
        if self.busy:
            self._set_status(self.tr("generating"), self.ACCENT)
        elif not self.health_checked:
            self._set_status(self.tr("connecting"), self.AMBER)
        elif not self.server_ok:
            self._set_status(self.tr("offline"), self.RED)
        elif not self.model_ok:
            self._set_status(self.tr("model_missing"), self.AMBER)
        else:
            self._set_status(self.tr("ready_model", model=self.active_model), self.GREEN)

    # ------------------------------------------------------------------ UI
    def _pick_fonts(self) -> tuple[str, str]:
        families = set(tkfont.families(self.root))
        for candidate in ("Inter", "IBM Plex Sans", "DejaVu Sans", "Liberation Sans"):
            if candidate in families:
                ui = candidate
                break
        else:
            ui = "TkDefaultFont"

        for candidate in ("IBM Plex Mono", "DejaVu Sans Mono", "Liberation Mono"):
            if candidate in families:
                mono = candidate
                break
        else:
            mono = "TkFixedFont"
        return ui, mono

    def _ui_scale(self) -> float:
        width = self.root.winfo_screenwidth()
        height = self.root.winfo_screenheight()
        return max(0.80, min(1.20, min(width / 1280.0, height / 800.0)))

    def _build_ui(self) -> None:
        screen_w = self.root.winfo_screenwidth()
        side = max(28, int((screen_w - 1120) / 2))
        vpad = self.fs(26)

        shell = tk.Frame(self.root, bg=self.BG)
        shell.pack(fill="both", expand=True, padx=side, pady=(vpad, self.fs(18)))

        # Header ---------------------------------------------------------
        header = tk.Frame(shell, bg=self.BG)
        header.pack(fill="x")

        brand = tk.Frame(header, bg=self.BG)
        brand.pack(side="left")
        tk.Label(
            brand,
            text="xOS",
            bg=self.BG,
            fg=self.FG,
            font=(self.ui_font, self.fs(14), "bold"),
        ).pack(side="left")


        self.clock_label = tk.Label(
            header,
            text="",
            bg=self.BG,
            fg=self.MUTED,
            font=(self.mono_font, self.fs(9)),
        )
        self.clock_label.pack(side="right", padx=(self.fs(14), 0))

        def update_clock():
            now = datetime.datetime.now().strftime("%H:%M:%S")
            self.clock_label.configure(text=now)
            self.root.after(1000, update_clock)
        
        self.root.after(0, update_clock)


        self.model_chip = tk.Label(
            header,
            text=self.active_model,
            bg=self.PANEL,
            fg=self.MUTED,
            font=(self.mono_font, self.fs(9)),
            padx=self.fs(10),
            pady=self.fs(4),
            highlightbackground=self.BORDER,
            highlightthickness=1,
        )
        self.model_chip.pack(side="right", padx=(self.fs(14), 0))

        self.status_label = tk.Label(
            header,
            textvariable=self.status_var,
            bg=self.BG,
            fg=self.MUTED,
            font=(self.ui_font, self.fs(9)),
        )
        self.status_label.pack(side="right")

        self.dot = tk.Canvas(
            header,
            width=self.fs(10),
            height=self.fs(10),
            bg=self.BG,
            highlightthickness=0,
            bd=0,
        )
        self.dot_id = self.dot.create_oval(
            2, 2, self.fs(8), self.fs(8), fill=self.AMBER, outline=""
        )
        self.dot.pack(side="right", padx=(0, self.fs(7)))

        tk.Frame(shell, bg=self.BORDER, height=1).pack(fill="x", pady=(self.fs(16), 0))

        # Conversation ---------------------------------------------------
        body = tk.Frame(shell, bg=self.BG)
        body.pack(fill="both", expand=True, pady=(self.fs(22), 0))

        self.text = tk.Text(
            body,
            bg=self.BG,
            fg=self.ASSISTANT,
            insertbackground=self.ACCENT,
            selectbackground=self.ACCENT_SOFT,
            selectforeground=self.FG,
            inactiveselectbackground=self.ACCENT_SOFT,
            font=(self.ui_font, self.fs(12)),
            wrap="word",
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=0,
            pady=0,
            cursor="arrow",
            takefocus=0,
        )
        self.text.pack(fill="both", expand=True)

        self.text.tag_configure(
            "label_user",
            foreground=self.ACCENT,
            font=(self.ui_font, self.fs(9), "bold"),
            spacing1=self.fs(18),
            spacing3=self.fs(3),
        )
        self.text.tag_configure(
            "user",
            foreground=self.FG,
            font=(self.ui_font, self.fs(13)),
            spacing3=self.fs(8),
            lmargin1=0,
            lmargin2=0,
        )
        self.text.tag_configure(
            "label_assistant",
            foreground=self.MUTED,
            font=(self.ui_font, self.fs(9), "bold"),
            spacing1=self.fs(16),
            spacing3=self.fs(3),
        )
        self.text.tag_configure(
            "label_nalar",
            foreground=self.MUTED,
            font=(self.ui_font, self.fs(9), "bold"),
            spacing1=self.fs(16),
            spacing3=self.fs(3),
        )
        self.text.tag_configure(
            "assistant",
            foreground=self.ASSISTANT,
            font=(self.ui_font, self.fs(12)),
            spacing3=self.fs(14),
            lmargin1=0,
            lmargin2=0,
        )
        self.text.tag_configure(
            "notice",
            foreground=self.MUTED,
            font=(self.mono_font, self.fs(10)),
            spacing1=self.fs(8),
            spacing3=self.fs(8),
            lmargin1=0,
            lmargin2=0,
        )
        self.text.tag_configure(
            "error",
            foreground=self.ERROR,
            font=(self.ui_font, self.fs(11)),
            spacing1=self.fs(6),
            spacing3=self.fs(12),
        )
        self.text.configure(state="disabled")

        self._bind_text_scroll()
        self._build_welcome(body)

        # Input ----------------------------------------------------------
        input_outer = tk.Frame(shell, bg=self.BG)
        input_outer.pack(fill="x", pady=(self.fs(18), 0))

        self.input_box = tk.Frame(
            input_outer,
            bg=self.PANEL,
            highlightbackground=self.BORDER,
            highlightthickness=1,
        )
        self.input_box.pack(fill="x")

        tk.Label(
            self.input_box,
            text="›",
            bg=self.PANEL,
            fg=self.ACCENT,
            font=(self.ui_font, self.fs(18)),
        ).pack(side="left", padx=(self.fs(15), self.fs(4)))

        self.entry = tk.Entry(
            self.input_box,
            bg=self.PANEL,
            fg=self.FG,
            insertbackground=self.ACCENT,
            insertwidth=2,
            selectbackground=self.ACCENT_SOFT,
            selectforeground=self.FG,
            font=(self.ui_font, self.fs(13)),
            relief="flat",
            bd=0,
            highlightthickness=0,
        )
        self.entry.pack(
            side="left",
            fill="x",
            expand=True,
            ipady=self.fs(13),
            pady=self.fs(2),
        )
        self.entry.bind("<FocusIn>", lambda _e: self.input_box.configure(
            highlightbackground=self.ACCENT
        ))
        self.entry.bind("<FocusOut>", lambda _e: self.input_box.configure(
            highlightbackground=self.BORDER
        ))

        self.send_btn = tk.Button(
            self.input_box,
            text=self.tr("send"),
            command=self._on_send_button,
            bg=self.ACCENT,
            fg=self.BUTTON_TEXT,
            activebackground="#a5f3fc",
            activeforeground=self.BUTTON_TEXT,
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=self.fs(17),
            pady=self.fs(8),
            font=(self.ui_font, self.fs(9), "bold"),
            cursor="hand2",
            takefocus=0,
        )
        self.send_btn.pack(side="right", padx=self.fs(7), pady=self.fs(7))

        footer = tk.Frame(shell, bg=self.BG)
        footer.pack(fill="x", pady=(self.fs(9), 0))

        quit_hint = self.tr("quit_hint") if ALLOW_QUIT else ""
        self.footer_hint_label = tk.Label(
            footer,
            text=self.tr("footer_hint") + quit_hint,
            bg=self.BG,
            fg=self.MUTED,
            font=(self.ui_font, self.fs(8)),
        )
        self.footer_hint_label.pack(side="left")

        self.offline_badge = tk.Label(
            footer,
            text=self.tr("offline_badge"),
            bg=self.BG,
            fg=self.MUTED,
            font=(self.mono_font, self.fs(8)),
        )
        self.offline_badge.pack(side="right")

    def _build_welcome(self, parent: tk.Widget) -> None:
        self.welcome = tk.Frame(parent, bg=self.BG)
        self.welcome.place(relx=0.5, rely=0.43, anchor="center")
        self.welcome.bind("<Button-1>", lambda _e: self.entry.focus_set())

        self.welcome_kicker_label = tk.Label(
            self.welcome,
            text=self.tr("welcome_kicker"),
            bg=self.BG,
            fg=self.MUTED,
            font=(self.mono_font, self.fs(8)),
        )
        self.welcome_kicker_label.pack(pady=(0, self.fs(13)))

        self.welcome_title_label = tk.Label(
            self.welcome,
            text=self.tr("welcome_title"),
            bg=self.BG,
            fg=self.FG,
            font=(self.ui_font, self.fs(27)),
        )
        self.welcome_title_label.pack()

        self.welcome_sub_label = tk.Label(
            self.welcome,
            text=self.tr("welcome_sub"),
            bg=self.BG,
            fg=self.MUTED,
            font=(self.ui_font, self.fs(10)),
        )
        self.welcome_sub_label.pack(pady=(self.fs(10), 0))

    def _assert_fullscreen(self) -> None:
        if WINDOWED:
            return
        try:
            self.root.attributes("-fullscreen", True)
            self.root.lift()
            self.entry.focus_set()
        except tk.TclError:
            pass

    def _bind_text_scroll(self) -> None:
        def scroll(delta: int) -> str:
            self.text.yview_scroll(delta, "units")
            return "break"

        self.text.bind("<Button-4>", lambda _e: scroll(-3))
        self.text.bind("<Button-5>", lambda _e: scroll(3))
        self.text.bind(
            "<MouseWheel>",
            lambda e: scroll(-3 if e.delta > 0 else 3),
        )

    def _bind_keys(self) -> None:
        self.entry.bind("<Return>", self._on_enter)
        self.entry.bind("<KP_Enter>", self._on_enter)
        self.root.bind("<Control-l>", lambda _e: self._clear())
        self.root.bind("<Control-L>", lambda _e: self._clear())
        self.root.bind("<Escape>", self._on_escape)
        if ALLOW_QUIT:
            self.root.bind("<Control-Shift-Q>", lambda _e: self._close())
            self.root.bind("<Control-Shift-q>", lambda _e: self._close())

    # -------------------------------------------------------------- helpers
    def _at_bottom(self) -> bool:
        try:
            return self.text.yview()[1] >= 0.995
        except tk.TclError:
            return True

    def _append(self, text: str, tag: str, follow: bool | None = None) -> None:
        if follow is None:
            follow = self._at_bottom()
        self.text.configure(state="normal")
        self.text.insert("end", text, tag)
        self.text.configure(state="disabled")
        if follow:
            self.text.see("end")

    def _hide_welcome(self) -> None:
        if self.welcome_visible:
            self.welcome.place_forget()
            self.welcome_visible = False

    def _show_welcome(self) -> None:
        if not self.welcome_visible:
            self.welcome.place(relx=0.5, rely=0.43, anchor="center")
            self.welcome_visible = True

    def _set_status(self, text: str, color: str) -> None:
        self.status_var.set(text)
        self.dot.itemconfigure(self.dot_id, fill=color)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        if busy:
            self.send_btn.configure(
                text=self.tr("stop"),
                bg=self.RED,
                activebackground="#fda4af",
            )
        else:
            self.send_btn.configure(
                text=self.tr("send"),
                bg=self.ACCENT,
                activebackground="#a5f3fc",
            )

    def _scroll_end(self) -> None:
        self.text.see("end")

    # --------------------------------------------------------------- health
    def _periodic_health(self) -> None:
        self._health_check()
        self.root.after(20_000, self._periodic_health)

    def _health_check(self) -> None:
        if self.health_busy:
            return
        self.health_busy = True
        threading.Thread(target=self._health_worker, daemon=True).start()

    def _health_worker(self) -> None:
        try:
            with request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=4) as response:
                payload = json.loads(response.read().decode("utf-8"))
            names = {
                str(item.get("name", ""))
                for item in payload.get("models", [])
                if isinstance(item, dict)
            }
            self.events.put(("health", (True, names, ""), 0))
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            self.events.put(("health", (False, set(), str(exc)), 0))
        finally:
            self.health_busy = False

    def _model_present(self, names: set[str]) -> bool:
        if self.active_model in names:
            return True
        if ":" not in self.active_model:
            return any(name.startswith(self.active_model + ":") for name in names)
        return False

    def _apply_health(self, ok: bool, names: set[str], detail: str) -> None:
        self.health_checked = True
        self.server_ok = ok
        self.installed_model_names = names
        self.model_ok = ok and self._model_present(names)
        if self.busy:
            return
        if not ok:
            logging.debug("Ollama health check failed: %s", detail)
        self._refresh_status()

    # --------------------------------------------------------------- worker
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

    @staticmethod
    def _decode_stream_line(line: str) -> list[dict[str, Any]]:
        """Decode one line, tolerating concatenated JSON objects.

        Ollama normally emits NDJSON (one object per line), but some proxies
        batch objects without a newline. Parsing with raw_decode is cheap and
        keeps a partial/misbehaving stream from killing the UI.
        """
        decoder = json.JSONDecoder()
        objects: list[dict[str, Any]] = []
        position = 0
        while position < len(line):
            value, end = decoder.raw_decode(line, position)
            if not isinstance(value, dict):
                raise ValueError("stream chunk bukan object JSON")
            objects.append(value)
            position = end
            while position < len(line) and line[position].isspace():
                position += 1
        return objects

    def _chat_worker(self, messages: list[dict[str, str]], generation: int) -> None:
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
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{OLLAMA_URL}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        parts: list[str] = []
        response = None
        try:
            response = request.urlopen(req, timeout=600)
            if generation != self.generation or self.stop_event.is_set():
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
                        self.events.put(("token", content, generation))
                    if chunk.get("done"):
                        self.events.put(("finished", "".join(parts), generation))
                        return
            self.events.put(("finished", "".join(parts), generation))
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            if self.stop_event.is_set():
                self.events.put(("finished", "".join(parts), generation))
            else:
                self.events.put(("failed", self._format_error(exc), generation))
        finally:
            if self.response is response:
                self.response = None
            if response is not None:
                try:
                    response.close()
                except Exception:  # noqa: BLE001
                    pass

    def _abort_response(self) -> None:
        """Close the active stream and wake a worker blocked on read()."""
        response = self.response
        if response is None:
            return
        self.response = None
        try:
            sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        except Exception:  # noqa: BLE001 - best effort cancellation
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

    # ---------------------------------------------------------------- events
    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload, generation = self.events.get_nowait()
                if kind == "health":
                    ok, names, detail = payload
                    self._apply_health(ok, names, detail)
                    continue
                if kind == "notice":
                    self._append(str(payload), "notice", follow=True)
                    continue
                if kind == "error":
                    self._append(str(payload), "error", follow=True)
                    continue
                if kind == "model_switched":
                    self._apply_model_switch(str(payload))
                    continue
                if kind == "model_deleted":
                    self._apply_model_deleted(str(payload))
                    continue
                if kind == "model_pulled":
                    self._apply_model_pulled(str(payload))
                    continue
                if generation != self.generation:
                    continue
                if kind == "token":
                    self._append(payload, "assistant", follow=True)
                elif kind == "finished":
                    self._finish(payload)
                elif kind == "failed":
                    self._fail(payload)
        except queue.Empty:
            pass
        except tk.TclError:
            return
        self.root.after(40, self._drain_events)

    def _finish(self, full_text: str) -> None:
        stopped = self.stop_event.is_set()
        self.stop_event.clear()
        self._set_busy(False)

        if full_text:
            self._append("\n", "assistant", follow=True)
            self.history.append({"role": "assistant", "content": full_text})
        elif not stopped:
            self._append(self.tr("no_response") + "\n", "error", follow=True)

        if stopped:
            self._append("\n" + self.tr("stopped_note") + "\n", "notice", follow=True)
            self._set_status(self.tr("stopped"), self.MUTED)
        elif full_text:
            self.server_ok = True
            self.health_checked = True
            self.model_ok = True
            self._set_status(self.tr("ready_model", model=self.active_model), self.GREEN)
        self.entry.focus_set()

    def _fail(self, message: str) -> None:
        self.stop_event.clear()
        self._set_busy(False)
        self._append(f"{message}\n", "error", follow=True)
        self._set_status(self.tr("error_status"), self.RED)
        self.entry.focus_set()

    # ---------------------------------------------------------------- models
    def _apply_model_switch(self, new_model: str) -> None:
        self.active_model = new_model
        self.model_chip.configure(text=self.active_model)
        self.model_ok = True
        self._refresh_status()
        self._append(self.tr("model_switched", model=self.active_model), "notice", follow=True)

    def _apply_model_deleted(self, deleted_model: str) -> None:
        self._append(self.tr("model_deleted", model=deleted_model), "notice", follow=True)
        if self.active_model == deleted_model or (
            ":" not in self.active_model and deleted_model.startswith(self.active_model + ":")
        ):
            remaining = [m for m in self.installed_model_names if m != deleted_model]
            if remaining:
                self._apply_model_switch(remaining[0])
            else:
                self.model_ok = False
                self._refresh_status()
        self._health_check()

    def _apply_model_pulled(self, pulled_model: str) -> None:
        self._append(self.tr("model_pull_done", model=pulled_model), "notice", follow=True)
        self._health_check()

    def _cmd_models_list(self) -> None:
        def worker() -> None:
            try:
                with request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                models = payload.get("models", [])
                lines = []
                if not models:
                    lines.append(self.tr("models_empty"))
                else:
                    lines.append(self.tr("models_title", count=len(models)))
                    for item in models:
                        name = str(item.get("name", "unknown"))
                        size_bytes = item.get("size", 0)
                        size_str = format_size(size_bytes)
                        is_active = (name == self.active_model) or (
                            ":" not in self.active_model and name.startswith(self.active_model + ":")
                        )
                        marker = "  * " if is_active else "    "
                        active_suffix = " " + self.tr("model_active_badge") if is_active else ""
                        lines.append(f"{marker}{name} ({size_str}){active_suffix}\n")
                    lines.append("\n" + self.tr("models_hint"))
                self.events.put(("notice", "".join(lines), 0))
            except Exception as exc:  # noqa: BLE001
                self.events.put(("error", self._format_error(exc) + "\n", 0))

        threading.Thread(target=worker, daemon=True).start()

    def _cmd_switch_model(self, target: str) -> None:
        target = target.strip()
        if not target:
            self._append(self.tr("model_switch_usage"), "notice", follow=True)
            return

        def worker() -> None:
            try:
                with request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                models = [str(item.get("name", "")) for item in payload.get("models", [])]
                matched = None
                for m in models:
                    if m == target or (":" not in target and m.startswith(target + ":")):
                        matched = m
                        break
                if matched:
                    self.events.put(("model_switched", matched, 0))
                else:
                    avail = ", ".join(models) if models else "(none)"
                    self.events.put(
                        ("error", self.tr("model_not_found", model=target, available=avail), 0)
                    )
            except Exception as exc:  # noqa: BLE001
                self.events.put(("error", self._format_error(exc) + "\n", 0))

        threading.Thread(target=worker, daemon=True).start()

    def _cmd_delete_model(self, target: str) -> None:
        target = target.strip()
        if not target:
            self._append(self.tr("model_rm_usage"), "notice", follow=True)
            return

        def worker() -> None:
            try:
                with request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                models = [str(item.get("name", "")) for item in payload.get("models", [])]
                matched = None
                for m in models:
                    if m == target or (":" not in target and m.startswith(target + ":")):
                        matched = m
                        break
                if not matched:
                    avail = ", ".join(models) if models else "(none)"
                    self.events.put(
                        ("error", self.tr("model_not_found", model=target, available=avail), 0)
                    )
                    return

                data = json.dumps({"name": matched, "model": matched}).encode("utf-8")
                req = request.Request(
                    f"{OLLAMA_URL}/api/delete",
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="DELETE",
                )
                with request.urlopen(req, timeout=15) as del_resp:
                    _ = del_resp.read()

                self.events.put(("model_deleted", matched, 0))
            except Exception as exc:  # noqa: BLE001
                self.events.put(
                    ("error", self.tr("model_delete_fail", model=target, detail=str(exc)), 0)
                )

        threading.Thread(target=worker, daemon=True).start()

    def _cmd_pull_model(self, target: str) -> None:
        target = target.strip()
        if not target:
            self._append(self.tr("model_pull_usage"), "notice", follow=True)
            return

        self._append(self.tr("model_pull_start", model=target), "notice", follow=True)

        def worker() -> None:
            try:
                data = json.dumps({"name": target, "stream": True}).encode("utf-8")
                req = request.Request(
                    f"{OLLAMA_URL}/api/pull",
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                last_percent = -1
                with request.urlopen(req, timeout=1800) as resp:
                    for raw in resp:
                        line = raw.decode("utf-8", "replace").strip()
                        if not line:
                            continue
                        chunk = json.loads(line)
                        if "error" in chunk:
                            raise RuntimeError(str(chunk["error"]))
                        total = chunk.get("total", 0)
                        completed = chunk.get("completed", 0)
                        status = chunk.get("status", "")
                        if total > 0:
                            percent = int((completed / total) * 100)
                            if percent != last_percent and percent % 20 == 0:
                                last_percent = percent
                                self.events.put(
                                    ("notice", f"[{target}] {status}: {percent}%\n", 0)
                                )
                        elif status and status not in ("downloading", "pulling"):
                            self.events.put(("notice", f"[{target}] {status}\n", 0))

                self.events.put(("model_pulled", target, 0))
            except Exception as exc:  # noqa: BLE001
                self.events.put(
                    ("error", self.tr("model_pull_fail", model=target, detail=str(exc)), 0)
                )

        threading.Thread(target=worker, daemon=True).start()

    # ---------------------------------------------------------------- actions
    def _on_enter(self, _event: tk.Event) -> str:
        if not self.busy:
            self._send()
        return "break"

    def _on_escape(self, _event: tk.Event) -> str:
        if self.busy:
            self._stop()
        elif self.entry.get():
            self.entry.delete(0, "end")
        return "break"

    def _on_send_button(self) -> None:
        if self.busy:
            self._stop()
        else:
            self._send()

    def _send(self) -> None:
        text = self.entry.get().strip()
        if not text:
            return
        if text.startswith("/"):
            self.entry.delete(0, "end")
            self._command(text)
            return

        self.entry.delete(0, "end")
        if len(text) > 6000:
            text = text[:6000]
        self._hide_welcome()

        self._append(self.tr("label_you") + "\n", "label_user", follow=True)
        self._append(f"{text}\n", "user", follow=True)
        self.history.append({"role": "user", "content": text})

        self._append(self.tr("label_assistant") + "\n", "label_assistant", follow=True)
        self.generation += 1
        generation = self.generation
        self.stop_event.clear()
        self._set_busy(True)
        self._set_status(self.tr("generating"), self.ACCENT)
        threading.Thread(
            target=self._chat_worker,
            args=(self._messages_for_api(), generation),
            daemon=True,
        ).start()

    def _stop(self) -> None:
        if not self.busy:
            return
        self.stop_event.set()
        # Invalidate the in-flight generation immediately. Ollama may still be
        # loading the model and not have sent response headers yet, in which
        # case there is no socket to close and waiting would freeze the UI.
        self.generation += 1
        self._abort_response()
        self._set_busy(False)
        self._append("\n" + self.tr("stopped_note") + "\n", "notice", follow=True)
        self._set_status(self.tr("stopped"), self.MUTED)
        self.entry.focus_set()

    def _clear(self) -> None:
        self.generation += 1
        self.stop_event.set()
        self._abort_response()
        self.stop_event.clear()
        self._set_busy(False)

        self.history = [{"role": "system", "content": self._system_prompt()}]
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self._show_welcome()
        self._refresh_status()
        self.entry.focus_set()

    def _command(self, command: str) -> None:
        parts = command.split()
        name = parts[0].lower()
        if name in ("/clear", "/reset"):
            self._clear()
            return

        self._hide_welcome()
        self._append(self.tr("label_system") + "\n", "label_assistant", follow=True)
        if name in ("/help", "/?"):
            self._append(
                self.tr("help", model=self.active_model),
                "notice",
                follow=True,
            )
        elif name == "/models":
            self._cmd_models_list()
        elif name == "/model":
            if len(parts) == 1:
                self._append(
                    self.tr(
                        "model_info",
                        model=self.active_model,
                        url=OLLAMA_URL,
                        language=SUPPORTED_LANGUAGES[self.language],
                        code=self.language,
                    ),
                    "notice",
                    follow=True,
                )
            else:
                sub = parts[1].lower()
                arg = parts[2] if len(parts) > 2 else ""
                if sub in ("list", "ls"):
                    self._cmd_models_list()
                elif sub in ("switch", "use", "set"):
                    self._cmd_switch_model(arg)
                elif sub in ("rm", "delete", "del"):
                    self._cmd_delete_model(arg)
                elif sub in ("pull", "download", "get"):
                    self._cmd_pull_model(arg)
                else:
                    self._append(
                        self.tr("model_help"),
                        "notice",
                        follow=True,
                    )
        elif name in ("/language", "/lang"):
            if len(parts) == 1:
                self._append(
                    self.tr(
                        "language_current",
                        language=SUPPORTED_LANGUAGES[self.language],
                        code=self.language,
                        available=self._available_languages(),
                    ),
                    "notice",
                    follow=True,
                )
            else:
                requested = parts[1]
                code = normalize_language(requested)
                if code is None or not self._set_language(code):
                    self._append(
                        self.tr(
                            "language_invalid",
                            code=requested,
                            available=self._available_languages(),
                        ),
                        "error",
                        follow=True,
                    )
                else:
                    self._append(
                        self.tr(
                            "language_changed",
                            language=SUPPORTED_LANGUAGES[code],
                            code=code,
                        ),
                        "notice",
                        follow=True,
                    )
        else:
            self._append(
                self.tr("unknown_command", name=name),
                "error",
                follow=True,
            )
        self.entry.focus_set()

    def _close(self) -> None:
        self.generation += 1
        self.stop_event.set()
        response = self.response
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass
        self.root.destroy()


NalarShell = XOSShell


def main() -> int:
    logging.basicConfig(
        level=get_env("XOS_LOG_LEVEL", "NALAR_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    root = tk.Tk()
    XOSShell(root)

    def report_callback_exception(exc_type, exc_value, exc_tb) -> None:
        logging.exception("Unhandled UI error", exc_info=(exc_type, exc_value, exc_tb))
        try:
            root.destroy()
        except tk.TclError:
            pass

    root.report_callback_exception = report_callback_exception
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
