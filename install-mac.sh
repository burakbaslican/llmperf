#!/usr/bin/env bash
# LLMPerf — macOS (Apple Silicon / M2 Ultra) native kurulum
# Docker VM host süreçlerini göremez; native Python + slots port izleyici kullanılır.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VENV="${ROOT}/.venv"
PID_FILE="${ROOT}/.llmperf.pid"
LOG_FILE="${ROOT}/.llmperf.log"
PORTS_FILE="${ROOT}/.llmperf-slots-ports"
WATCH_PID_FILE="${ROOT}/.llmperf-ports-watch.pid"
HOST="${LLMPERF_HOST:-127.0.0.1}"
PORT="${LLMPERF_PORT:-8080}"

die() { echo "Hata: $*" >&2; exit 1; }

require_macos() {
  [[ "$(uname -s)" == "Darwin" ]] || die "Bu script yalnızca macOS içindir. Linux için ./install.sh kullanın."
}

find_python() {
  local candidates=(python3.13 python3.12 python3.11 python3.10 python3)
  local p
  for p in "${candidates[@]}"; do
    if command -v "$p" >/dev/null 2>&1; then
      if "$p" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
        echo "$p"
        return 0
      fi
    fi
  done
  return 1
}

ensure_env() {
  if [[ ! -f .env ]]; then
    cp .env.example .env
    echo ".env oluşturuldu (.env.example'dan)."
  fi
  set -a
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    export "$line"
  done < .env
  set +a
  HOST="${LLMPERF_HOST:-$HOST}"
  PORT="${LLMPERF_PORT:-$PORT}"
}

setup_venv() {
  local py
  py="$(find_python)" || die "Python 3.10+ gerekli. Örn: brew install python@3.12"
  echo "Python: $py ($("$py" --version 2>&1))"

  if [[ ! -d "$VENV" ]]; then
    echo "venv oluşturuluyor..."
    "$py" -m venv "$VENV"
  fi
  # shellcheck disable=SC1091
  source "${VENV}/bin/activate"
  pip install -q --upgrade pip
  pip install -q -r backend/requirements.txt
  echo "Bağımlılıklar hazır."
}

write_ports() {
  local ports
  chmod +x "${ROOT}/scripts/detect-slots-ports.sh" 2>/dev/null || true
  ports="$("${ROOT}/scripts/detect-slots-ports.sh" 2>/dev/null || true)"
  echo "$ports" >"$PORTS_FILE"
}

start_ports_watch() {
  stop_ports_watch
  touch "$PORTS_FILE"
  write_ports
  (
    while true; do
      write_ports
      sleep 2
    done
  ) &
  echo $! >"$WATCH_PID_FILE"
}

stop_ports_watch() {
  if [[ -f "$WATCH_PID_FILE" ]]; then
    kill "$(cat "$WATCH_PID_FILE")" 2>/dev/null || true
    rm -f "$WATCH_PID_FILE"
  fi
}

is_running() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE")"
    if kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    rm -f "$PID_FILE"
  fi
  return 1
}

cmd_up() {
  require_macos
  ensure_env
  setup_venv
  start_ports_watch

  if is_running; then
    echo "LLMPerf zaten çalışıyor (pid $(cat "$PID_FILE"))."
    echo "Panel: http://${HOST}:${PORT}"
    echo "Slots ports: $(cat "$PORTS_FILE" 2>/dev/null || echo '-')"
    return 0
  fi

  # shellcheck disable=SC1091
  source "${VENV}/bin/activate"

  echo "LLMPerf başlatılıyor (native, Apple Silicon)..."
  nohup env \
    LLMPERF_OLLAMA_BASE_URL="${LLMPERF_OLLAMA_BASE_URL:-http://127.0.0.1:11434}" \
    LLMPERF_POLL_INTERVAL_SEC="${LLMPERF_POLL_INTERVAL_SEC:-0.4}" \
    LLMPERF_SLOTS_HOST="${LLMPERF_SLOTS_HOST:-127.0.0.1}" \
    LLMPERF_SLOTS_PORTS_FILE="${PORTS_FILE}" \
    LLMPERF_PROC_ROOT="" \
    "${VENV}/bin/uvicorn" backend.main:app \
      --host "$HOST" \
      --port "$PORT" \
      >"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  sleep 0.8
  if is_running; then
    echo ""
    echo "Panel: http://${HOST}:${PORT}"
    echo "Log:   $LOG_FILE"
    echo "Slots: $(cat "$PORTS_FILE" 2>/dev/null || echo '-')"
    echo "Durdurmak: $0 down"
  else
    stop_ports_watch
    echo "Başlatılamadı. Son log:"
    tail -n 40 "$LOG_FILE" 2>/dev/null || true
    exit 1
  fi
}

cmd_down() {
  stop_ports_watch
  if ! is_running; then
    echo "LLMPerf çalışmıyor."
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  kill "$pid" 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.3
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  echo "LLMPerf durduruldu."
}

cmd_logs() {
  touch "$LOG_FILE"
  tail -n 80 -f "$LOG_FILE"
}

cmd_status() {
  if is_running; then
    echo "çalışıyor  pid=$(cat "$PID_FILE")  http://${HOST}:${PORT}"
    echo "slots ports: $(cat "$PORTS_FILE" 2>/dev/null || echo '-')"
  else
    echo "durdu"
  fi
}

cmd_rebuild() {
  require_macos
  cmd_down
  rm -rf "$VENV"
  cmd_up
}

MODE="${1:-up}"
case "$MODE" in
  up|start)     cmd_up ;;
  down|stop)    cmd_down ;;
  logs)         cmd_logs ;;
  status|ps)    ensure_env; cmd_status ;;
  rebuild)      cmd_rebuild ;;
  *)
    echo "Kullanım: $0 {up|down|logs|status|rebuild}"
    exit 1
    ;;
esac
