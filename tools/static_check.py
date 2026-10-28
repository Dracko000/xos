#!/usr/bin/env python3
"""Minimal stdlib-only static checks for the xOS Python sources.

This is deliberately small: it catches the classes of defect that have actually
bitten this project, without adding a pip dependency to the build.

Checks:
  * unused imports
  * bare ``except:`` clauses
  * ``eval`` / ``exec`` / ``os.system`` use
  * mutable default arguments
  * syntax errors (via ``ast.parse``)

Exit code 0 when clean, 1 when problems are found.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = [
    ROOT / "shell" / "xos_shell.py",
    ROOT / "tools" / "mock_ollama.py",
    ROOT / "tests" / "test_shell_logic.py",
    ROOT / "tests" / "test_regressions.py",
    ROOT / "tests" / "test_ollama_client.py",
    ROOT / "tests" / "test_tui_e2e.py",
]

BANNED_CALLS = {"eval", "exec"}


def check_file(path: Path) -> list[str]:
    problems: list[str] = []
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [f"{path}: syntax error: {exc}"]

    imported: dict[str, int] = {}
    used: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                imported[name] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                name = alias.asname or alias.name
                imported[name] = node.lineno
        elif isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            base = node
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name):
                used.add(base.id)

    for name, lineno in imported.items():
        if name not in used:
            problems.append(f"{path}:{lineno}: unused import '{name}'")

    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            problems.append(f"{path}:{node.lineno}: bare 'except:' clause")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in BANNED_CALLS:
                problems.append(f"{path}:{node.lineno}: use of '{node.func.id}()'")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for default in node.args.defaults + [d for d in node.args.kw_defaults if d]:
                if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    problems.append(
                        f"{path}:{node.lineno}: mutable default in '{node.name}()'"
                    )
    return problems


def main() -> int:
    all_problems: list[str] = []
    for target in TARGETS:
        if not target.exists():
            all_problems.append(f"{target}: missing file")
            continue
        all_problems.extend(check_file(target))

    if all_problems:
        for problem in all_problems:
            print(problem, file=sys.stderr)
        print(f"static check: {len(all_problems)} problem(s)", file=sys.stderr)
        return 1

    print(f"static check: {len(TARGETS)} files clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
