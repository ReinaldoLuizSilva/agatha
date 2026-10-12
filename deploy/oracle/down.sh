#!/usr/bin/env bash
# Derruba o Oracle de laboratório (os dados ficam em /dados/agatha/lab/oracle/oradata).
set -euo pipefail
podman stop -t 60 oracle-free >/dev/null 2>&1 || true   # dá tempo do shutdown limpo do banco
podman rm -f oracle-free >/dev/null 2>&1 && echo "oracle-free removido" || echo "oracle-free não existia"
