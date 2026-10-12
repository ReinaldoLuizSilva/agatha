#!/usr/bin/env bash
# Sobe o Zabbix de laboratório (ADR-011): pod "zabbix" (postgres + server + web) e o agent2 em
# container usando a rede e os processos do host, para monitorar o próprio agatha-srv.
#
#   deploy/zabbix/up.sh             sobe (ou recria os containers; os dados ficam)
#   deploy/zabbix/down.sh           derruba
#   .venv/bin/python deploy/zabbix/setup.py   configuração inicial pela API (uma vez)
#
# Frontend: http://<agatha-srv>:8081 (liberado no firewall só para a LAN).
# De dentro dos containers, o host é "host.containers.internal" (rede pasta do Podman sem root:
# o IP da LAN do host aponta para o próprio container).
set -euo pipefail

VERSION=${ZABBIX_VERSION:-alpine-7.0.31}
PG_IMAGE=${ZABBIX_PG_IMAGE:-docker.io/library/postgres:17.11-alpine}
LAB=${ZABBIX_LAB_DIR:-/dados/agatha/lab/zabbix}
POD=zabbix
PG_PASS_FILE="$LAB/postgres-password"

mkdir -p "$LAB/postgres"
if [ ! -f "$PG_PASS_FILE" ]; then
    (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > "$PG_PASS_FILE")
fi
PG_PASS=$(cat "$PG_PASS_FILE")
DB_ENV=(-e DB_SERVER_HOST=127.0.0.1 -e POSTGRES_USER=zabbix -e POSTGRES_PASSWORD="$PG_PASS" -e POSTGRES_DB=zabbix)

podman pod rm -f "$POD" >/dev/null 2>&1 || true
podman rm -f zabbix-agent2 >/dev/null 2>&1 || true
podman pod create --name "$POD" -p 8081:8080 >/dev/null

podman run -d --pod "$POD" --name zabbix-postgres --tz=local \
    -e POSTGRES_USER=zabbix -e POSTGRES_PASSWORD="$PG_PASS" -e POSTGRES_DB=zabbix \
    -v "$LAB/postgres:/var/lib/postgresql/data:Z" \
    "$PG_IMAGE" >/dev/null

podman run -d --pod "$POD" --name zabbix-server --tz=local "${DB_ENV[@]}" \
    -e ZBX_CACHESIZE=64M \
    "docker.io/zabbix/zabbix-server-pgsql:$VERSION" >/dev/null

podman run -d --pod "$POD" --name zabbix-web --tz=local "${DB_ENV[@]}" \
    -e ZBX_SERVER_HOST=127.0.0.1 -e PHP_TZ=America/Sao_Paulo \
    "docker.io/zabbix/zabbix-web-nginx-pgsql:$VERSION" >/dev/null

# Agente no host: rede e processos do host (CPU, memória, rede, processos corretos); discos só os
# montados aqui. O server (no pod) chega por host.containers.internal, e o pasta entrega a conexão
# com o IP da LAN do host como origem: é esse IP que o agente precisa aceitar.
HOST_IP=$(hostname -I | awk '{print $1}')
podman run -d --name zabbix-agent2 --tz=local \
    --network host --pid host \
    -e ZBX_HOSTNAME=agatha-srv \
    -e ZBX_SERVER_HOST="$HOST_IP,127.0.0.1" \
    -e ZBX_ACTIVE_ALLOW=false \
    -v /dados:/dados:ro \
    --security-opt label=disable \
    "docker.io/zabbix/zabbix-agent2:$VERSION" >/dev/null

printf "aguardando o frontend"
for _ in $(seq 1 180); do
    if curl -fsS -o /dev/null http://127.0.0.1:8081/; then echo " ok: http://$(hostname -I | awk '{print $1}'):8081/"; exit 0; fi
    for c in zabbix-postgres zabbix-server zabbix-web zabbix-agent2; do
        if [ "$(podman inspect -f '{{.State.Running}}' $c)" != "true" ]; then
            echo " $c parou:"; podman logs --tail 30 $c; exit 1
        fi
    done
    printf "."; sleep 1
done
echo " timeout"; exit 1
