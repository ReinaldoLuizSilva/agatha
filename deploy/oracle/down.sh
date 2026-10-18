#!/usr/bin/env bash
# Derruba o Oracle de laboratório (os dados ficam em /dados/agatha/lab/oracle/oradata).
set -euo pipefail

if systemctl --user cat oracle-free.service >/dev/null 2>&1; then  # unit do systemd (ADR-017)
    systemctl --user stop oracle-free
    echo "Oracle parado (volta no próximo boot; para não voltar: deploy/install-labs.sh --uninstall)"
    exit 0
fi
podman stop -t 60 oracle-free >/dev/null 2>&1 || true   # dá tempo do shutdown limpo do banco
podman rm -f oracle-free >/dev/null 2>&1 && echo "oracle-free removido" || echo "oracle-free não existia"
