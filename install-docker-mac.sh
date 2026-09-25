#!/usr/bin/env bash
# LLMPerf — macOS Docker Desktop (Apple Silicon / M2 Ultra)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PORTS_FILE="${ROOT}/.llmperf-slots-ports"
WATCH_PID_FILE="${ROOT}/.llmperf-ports-watch.pid"
COMPOSE_FILE="docker-compose.mac.yml"

die() { echo "Hata: $*" >&2; exit 1; }

require_mac() {
  [[ "$(uname -s)" == "Darwin" ]] || die "Bu script macOS (Docker Desktop) içindir."
}

require_docker() {
  command -v docker >/dev/null 2>&1 || die "docker yok. Docker Desktop kurun: https://www.docker.com/products/docker-desktop/"
  if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
  else
    die "Docker Compose bulunamadı."
  fi
  docker info >/dev/null 2>&1 || die "Docker çalışmıyor. Docker Desktop'ı açın."
}

ensure_env() {
  if [[ ! -f .env ]]; then
    cp .env.example .env
    # Mac Docker varsayılanları
    {
      echo ""
      echo "# Mac Docker Desktop"
      echo "LLMPERF_OLLAMA_BASE_URL=http://host.docker.internal:11434"
      echo "LLMPERF_SLOTS_HOST=host.docker.internal"
    } >> .env
    echo ".env oluşturuldu (Mac Docker ayarlarıyla)."
  fi
  touch "$PORTS_FILE"
}

write_ports() {
  local ports
  ports="$("${ROOT}/scripts/detect-slots-ports.sh" 2>/dev/null || true)"
  echo "$ports" >"$PORTS_FILE"
}

start_ports_watch() {
  stop_ports_watch
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
    local pid
    pid="$(cat "$WATCH_PID_FILE")"
    kill "$pid" 2>/dev/null || true
    rm -f "$WATCH_PID_FILE"
  fi
}

cmd_up() {
  require_mac
  require_docker
  ensure_env
  chmod +x "${ROOT}/scripts/detect-slots-ports.sh" 2>/dev/null || true
  start_ports_watch

  echo "Apple Silicon (linux/arm64) image derleniyor..."
  DOCKER_DEFAULT_PLATFORM=linux/arm64 \
    "${COMPOSE[@]}" -f "$COMPOSE_FILE" build --pull

  echo "Container başlatılıyor..."
  "${COMPOSE[@]}" -f "$COMPOSE_FILE" up -d

  local port
  port="$(grep -E '^LLMPERF_PORT=' .env 2>/dev/null | cut -d= -f2 || true)"
  port="${port:-8080}"

  echo ""
  echo "Panel: http://127.0.0.1:${port}"
  echo "Image: llmperf:mac-arm64"
  echo "Slots ports: $(cat "$PORTS_FILE" 2>/dev/null || echo '-')"
  echo "Durdurmak: $0 down"
  echo ""
  echo "Not: Ollama Mac'te çalışıyor olmalı (ollama serve)."
  echo "     Ollama yalnızca 127.0.0.1 dinliyorsa Docker erişebilir (Desktop)."
}

cmd_down() {
  require_docker
  stop_ports_watch
  "${COMPOSE[@]}" -f "$COMPOSE_FILE" down 2>/dev/null || true
  echo "LLMPerf (Docker Mac) durduruldu."
}

cmd_logs() {
  require_docker
  "${COMPOSE[@]}" -f "$COMPOSE_FILE" logs -f
}

cmd_status() {
  require_docker
  "${COMPOSE[@]}" -f "$COMPOSE_FILE" ps
  echo "slots ports: $(cat "$PORTS_FILE" 2>/dev/null || echo '-')"
}

cmd_rebuild() {
  require_mac
  require_docker
  ensure_env
  start_ports_watch
  DOCKER_DEFAULT_PLATFORM=linux/arm64 \
    "${COMPOSE[@]}" -f "$COMPOSE_FILE" up -d --build --force-recreate --pull always
  echo "Panel: http://127.0.0.1:8080"
}

MODE="${1:-up}"
case "$MODE" in
  up|start)   cmd_up ;;
  down|stop)  cmd_down ;;
  logs)       cmd_logs ;;
  status|ps)  cmd_status ;;
  rebuild)    cmd_rebuild ;;
  *)
    echo "Kullanım: $0 {up|down|logs|status|rebuild}"
    echo "  up       — arm64 image derle + Docker Desktop'ta başlat"
    echo "  down     — durdur"
    echo "  logs     — log"
    echo "  status   — durum"
    echo "  rebuild  — sıfırdan derle"
    exit 1
    ;;
esac
