#!/usr/bin/env bash
# Host'taki llama-server dinleme portlarını bulur (macOS / Linux).
# Çıktı: virgülle ayrılmış port listesi
set -euo pipefail

ports=""

if command -v lsof >/dev/null 2>&1; then
  # llama-server veya ollama runner
  while read -r p; do
    [[ -z "$p" ]] && continue
    if [[ ",$ports," != *",$p,"* ]]; then
      ports="${ports:+$ports,}$p"
    fi
  done < <(
    lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null \
      | awk 'tolower($1) ~ /llama|ollama/ && $9 ~ /:[0-9]+$/ {
          split($9, a, ":"); print a[length(a)]
        }'
  )
fi

# ps cmdline --port fallback (macOS)
if command -v ps >/dev/null 2>&1; then
  while read -r p; do
    [[ -z "$p" ]] && continue
    if [[ ",$ports," != *",$p,"* ]]; then
      ports="${ports:+$ports,}$p"
    fi
  done < <(
    ps -ax -o command= 2>/dev/null \
      | grep -E 'llama-server' \
      | grep -v grep \
      | sed -n 's/.*--port[[:space:]]*\([0-9][0-9]*\).*/\1/p'
  )
fi

echo "$ports"
