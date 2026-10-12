#!/usr/bin/env bash
# Instala a Agatha como serviço do systemd do usuário (Quadlet, ADR-013) e sobe o pod.
#
#   deploy/install-systemd.sh     instala/atualiza as units e (re)inicia o pod
#
# As units ficam em ~/.config/containers/systemd/ (cópia de deploy/quadlet/); o systemd gera
# agatha-pod.service, agatha-llm.service e agatha-server.service. Depois disso, use deploy/up.sh,
# deploy/down.sh e journalctl --user -u agatha-server.
#
# Para subir no boot sem ninguém logado, o usuário precisa de "linger" (uma vez, como root):
#   sudo loginctl enable-linger agatha
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
DEST=${XDG_CONFIG_HOME:-$HOME/.config}/containers/systemd

[ -f "$REPO/server/.env" ] || { echo "falta $REPO/server/.env (veja server/.env.example)"; exit 1; }
for image in localhost/agatha-llama:b11205 localhost/agatha-server:0.2.0; do
    podman image exists "$image" || { echo "falta a imagem $image (veja deploy/README.md)"; exit 1; }
done
mkdir -p "$DEST" /dados/agatha/data/memory
install -m 0644 "$REPO"/deploy/quadlet/agatha.pod "$REPO"/deploy/quadlet/agatha-*.container "$DEST"/
systemctl --user daemon-reload
echo "units instaladas em $DEST"

# pod criado à mão (versões antigas do up.sh) tem o mesmo nome: sai antes do systemd assumir
if podman pod exists agatha && ! systemctl --user is-active -q agatha-pod.service; then
    podman pod rm -f -t 10 agatha >/dev/null
    echo "pod agatha antigo (fora do systemd) removido"
fi

"$REPO"/deploy/up.sh

if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
    echo
    echo "ATENÇÃO: linger desligado. Sem ele a Agatha para quando você sai da sessão e não sobe no boot."
    echo "Rode uma vez, como root:  sudo loginctl enable-linger $USER"
fi
