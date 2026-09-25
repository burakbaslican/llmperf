#!/usr/bin/env bash
# Host'taki Ollama runner / llama-server dinleme portlarını bulur (macOS / Linux).
# Çıktı: virgülle ayrılmış port listesi (11434 hariç)
set -euo pipefail

OLLAMA_API_PORT="${OLLAMA_API_PORT:-11434}"
ports=""

add_port() {
  local p="$1"
  [[ -z "$p" ]] && return
  [[ "$p" == "$OLLAMA_API_PORT" ]] && return
  [[ "$p" =~ ^[0-9]+$ ]] || return
  if [[ ",$ports," != *",$p,"* ]]; then
    ports="${ports:+$ports,}$p"
  fi
}

if command -v lsof >/dev/null 2>&1; then
  while read -r p; do
    add_port "$p"
  done < <(
    # macOS lsof: COMMAND kısa olabilir (ollama, llama-ser)
    lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null \
      | awk 'NR>1 {
          cmd=tolower($1)
          # NAME genelde son alan: 127.0.0.1:54321 veya *:54321
          name=$NF
          if (cmd ~ /llama|ollama/ && name ~ /:[0-9]+$/) {
            n=split(name, a, ":")
            print a[n]
          }
        }'
  )
fi

# ps cmdline: llama-server VE ollama runner --port
if command -v ps >/dev/null 2>&1; then
  while read -r line; do
    # ollama serve / GUI hariç
    echo "$line" | grep -Eqi 'ollama[[:space:]]+serve|Ollama\.app.*serve' && continue
    if echo "$line" | grep -Eqi 'llama-server|ollama.*runner|/runners/'; then
      p="$(echo "$line" | sed -n 's/.*--port[=[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -1)"
      add_port "$p"
    elif echo "$line" | grep -Eqi 'ollama' && echo "$line" | grep -Eq '--port'; then
      p="$(echo "$line" | sed -n 's/.*--port[=[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -1)"
      add_port "$p"
    fi
  done < <(ps -ax -o command= 2>/dev/null || true)
fi

echo "$ports"
