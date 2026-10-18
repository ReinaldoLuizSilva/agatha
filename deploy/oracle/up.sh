#!/usr/bin/env bash
# Sobe o Oracle de laboratório (ADR-012): Oracle Database 23ai Free (imagem gvenzl/oracle-free),
# PDB FREEPDB1, dados em /dados/agatha/lab/oracle. Variante normal, não "faststart": a faststart
# traz o banco descompactado dentro da imagem, e o volume em /opt/oracle/oradata o esconde
# (ORA-00205); a normal extrai o banco para o volume na primeira partida.
#
#   deploy/oracle/up.sh                       sobe (ou recria o container; os dados ficam)
#   deploy/oracle/down.sh                     derruba
#   .venv/bin/python deploy/oracle/setup.py   usuário somente leitura + dados de demonstração
#
# Com a unit do systemd instalada (deploy/install-labs.sh, ADR-017), sobe/reinicia por ela.
#
# A porta 1521 é publicada no host, mas o firewall não a libera para a LAN: só os containers do
# próprio servidor (a Agatha, por host.containers.internal) conectam.
set -euo pipefail

if systemctl --user cat oracle-free.service >/dev/null 2>&1; then
    printf "subindo o Oracle pelo systemd (pode levar ~1 min)..."
    # o start só termina com o banco saudável (Notify=healthy)
    if systemctl --user restart oracle-free; then echo " ok"; exit 0; fi
    echo; systemctl --user --no-pager status oracle-free | tail -30
    exit 1
fi

IMAGE=${ORACLE_IMAGE:-docker.io/gvenzl/oracle-free:23.9}
LAB=${ORACLE_LAB_DIR:-/dados/agatha/lab/oracle}
NAME=oracle-free
SYS_PASS_FILE="$LAB/system-password"

mkdir -p "$LAB/oradata"
if [ ! -f "$SYS_PASS_FILE" ]; then
    (umask 077; python3 -c 'import secrets; print("A" + secrets.token_urlsafe(18).replace("-", "x").replace("_", "y"))' > "$SYS_PASS_FILE")
fi

podman rm -f "$NAME" >/dev/null 2>&1 || true
# :U ajusta o dono do volume para o usuário oracle de dentro do container (Podman sem root)
podman run -d --name "$NAME" --tz=local \
    -p 1521:1521 \
    -e ORACLE_PASSWORD="$(cat "$SYS_PASS_FILE")" \
    -v "$LAB/oradata:/opt/oracle/oradata:Z,U" \
    "$IMAGE" >/dev/null

printf "aguardando o banco"
for _ in $(seq 1 600); do
    if podman logs "$NAME" 2>&1 | grep -q "DATABASE IS READY TO USE"; then echo " ok"; exit 0; fi
    if [ "$(podman inspect -f '{{.State.Running}}' "$NAME")" != "true" ]; then
        echo " o container parou:"; podman logs --tail 40 "$NAME"; exit 1
    fi
    printf "."; sleep 1
done
echo " timeout"; podman logs --tail 40 "$NAME"; exit 1
