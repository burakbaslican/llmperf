#!/usr/bin/env bash
# Dağıtım paketi oluşturur: dist/llmperf-<version>.tar.gz
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="${1:-1.2.1}"
NAME="llmperf-${VERSION}"
OUT_DIR="${ROOT}/dist"
STAGE="${OUT_DIR}/${NAME}"

rm -rf "${STAGE}"
mkdir -p "${STAGE}"

# Kaynak dosyalar (venv / cache yok)
cp -a backend frontend "${STAGE}/"
cp Dockerfile docker-compose.yml docker-compose.bridge.yml docker-compose.mac.yml \
  .dockerignore .env.example install.sh install-mac.sh install-docker-mac.sh README.md "${STAGE}/"
mkdir -p "${STAGE}/scripts"
cp scripts/detect-slots-ports.sh "${STAGE}/scripts/" 2>/dev/null || true
# .gitignore opsiyonel
[[ -f .gitignore ]] && cp .gitignore "${STAGE}/"

# venv/pyc temizliği
find "${STAGE}" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
find "${STAGE}" -type d -name '.venv' -exec rm -rf {} + 2>/dev/null || true
find "${STAGE}" -name '*.pyc' -delete 2>/dev/null || true

chmod +x "${STAGE}/install.sh" "${STAGE}/install-mac.sh" "${STAGE}/install-docker-mac.sh" 2>/dev/null || true
[[ -f "${STAGE}/scripts/detect-slots-ports.sh" ]] && chmod +x "${STAGE}/scripts/detect-slots-ports.sh"

ARCHIVE="${OUT_DIR}/${NAME}.tar.gz"
tar -C "${OUT_DIR}" -czf "${ARCHIVE}" "${NAME}"
rm -rf "${STAGE}"

echo "Paket hazır: ${ARCHIVE}"
ls -lh "${ARCHIVE}"
echo ""
echo "Başka makinede:"
echo "  tar -xzf ${NAME}.tar.gz && cd ${NAME} && ./install.sh up"
