#!/usr/bin/env bash
# macOS Docker Desktop paketi: dist/llmperf-docker-mac-arm64-<version>.tar.gz
# M2 Ultra'da: tar xzf … && ./install-docker-mac.sh up
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="${1:-1.2.0}"
NAME="llmperf-docker-mac-arm64-${VERSION}"
OUT_DIR="${ROOT}/dist"
STAGE="${OUT_DIR}/${NAME}"

rm -rf "${STAGE}"
mkdir -p "${STAGE}/scripts"

cp -a backend frontend "${STAGE}/"
cp Dockerfile docker-compose.mac.yml .dockerignore .env.example \
  install-docker-mac.sh README.md "${STAGE}/"
cp scripts/detect-slots-ports.sh "${STAGE}/scripts/"
[[ -f .gitignore ]] && cp .gitignore "${STAGE}/"

find "${STAGE}" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
find "${STAGE}" -type d -name '.venv' -exec rm -rf {} + 2>/dev/null || true
find "${STAGE}" -name '*.pyc' -delete 2>/dev/null || true

chmod +x "${STAGE}/install-docker-mac.sh" "${STAGE}/scripts/detect-slots-ports.sh"

cat > "${STAGE}/MAC-DOCKER.md" <<'EOF'
# LLMPerf — Docker Desktop (M1 / M2 Ultra / M3)

`linux/arm64` image. Host’taki Ollama’ya `host.docker.internal` ile bağlanır.

## Gereksinimler

- Docker Desktop for Mac (Apple Silicon)
- Host’ta Ollama (`ollama serve`, port 11434)

## Kurulum

```bash
tar -xzf llmperf-docker-mac-arm64-*.tar.gz
cd llmperf-docker-mac-arm64-*
./install-docker-mac.sh up
```

Panel: **http://127.0.0.1:8080**

```bash
./install-docker-mac.sh down
./install-docker-mac.sh logs
./install-docker-mac.sh rebuild
```

## Ne çalışır?

| Özellik | Docker Mac |
|---|---|
| Ollama modelleri (`/api/ps`) | Evet |
| Canlı tok/s (`/slots`) | Evet (host port izleyici) |
| Benchmark | Evet |
| Ajan süreç adları | Sınırlı (VM /proc) |
| Apple GPU util % | Hayır (etiket yok) |

Tam ajan/GPU için native paket: `llmperf-mac-arm64-*.tar.gz` + `./install-mac.sh up`
EOF

ARCHIVE="${OUT_DIR}/${NAME}.tar.gz"
tar -C "${OUT_DIR}" -czf "${ARCHIVE}" "${NAME}"
rm -rf "${STAGE}"

echo "Docker Mac paketi: ${ARCHIVE}"
ls -lh "${ARCHIVE}"
echo ""
echo "M2 Ultra'da:"
echo "  tar -xzf ${NAME}.tar.gz && cd ${NAME} && ./install-docker-mac.sh up"
