#!/usr/bin/env bash
# LLMPerf — macOS Docker Desktop (Apple Silicon / M2 Ultra)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PORTS_FILE="${ROOT}/.llmperf-slots-ports"
OBS_FILE="${ROOT}/.llmperf-host-obs"
GPU_FILE="${ROOT}/.llmperf-gpu.json"
WATCH_PID_FILE="${ROOT}/.llmperf-ports-watch.pid"
GPU_WATCH_PID_FILE="${ROOT}/.llmperf-gpu-watch.pid"
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
  # Dosyaları bir kez oluştur; sonra yalnızca inplace yaz (Docker bind inode)
  [[ -f "$PORTS_FILE" ]] || : >"$PORTS_FILE"
  [[ -f "$OBS_FILE" ]] || printf '%s\n' '{}' >"$OBS_FILE"
  [[ -f "$GPU_FILE" ]] || printf '%s\n' '[]' >"$GPU_FILE"
}

write_inplace() {
  # Docker Desktop Mac: mv/replace inode değiştirir → volume bağını koparır
  local dest="$1"
  local src="$2"
  if [[ -f "$dest" ]]; then
    cat "$src" >"$dest"
  else
    cp "$src" "$dest"
  fi
  rm -f "$src"
}

write_ports() {
  local ports
  ports="$("${ROOT}/scripts/detect-slots-ports.sh" 2>/dev/null || true)"
  if [[ -f "$PORTS_FILE" ]]; then
    printf '%s\n' "$ports" >"$PORTS_FILE"
  else
    printf '%s\n' "$ports" >"$PORTS_FILE"
  fi
  chmod +x "${ROOT}/scripts/detect-host-obs.py" "${ROOT}/scripts/sample-mactop-gpu.py" 2>/dev/null || true
  if command -v python3 >/dev/null 2>&1; then
    if LLMPERF_GPU_CACHE="$GPU_FILE" \
       LLMPERF_CLIENT_CACHE="${ROOT}/.llmperf-clients-cache.json" \
       python3 "${ROOT}/scripts/detect-host-obs.py" >"${OBS_FILE}.tmp" 2>/dev/null; then
      write_inplace "$OBS_FILE" "${OBS_FILE}.tmp"
    else
      rm -f "${OBS_FILE}.tmp"
      printf '%s\n' '{}' >"$OBS_FILE"
    fi
  fi
}

cleanup_stale_watches() {
  # Eski kurulumlardan kalan izleyicileri temizle (dosya kilidi / donma)
  pkill -f "${ROOT}/scripts/sample-mactop-gpu.py" 2>/dev/null || true
  pkill -f "${ROOT}/scripts/detect-host-obs.py" 2>/dev/null || true
  # Bu dizine ait eski install watch döngüleri
  if [[ -f "$WATCH_PID_FILE" ]]; then
    kill "$(cat "$WATCH_PID_FILE")" 2>/dev/null || true
    rm -f "$WATCH_PID_FILE"
  fi
  if [[ -f "$GPU_WATCH_PID_FILE" ]]; then
    kill "$(cat "$GPU_WATCH_PID_FILE")" 2>/dev/null || true
    pkill -P "$(cat "$GPU_WATCH_PID_FILE" 2>/dev/null)" 2>/dev/null || true
    rm -f "$GPU_WATCH_PID_FILE"
  fi
}

start_gpu_watch() {
  stop_gpu_watch
  chmod +x "${ROOT}/scripts/sample-mactop-gpu.py" 2>/dev/null || true
  (
    while true; do
      if command -v python3 >/dev/null 2>&1; then
        python3 "${ROOT}/scripts/sample-mactop-gpu.py" >"${GPU_FILE}.tmp" 2>/dev/null \
          && write_inplace "$GPU_FILE" "${GPU_FILE}.tmp" \
          || printf '%s\n' '[]' >"$GPU_FILE"
        # GPU'yu host-obs içine de yaz (container gecikmesin) — inplace
        if [[ -f "$OBS_FILE" ]] && command -v python3 >/dev/null 2>&1; then
          python3 - "$OBS_FILE" "$GPU_FILE" <<'PY' 2>/dev/null || true
import json, sys
obs_p, gpu_p = sys.argv[1], sys.argv[2]
try:
    with open(obs_p, encoding="utf-8") as f:
        obs = json.loads(f.read() or "{}")
except Exception:
    obs = {}
try:
    with open(gpu_p, encoding="utf-8") as f:
        gpus = json.loads(f.read() or "[]")
except Exception:
    gpus = []
if not isinstance(obs, dict):
    obs = {}
obs["gpus"] = gpus if isinstance(gpus, list) else []
# Truncate in place — keep Docker bind-mount inode
data = json.dumps(obs, ensure_ascii=False)
with open(obs_p, "w", encoding="utf-8") as f:
    f.write(data)
    f.write("\n")
PY
        fi
      fi
      sleep 2
    done
  ) &
  echo $! >"$GPU_WATCH_PID_FILE"
}

stop_gpu_watch() {
  if [[ -f "$GPU_WATCH_PID_FILE" ]]; then
    local pid
    pid="$(cat "$GPU_WATCH_PID_FILE")"
    kill "$pid" 2>/dev/null || true
    pkill -P "$pid" 2>/dev/null || true
    rm -f "$GPU_WATCH_PID_FILE"
  fi
}

start_ports_watch() {
  cleanup_stale_watches
  stop_ports_watch
  start_gpu_watch
  write_ports
  (
    while true; do
      write_ports
      sleep 1
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
  stop_gpu_watch
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
