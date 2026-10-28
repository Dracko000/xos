#!/usr/bin/env bash
# xOS CI check — runs the checks that must pass on every change.
#
# Usage: ./tools/ci_check.sh
#
# Exits non-zero on the first failure. No network access is required; the
# Ollama API is emulated by tools/mock_ollama.py.
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

log()  { printf "\033[1;36m[CI]\033[0m %s\n" "$*"; }
die()  { printf "\033[1;31m[CI] ERROR:\033[0m %s\n" "$*" >&2; exit 1; }

# 1. Shell syntax. NOTE: `bash -n a b` only checks `a`, so loop explicitly.
log "Running shell syntax checks..."
shell_files=(build.sh tools/provision.sh tools/ci_check.sh config/xos-seal.sh)
for f in "${shell_files[@]}"; do
    [[ -f "$f" ]] || die "missing shell file: $f"
    bash -n "$f" || die "syntax error in $f"
done

# 2. Python syntax and byte-compilation.
log "Running Python syntax checks..."
python_files=(
    shell/xos_shell.py
    tools/mock_ollama.py
    tools/static_check.py
    tests/test_shell_logic.py
    tests/test_regressions.py
    tests/test_ollama_client.py
    tests/test_tui_e2e.py
)
for f in "${python_files[@]}"; do
    [[ -f "$f" ]] || die "missing Python file: $f"
    python3 -m py_compile "$f" || die "syntax error in $f"
done

# 3. Static analysis (stdlib only, no pip dependency).
log "Running static analysis..."
python3 tools/static_check.py || die "static analysis failed"

# 3b. Headless render smoke test (no TTY required).
log "Running headless render self-test..."
python3 shell/xos_shell.py --selftest >/dev/null || die "shell self-test failed"

# 4. Unit, regression and Ollama client tests (no network, no TTY).
log "Running shell logic tests..."
PYTHONPATH=. python3 tests/test_shell_logic.py || die "logic tests failed"

log "Running regression tests..."
PYTHONPATH=. python3 tests/test_regressions.py || die "regression tests failed"

log "Running Ollama client tests..."
PYTHONPATH=. python3 tests/test_ollama_client.py || die "Ollama client tests failed"

# 5. End-to-end TUI tests against the mock Ollama API.
log "Running E2E TUI tests..."
MOCK_PORT=11435 python3 tools/mock_ollama.py >/dev/null 2>&1 &
mock_pid=$!
cleanup() { kill "$mock_pid" 2>/dev/null || true; wait "$mock_pid" 2>/dev/null || true; }
trap cleanup EXIT

for _ in $(seq 1 50); do
    if python3 - <<'PY' 2>/dev/null
import socket, sys
s = socket.socket()
s.settimeout(0.2)
try:
    s.connect(("127.0.0.1", 11435))
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
    then break; fi
    sleep 0.1
done

PYTHONPATH=. python3 tests/test_tui_e2e.py || die "E2E tests failed"

log "All CI checks passed."
