#!/usr/bin/env bash
# Sobe a Agatha no agatha-srv: um pod Podman (sem root) com o LLM e o servidor.
#
#   deploy/up.sh        sobe (recria se já existir)
#   deploy/down.sh      derruba
#   podman logs -f agatha-server
#
# Portas do pod: 8765 (WebSocket, aberta para a LAN pelo firewall) e 8080 (llama-server, só em
# 127.0.0.1 do host, para os benchmarks). Dentro do pod, o servidor fala com o LLM por 127.0.0.1.
# Os containers usam o fuso do host (--tz=local): a Agatha informa a hora a partir dele.
# Na Fase 4 isto vira um conjunto de units do systemd (Quadlet).
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
POD=agatha
MODELS=${AGATHA_MODELS_DIR:-/dados/agatha/models}
DATA=${AGATHA_DATA_DIR:-/dados/agatha/data}   # memória (SQLite), com escrita
LLM_IMAGE=${AGATHA_LLM_IMAGE:-localhost/agatha-llama:b11205}
LLM_MODEL=${AGATHA_LLM_MODEL:-gemma-4-E4B-it-Q4_K_M.gguf}
SERVER_IMAGE=${AGATHA_SERVER_IMAGE:-localhost/agatha-server:0.2.0}
ENV_FILE=${AGATHA_ENV_FILE:-$REPO/server/.env}

[ -f "$ENV_FILE" ] || { echo "falta $ENV_FILE (veja server/.env.example)"; exit 1; }
mkdir -p "$DATA/memory"

podman pod rm -f "$POD" >/dev/null 2>&1 || true
podman pod create --name "$POD" -p 8765:8765 -p 127.0.0.1:8080:8080 >/dev/null

# LLM (ADR-004). 2 threads de CPU bastam (todas as camadas na GPU) e deixam núcleos para o Piper.
podman run -d --pod "$POD" --name agatha-llm --tz=local \
    --device nvidia.com/gpu=all \
    -v "$MODELS/llm:/models:ro,z" \
    "$LLM_IMAGE" \
    -m "/models/$LLM_MODEL" --host 0.0.0.0 --port 8080 \
    -t 2 -ngl 99 -c 4096 -np 1 --jinja --reasoning-budget 0 >/dev/null

# espera até `check` dar certo; falha se o container parar ou se passar de 120 s
wait_for() {
    local name=$1 check=$2
    printf "aguardando %s" "$name"
    for _ in $(seq 1 120); do
        if eval "$check"; then echo " ok"; return 0; fi
        if [ "$(podman inspect -f '{{.State.Running}}' "$name")" != "true" ]; then
            echo " o container parou:"; podman logs --tail 30 "$name"; exit 1
        fi
        printf "."; sleep 1
    done
    echo " timeout:"; podman logs --tail 30 "$name"; exit 1
}

wait_for agatha-llm "curl -fsS http://127.0.0.1:8080/health >/dev/null 2>&1"

# Servidor: STT na GPU, TTS na CPU, núcleo. Modelos somente leitura; token pelo env-file.
podman run -d --pod "$POD" --name agatha-server --tz=local \
    --device nvidia.com/gpu=all \
    --env-file "$ENV_FILE" \
    -v "$MODELS:/models:ro,z" \
    -v "$DATA:/data:z" \
    "$SERVER_IMAGE" >/dev/null

wait_for agatha-server "podman logs agatha-server 2>&1 | grep -q 'ouvindo em'"
echo "Agatha no ar: ws://$(hostname -I | awk '{print $1}'):8765/"
