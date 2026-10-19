#!/usr/bin/env bash
# Instala os laboratórios (Zabbix e Oracle) como serviços do systemd do usuário (Quadlet, ADR-017):
# sobem sozinhos no boot, como a Agatha, e voltam se caírem.
#
#   deploy/install-labs.sh               instala/atualiza as units e (re)inicia os laboratórios
#   deploy/install-labs.sh --uninstall   para e remove as units (voltam a ser só os up.sh/down.sh)
#
# As senhas continuam nos arquivos criados pelos up.sh (/dados/agatha/lab/*/…-password); daqui saem
# os arquivos de ambiente que as units leem (db.env, agent.env, oracle.env, permissão 600, fora do
# git). O agent.env leva o IP da LAN do host: rode de novo se o IP mudar.
# Memória: ~2,4 GB a mais, sempre (Oracle ~2,1 GB, Zabbix ~0,3 GB).
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
DEST=${XDG_CONFIG_HOME:-$HOME/.config}/containers/systemd
ZLAB=/dados/agatha/lab/zabbix
OLAB=/dados/agatha/lab/oracle
UNITS=(zabbix.pod zabbix-postgres.container zabbix-server.container zabbix-web.container
       zabbix-agent2.container oracle-free.container)
SERVICES=(zabbix-agent2 zabbix-web zabbix-server zabbix-postgres zabbix-pod oracle-free)
TIMER_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
TIMER_UNITS=(oracle-backup.service oracle-backup.timer)  # backup RMAN diário (ADR-020)

if [ "${1:-}" = "--uninstall" ]; then
    systemctl --user disable --now oracle-backup.timer 2>/dev/null || true
    systemctl --user stop "${SERVICES[@]}" 2>/dev/null || true
    for u in "${UNITS[@]}"; do rm -f "$DEST/$u"; done
    for u in "${TIMER_UNITS[@]}"; do rm -f "$TIMER_DIR/$u"; done
    systemctl --user daemon-reload
    echo "laboratórios removidos do systemd (dados preservados em /dados/agatha/lab)"
    exit 0
fi

[ -f "$ZLAB/postgres-password" ] && [ -f "$OLAB/system-password" ] \
    || { echo "rode uma vez deploy/zabbix/up.sh e deploy/oracle/up.sh (criam senhas e dados)"; exit 1; }

(
    umask 077
    printf 'POSTGRES_USER=zabbix\nPOSTGRES_PASSWORD=%s\nPOSTGRES_DB=zabbix\nDB_SERVER_HOST=127.0.0.1\n' \
        "$(cat "$ZLAB/postgres-password")" > "$ZLAB/db.env"
    printf 'ZBX_SERVER_HOST=%s,127.0.0.1\n' "$(hostname -I | awk '{print $1}')" > "$ZLAB/agent.env"
    printf 'ORACLE_PASSWORD=%s\n' "$(cat "$OLAB/system-password")" > "$OLAB/oracle.env"
)

mkdir -p "$DEST"
for u in "${UNITS[@]}"; do install -m 0644 "$REPO/deploy/quadlet/lab/$u" "$DEST/"; done
mkdir -p "$TIMER_DIR"
for u in "${TIMER_UNITS[@]}"; do install -m 0644 "$REPO/deploy/systemd/$u" "$TIMER_DIR/"; done
systemctl --user daemon-reload
systemctl --user enable --now oracle-backup.timer
echo "units instaladas em $DEST e $TIMER_DIR (backup do Oracle às 02:30)"

# containers criados à mão pelos up.sh antigos têm os mesmos nomes: saem antes do systemd assumir
if ! systemctl --user is-active -q zabbix-pod.service && podman pod exists zabbix; then
    podman pod rm -f -t 10 zabbix >/dev/null
    podman rm -f -t 10 zabbix-agent2 >/dev/null 2>&1 || true
    echo "pod zabbix antigo (fora do systemd) removido"
fi
if ! systemctl --user is-active -q oracle-free.service && podman container exists oracle-free; then
    podman stop -t 60 oracle-free >/dev/null  # shutdown limpo do banco
    podman rm -f oracle-free >/dev/null
    echo "oracle-free antigo (fora do systemd) removido"
fi

"$REPO/deploy/zabbix/up.sh"
"$REPO/deploy/oracle/up.sh"
