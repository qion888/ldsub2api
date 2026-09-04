#!/usr/bin/env bash

set -Eeuo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"
RUNTIME_DIR="$ROOT_DIR/.runtime"
LOG_DIR="$RUNTIME_DIR/logs"
BACKEND_PID_FILE="$RUNTIME_DIR/backend.pid"
PACKAGE_LOCK="$FRONTEND_DIR/package-lock.json"

AUTO_INSTALL="${LDXP_AUTO_INSTALL:-0}"
SKIP_INSTALL=0
BOOTSTRAP_ONLY=0

usage() {
  cat <<'EOF'
Usage: ./start.sh [options]

Options:
  --auto-install    Install missing system/runtime dependencies when possible.
  --skip-install    Do not install system or project dependencies.
  --bootstrap-only  Check and install dependencies, then exit without starting services.
  -h, --help        Show this help.

Environment:
  LDXP_AUTO_INSTALL=1  Same as --auto-install.
  LDXP_PYTHON          Python executable to use.
  LDXP_NODE            Node.js executable to use.
EOF
}

die() {
  printf 'Startup failed: %s\n' "$*" >&2
  exit 1
}

step() {
  printf '\n==> %s\n' "$*"
}

for argument in "$@"; do
  case "$argument" in
    --auto-install) AUTO_INSTALL=1 ;;
    --skip-install) SKIP_INSTALL=1 ;;
    --bootstrap-only) BOOTSTRAP_ONLY=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $argument (use --help for usage)" ;;
  esac
done

if [[ "$AUTO_INSTALL" == "1" && "$SKIP_INSTALL" == "1" ]]; then
  die '--auto-install and --skip-install cannot be used together'
fi

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

run_privileged() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  elif command_exists sudo; then
    sudo "$@"
  else
    die "Administrator privileges are required for: $* (install sudo or run as root)"
  fi
}

version_is_supported_python() {
  local executable="$1"
  "$executable" -c 'import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] < (3, 14) else 1)' >/dev/null 2>&1
}

version_is_supported_node() {
  local executable="$1"
  "$executable" -e 'const [major, minor] = process.versions.node.split(".").map(Number); process.exit((major === 20 && minor >= 19) || major >= 22 ? 0 : 1)' >/dev/null 2>&1
}

resolve_python() {
  local candidate
  local candidates=()
  [[ -n "${LDXP_PYTHON:-}" ]] && candidates+=("$LDXP_PYTHON")
  if [[ -x "$RUNTIME_DIR/python/bin/python" ]]; then
    candidates+=("$RUNTIME_DIR/python/bin/python")
  fi
  candidates+=(python3 python)
  for candidate in "${candidates[@]}"; do
    if [[ "$candidate" == */* && ! -x "$candidate" ]]; then
      continue
    fi
    if command_exists "$candidate" || [[ -x "$candidate" ]]; then
      if version_is_supported_python "$candidate"; then
        command -v "$candidate" 2>/dev/null || printf '%s\n' "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

resolve_node() {
  local candidate
  local candidates=()
  [[ -n "${LDXP_NODE:-}" ]] && candidates+=("$LDXP_NODE")
  candidates+=(node)
  for candidate in "${candidates[@]}"; do
    if [[ "$candidate" == */* && ! -x "$candidate" ]]; then
      continue
    fi
    if command_exists "$candidate" || [[ -x "$candidate" ]]; then
      if version_is_supported_node "$candidate"; then
        command -v "$candidate" 2>/dev/null || printf '%s\n' "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

install_system_runtime() {
  local os_name
  os_name="$(uname -s)"
  if [[ "$os_name" == "Darwin" ]] && command_exists brew; then
    step 'Installing Python and Node.js with Homebrew'
    brew install python@3.12 node
    return
  fi

  if [[ "$os_name" == "Linux" ]]; then
    if command_exists apt-get; then
      step 'Installing Python, venv, pip, Node.js and npm with apt'
      run_privileged apt-get update
      run_privileged apt-get install -y python3 python3-venv python3-pip nodejs npm
      return
    fi
    if command_exists dnf; then
      step 'Installing Python, pip, Node.js and npm with dnf'
      run_privileged dnf install -y python3 python3-pip nodejs npm
      return
    fi
    if command_exists pacman; then
      step 'Installing Python, pip, Node.js and npm with pacman'
      run_privileged pacman -Sy --needed --noconfirm python python-pip nodejs npm
      return
    fi
    if command_exists zypper; then
      step 'Installing Python, pip, Node.js and npm with zypper'
      run_privileged zypper --non-interactive install python311 python311-pip nodejs npm
      return
    fi
  fi

  if [[ "$os_name" == "Darwin" ]]; then
    die 'Homebrew is required for automatic installation. Install it from https://brew.sh, then rerun with --auto-install.'
  fi
  die 'No supported package manager found. Install Python 3.10-3.13 and Node.js 20.19+ or 22+ manually, then rerun.'
}

install_linux_node22() {
  local installer
  command_exists curl || die 'curl is required to install Node.js 22 on Linux.'
  installer="$(mktemp "${TMPDIR:-/tmp}/ldxp-node-setup.XXXXXX")"
  trap 'rm -f "$installer"' RETURN
  curl -fsSL https://deb.nodesource.com/setup_22.x -o "$installer" || die 'Could not download the Node.js 22 package setup.'
  run_privileged bash "$installer"
  run_privileged apt-get install -y nodejs
  rm -f "$installer"
  trap - RETURN
}

ensure_python() {
  local executable
  executable="$(resolve_python || true)"
  if [[ -n "$executable" ]]; then
    printf '%s\n' "$executable"
    return 0
  fi
  [[ "$SKIP_INSTALL" -eq 1 ]] && die 'Python 3.10-3.13 was not found. Install it or remove --skip-install.'
  [[ "$AUTO_INSTALL" -eq 1 ]] || die 'Python 3.10-3.13 was not found. Rerun with --auto-install or install it manually.'
  install_system_runtime >&2
  executable="$(resolve_python || true)"
  [[ -n "$executable" ]] || die 'Python installation completed, but a supported interpreter was not found.'
  printf '%s\n' "$executable"
}

ensure_node() {
  local executable
  executable="$(resolve_node || true)"
  if [[ -n "$executable" ]]; then
    printf '%s\n' "$executable"
    return 0
  fi
  [[ "$SKIP_INSTALL" -eq 1 ]] && die 'Node.js 20.19+ or 22+ was not found. Install it or remove --skip-install.'
  [[ "$AUTO_INSTALL" -eq 1 ]] || die 'Node.js 20.19+ or 22+ was not found. Rerun with --auto-install or install it manually.'
  install_system_runtime >&2
  if [[ "$(uname -s)" == "Linux" ]] && command_exists apt-get && ! resolve_node >/dev/null 2>&1; then
    install_linux_node22 >&2
  fi
  executable="$(resolve_node || true)"
  [[ -n "$executable" ]] || die 'Node.js installation completed, but a supported runtime was not found.'
  printf '%s\n' "$executable"
}

ensure_npm() {
  local npm_command
  npm_command="$(command -v npm 2>/dev/null || true)"
  [[ -n "$npm_command" ]] || die 'npm was not found. Install it together with Node.js.'
  printf '%s\n' "$npm_command"
}

ensure_backend_dependencies() {
  local python_executable="$1"
  local runtime_python="$RUNTIME_DIR/python/bin/python"
  if "$python_executable" -c 'import selenium, ddddocr, numpy, onnxruntime' >/dev/null 2>&1; then
    return 0
  fi
  [[ "$SKIP_INSTALL" -eq 1 ]] && printf 'Warning: OCR/browser dependencies are missing; manual captcha entry remains available.\n' >&2 && return 0
  if [[ -x "$runtime_python" ]]; then
    python_executable="$runtime_python"
  else
    step 'Creating a project-local Python environment'
    "$python_executable" -m venv "$RUNTIME_DIR/python" || die 'Could not create the project-local Python environment.'
    [[ -x "$runtime_python" ]] || die 'The project-local Python environment was created without a usable interpreter.'
    python_executable="$runtime_python"
  fi
  step 'Installing backend browser verification and OCR dependencies'
  if ! "$python_executable" -m pip --version >/dev/null 2>&1; then
    "$python_executable" -m ensurepip --upgrade >/dev/null 2>&1 || die 'pip is unavailable for the selected Python interpreter.'
  fi
  "$python_executable" -m pip install -r "$BACKEND_DIR/requirements.txt"
  if ! "$python_executable" -c 'import selenium, ddddocr, numpy, onnxruntime' >/dev/null 2>&1; then
    printf 'Warning: some OCR/browser dependencies could not be loaded; manual captcha entry remains available.\n' >&2
  fi
  PYTHON_EXECUTABLE="$python_executable"
}

ensure_frontend_dependencies() {
  local npm_command="$1"
  [[ -x "$FRONTEND_DIR/node_modules/.bin/vite" ]] && return 0
  [[ "$SKIP_INSTALL" -eq 1 ]] && die 'Frontend dependencies are missing. Run npm --prefix frontend ci, or remove --skip-install.'
  step 'Installing frontend dependencies'
  if [[ -f "$PACKAGE_LOCK" ]]; then
    "$npm_command" --prefix "$FRONTEND_DIR" ci
  else
    "$npm_command" --prefix "$FRONTEND_DIR" install
  fi
  [[ -x "$FRONTEND_DIR/node_modules/.bin/vite" ]] || die 'Frontend dependency installation did not provide Vite.'
}

stop_stale_processes() {
  local pattern
  for pattern in "$BACKEND_DIR/main.py" "$FRONTEND_DIR/node_modules/.bin/vite"; do
    if command_exists pgrep; then
      while read -r pid; do
        [[ -z "$pid" || "$pid" -eq "$$" ]] && continue
        kill "$pid" >/dev/null 2>&1 || true
      done < <(pgrep -f -- "$pattern" || true)
    fi
  done
  rm -f "$BACKEND_PID_FILE"
}

port_is_free() {
  local port="$1"
  if command_exists lsof && lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    return 1
  fi
  if command_exists ss && ss -ltn "sport = :$port" 2>/dev/null | tail -n +2 | grep -q .; then
    return 1
  fi
  if command_exists netstat && netstat -an 2>/dev/null | grep -E "[.:]$port[[:space:]].*LISTEN" >/dev/null; then
    return 1
  fi
  if [[ -n "${PYTHON_EXECUTABLE:-}" ]]; then
    "$PYTHON_EXECUTABLE" - "$port" <<'PY' >/dev/null 2>&1
import socket
import sys

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", int(sys.argv[1])))
    except OSError:
        raise SystemExit(1)
PY
    return $?
  fi
  return 0
}

find_free_port() {
  local start="$1"
  local port
  for ((port=start; port<=start+50; port++)); do
    if port_is_free "$port"; then
      printf '%s\n' "$port"
      return 0
    fi
  done
  die "No available local port found after $start"
}

wait_backend() {
  local python_executable="$1"
  local port="$2"
  local attempt
  for ((attempt=1; attempt<=60; attempt++)); do
    if "$python_executable" - "$port" <<'PY' >/dev/null 2>&1
import json
import sys
from urllib.request import urlopen

port = sys.argv[1]
with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
    payload = json.load(response)
required = ("ok", "sub2api_accounts", "sub2api_card_import_history_delete", "order_complaint_submit")
raise SystemExit(0 if all(payload.get(key) for key in required) else 1)
PY
    then
      return 0
    fi
    if [[ -n "${BACKEND_PID:-}" ]] && ! kill -0 "$BACKEND_PID" >/dev/null 2>&1; then
      die 'Backend exited before becoming ready. Check .runtime/logs/backend.log.'
    fi
    sleep 0.25
  done
  die 'Backend readiness check failed. Check .runtime/logs/backend.log.'
}

cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM
  if [[ -n "${BACKEND_PID:-}" ]] && kill -0 "$BACKEND_PID" >/dev/null 2>&1; then
    kill "$BACKEND_PID" >/dev/null 2>&1 || true
    wait "$BACKEND_PID" >/dev/null 2>&1 || true
  fi
  rm -f "$BACKEND_PID_FILE"
  exit "$exit_code"
}

mkdir -p "$RUNTIME_DIR" "$LOG_DIR"
PYTHON_EXECUTABLE="$(ensure_python)"
NODE_EXECUTABLE="$(ensure_node)"
NPM_COMMAND="$(ensure_npm)"
ensure_frontend_dependencies "$NPM_COMMAND"
ensure_backend_dependencies "$PYTHON_EXECUTABLE"

printf 'Python: %s\n' "$PYTHON_EXECUTABLE"
printf 'Node.js: %s\n' "$NODE_EXECUTABLE"
printf 'npm: %s\n' "$NPM_COMMAND"

if [[ "$BOOTSTRAP_ONLY" -eq 1 ]]; then
  printf 'Dependency check completed. Run ./start.sh to start services.\n'
  exit 0
fi

stop_stale_processes
BACKEND_PORT="$(find_free_port 8000)"
FRONTEND_PORT="$(find_free_port 5173)"
export LDXP_PORT="$BACKEND_PORT"
export LDXP_FRONTEND_URL="http://127.0.0.1:$FRONTEND_PORT/"
export LDXP_API_TARGET="http://127.0.0.1:$BACKEND_PORT"

printf 'Starting backend at http://127.0.0.1:%s\n' "$BACKEND_PORT"
"$PYTHON_EXECUTABLE" "$BACKEND_DIR/main.py" >"$LOG_DIR/backend.log" 2>&1 &
BACKEND_PID=$!
printf '%s\n' "$BACKEND_PID" >"$BACKEND_PID_FILE"
trap cleanup EXIT INT TERM

wait_backend "$PYTHON_EXECUTABLE" "$BACKEND_PORT"

printf '\n========================================\n'
printf 'LDXP services started\n'
printf 'Frontend:      http://127.0.0.1:%s\n' "$FRONTEND_PORT"
printf 'Backend API:   http://127.0.0.1:%s\n' "$BACKEND_PORT"
printf 'Health check:  http://127.0.0.1:%s/api/health\n' "$BACKEND_PORT"
printf 'Ports:         frontend=%s  backend=%s\n' "$FRONTEND_PORT" "$BACKEND_PORT"
printf 'Press Ctrl+C to stop both services.\n'
printf '========================================\n'

"$NPM_COMMAND" --prefix "$FRONTEND_DIR" run dev -- --host 127.0.0.1 --port "$FRONTEND_PORT" --strictPort
