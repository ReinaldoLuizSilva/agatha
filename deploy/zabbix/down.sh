#!/usr/bin/env bash
# Derruba o Zabbix de laboratório (os dados do PostgreSQL ficam em /dados/agatha/lab/zabbix).
set -euo pipefail

if systemctl --user cat zabbix-pod.service >/dev/null 2>&1; then  # units do systemd (ADR-017)
    systemctl --user stop zabbix-agent2 zabbix-web zabbix-server zabbix-postgres zabbix-pod
    echo "Zabbix parado (volta no próximo boot; para não voltar: deploy/install-labs.sh --uninstall)"
    exit 0
fi
podman pod rm -f -t 10 zabbix >/dev/null 2>&1 && echo "pod zabbix removido" || echo "pod zabbix não existia"
podman rm -f -t 10 zabbix-agent2 >/dev/null 2>&1 && echo "zabbix-agent2 removido" || true
