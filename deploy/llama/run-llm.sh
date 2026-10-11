#!/usr/bin/env bash
# Sobe o llama-server da Agatha (ADR-004) num container Podman sem root, com a GPU via CDI.
# Uso: deploy/llama/run-llm.sh      (parar: podman stop agatha-llm)
# Na Fase 4 isto vira um serviço systemd.
set -euo pipefail

IMAGE=${AGATHA_LLM_IMAGE:-localhost/agatha-llama:b11205}
MODEL=${AGATHA_LLM_MODEL:-gemma-4-E4B-it-Q4_K_M.gguf}
MODELS_DIR=${AGATHA_LLM_MODELS_DIR:-/dados/agatha/models/llm}
PORT=${AGATHA_LLM_PORT:-8080}
NAME=agatha-llm
# 2 threads de CPU bastam (todas as camadas na GPU) e deixam núcleos livres para o Piper

podman rm -f "$NAME" >/dev/null 2>&1 || true
podman run -d --name "$NAME" \
    --device nvidia.com/gpu=all \
    -p "127.0.0.1:${PORT}:8080" \
    -v "${MODELS_DIR}:/models:ro,z" \
    "$IMAGE" \
    -m "/models/${MODEL}" \
    --host 0.0.0.0 --port 8080 \
    -t "${AGATHA_LLM_THREADS:-2}" \
    -ngl 99 -c 4096 -np 1 --jinja --reasoning-budget 0 >/dev/null

printf "aguardando o llama-server (%s)" "$MODEL"
for _ in $(seq 1 120); do
    if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
        echo " pronto em http://127.0.0.1:${PORT}"
        exit 0
    fi
    if [ "$(podman inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" != "true" ]; then
        echo " o container parou:"; podman logs --tail 30 "$NAME"; exit 1
    fi
    printf "."; sleep 1
done
echo " timeout"; exit 1
