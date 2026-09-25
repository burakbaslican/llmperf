#!/usr/bin/env bash
# macOS Apple Silicon paketi: dist/llmperf-mac-arm64-<version>.tar.gz
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="${1:-1.2.1}"
NAME="llmperf-mac-arm64-${VERSION}"
OUT_DIR="${ROOT}/dist"
STAGE="${OUT_DIR}/${NAME}"

rm -rf "${STAGE}"
mkdir -p "${STAGE}"

cp -a backend frontend "${STAGE}/"
cp install-mac.sh .env.example README.md "${STAGE}/"
mkdir -p "${STAGE}/scripts"
cp scripts/detect-slots-ports.sh "${STAGE}/scripts/"
[[ -f .gitignore ]] && cp .gitignore "${STAGE}/"

# Docker dosyaları Mac native pakete gerekmez; karışıklık olmasın diye ekleme.

find "${STAGE}" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
find "${STAGE}" -type d -name '.venv' -exec rm -rf {} + 2>/dev/null || true
find "${STAGE}" -name '*.pyc' -delete 2>/dev/null || true

chmod +x "${STAGE}/install-mac.sh" "${STAGE}/scripts/detect-slots-ports.sh"

# Kısa MAC.md kurulum notu
cat > "${STAGE}/MAC.md" <<'EOF'
# LLMPerf — macOS (M1 / M2 / M2 Ultra / M3)

Docker Desktop host süreçlerini ve llama-server `/slots` portlarını göremez.
Bu paket native Python ile çalışır.

## Gereksinimler

- macOS Apple Silicon
- Python 3.10+ (`brew install python@3.12`)
- Çalışan Ollama (`ollama serve`, port 11434)
- `lsof` (sistemde zaten var)

## Kurulum

```bash
tar -xzf llmperf-mac-arm64-*.tar.gz
cd llmperf-mac-arm64-*
./install-mac.sh up
```

Panel: **http://127.0.0.1:8080**

```bash
./install-mac.sh down      # durdur
./install-mac.sh logs      # log
./install-mac.sh status    # durum
./install-mac.sh rebuild   # venv sıfırla
```

## Ortam

`.env` (ilk çalıştırmada `.env.example`'dan kopyalanır):

```
LLMPERF_OLLAMA_BASE_URL=http://127.0.0.1:11434
LLMPERF_POLL_INTERVAL_SEC=0.4
```

Port değiştirmek için: `LLMPERF_PORT=9090 ./install-mac.sh up`
EOF

ARCHIVE="${OUT_DIR}/${NAME}.tar.gz"
tar -C "${OUT_DIR}" -czf "${ARCHIVE}" "${NAME}"
rm -rf "${STAGE}"

echo "Mac paketi hazır: ${ARCHIVE}"
ls -lh "${ARCHIVE}"
echo ""
echo "M2 Ultra'da:"
echo "  tar -xzf ${NAME}.tar.gz && cd ${NAME} && ./install-mac.sh up"
