#!/usr/bin/env bash
# Derruba o Zabbix de laboratório (os dados do PostgreSQL ficam em /dados/agatha/lab/zabbix).
set -euo pipefail
podman pod rm -f -t 10 zabbix >/dev/null 2>&1 && echo "pod zabbix removido" || echo "pod zabbix não existia"
podman rm -f -t 10 zabbix-agent2 >/dev/null 2>&1 && echo "zabbix-agent2 removido" || true
