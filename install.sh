#!/usr/bin/env bash
# LLMPerf kurulum / başlatma scripti
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

COMPOSE="docker compose"
if ! docker compose version >/dev/null 2>&1; then
  if command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
  else
    echo "Hata: Docker Compose bulunamadı. Docker'ı kurun: https://docs.docker.com/get-docker/"
    exit 1
  fi
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Hata: docker komutu yok."
  exit 1
fi

MODE="${1:-up}"
FILE_ARGS=(-f docker-compose.yml)

# macOS / Docker Desktop için: ./install.sh up bridge
if [[ "${2:-}" == "bridge" ]] || [[ "${MODE}" == "bridge" ]]; then
  FILE_ARGS=(-f docker-compose.bridge.yml)
  if [[ "${MODE}" == "bridge" ]]; then
    MODE="up"
  fi
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo ".env oluşturuldu (.env.example'dan)."
fi

case "$MODE" in
  up|start)
    echo "LLMPerf derleniyor ve başlatılıyor..."
    $COMPOSE "${FILE_ARGS[@]}" up -d --build
    echo ""
    echo "Panel: http://127.0.0.1:8080"
    echo "Durdurmak: $0 down"
    ;;
  down|stop)
    $COMPOSE "${FILE_ARGS[@]}" down
    echo "LLMPerf durduruldu."
    ;;
  logs)
    $COMPOSE "${FILE_ARGS[@]}" logs -f
    ;;
  status|ps)
    $COMPOSE "${FILE_ARGS[@]}" ps
    ;;
  rebuild)
    $COMPOSE "${FILE_ARGS[@]}" up -d --build --force-recreate
    echo "Panel: http://127.0.0.1:8080"
    ;;
  *)
    echo "Kullanım: $0 {up|down|logs|status|rebuild} [bridge]"
    echo "  up       — derle ve başlat (Linux: host network)"
    echo "  up bridge — macOS/Desktop için bridge ağ"
    echo "  down     — durdur"
    echo "  logs     — logları izle"
    exit 1
    ;;
esac
