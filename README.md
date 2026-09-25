# LLMPerf

Yerel Ollama performansını (token/s, ajanlar, GPU) canlı izleyen web paneli.

## GitHub Release’ten kurulum

Repo: https://github.com/burakbaslican/llmperf/releases/tag/v1.2.1  
(Private repo → `gh auth login` gerekli — public ise doğrudan indirilebilir.)

### Mac M2 Ultra — Docker Desktop (önerilen)

```bash
gh release download v1.2.1 -R burakbaslican/llmperf -p 'llmperf-docker-mac-arm64-1.2.1.tar.gz' \
  && tar -xzf llmperf-docker-mac-arm64-1.2.1.tar.gz \
  && cd llmperf-docker-mac-arm64-1.2.1 \
  && ./install-docker-mac.sh up
```

### Mac M2 Ultra — native Python

```bash
gh release download v1.2.1 -R burakbaslican/llmperf -p 'llmperf-mac-arm64-1.2.1.tar.gz' \
  && tar -xzf llmperf-mac-arm64-1.2.1.tar.gz \
  && cd llmperf-mac-arm64-1.2.1 \
  && ./install-mac.sh up
```

### Linux — Docker

```bash
gh release download v1.2.1 -R burakbaslican/llmperf -p 'llmperf-1.2.1.tar.gz' \
  && tar -xzf llmperf-1.2.1.tar.gz \
  && cd llmperf-1.2.1 \
  && ./install.sh up
```

Panel: **http://127.0.0.1:8080** · durdurmak: `./install*.sh down`

## Hızlı kurulum (yerel paket)

**Gereksinimler:** Docker + Docker Compose, host’ta çalışan Ollama (`11434`).

```bash
tar -xzf llmperf-*.tar.gz
cd llmperf-*
./install.sh up
```

Panel: **http://127.0.0.1:8080**

Durdurmak:

```bash
./install.sh down
```

### macOS Docker Desktop (M1 / M2 Ultra / M3)

`linux/arm64` image — Docker Desktop üzerinde:

```bash
tar -xzf llmperf-docker-mac-arm64-*.tar.gz
cd llmperf-docker-mac-arm64-*
./install-docker-mac.sh up
```

veya kaynak ağacında:

```bash
./install-docker-mac.sh up
```

Panel: **http://127.0.0.1:8080** · image: `llmperf:mac-arm64`

Host’taki Ollama’ya `host.docker.internal` ile bağlanır; `/slots` portları host izleyici ile güncellenir.

### macOS native (Docker’sız)

Ajan süreçleri ve daha doğrudan gözlem için:

```bash
tar -xzf llmperf-mac-arm64-*.tar.gz
cd llmperf-mac-arm64-*
./install-mac.sh up
```

### macOS / eski bridge

```bash
./install.sh up bridge
```

### Ortam

`.env.example` → `.env` (install otomatik kopyalar):

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `LLMPERF_OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Ollama API |
| `LLMPERF_POLL_INTERVAL_SEC` | `0.4` | Örnekleme aralığı |

## Manuel Docker

```bash
docker compose up -d --build
```

Linux varsayılanı: `network_mode: host` + `pid: host` (runner `/slots`, süreç ve GPU gözlemi için).

## Docker’sız geliştirme

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
LLMPERF_OLLAMA_BASE_URL=http://127.0.0.1:11434 \
  uvicorn backend.main:app --host 0.0.0.0 --port 8080
```

## Paket üretimi

```bash
./scripts/package.sh 1.2.1
# → dist/llmperf-1.2.1.tar.gz                 (Linux / Docker)

./scripts/package-docker-mac.sh 1.2.1
# → dist/llmperf-docker-mac-arm64-1.2.1.tar.gz (Mac Docker Desktop)

./scripts/package-mac.sh 1.2.1
# → dist/llmperf-mac-arm64-1.2.1.tar.gz        (Mac native Python)
```

## Ne izler?

- Bellekteki modeller (`/api/ps`)
- Canlı tok/s (`llama-server` `/slots`)
- Bağlı ajan süreçleri
- GPU kullanımı (AMD/Intel sysfs, nvidia-smi veya Apple Silicon etiketi)
- Kontrollü benchmark ölçümü
