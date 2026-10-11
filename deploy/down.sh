#!/usr/bin/env bash
# Derruba o pod da Agatha (LLM + servidor).
set -euo pipefail
podman pod rm -f -t 10 agatha >/dev/null 2>&1 && echo "pod agatha removido" || echo "pod agatha não existia"
