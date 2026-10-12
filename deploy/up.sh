#!/usr/bin/env bash
# Sobe a Agatha (pod agatha: agatha-llm + agatha-server) pelo systemd do usuário (ADR-013).
# Instalação inicial: deploy/install-systemd.sh.
#
#   deploy/up.sh        sobe (ou reinicia, se já estiver no ar)
#   deploy/down.sh      derruba
#   journalctl --user -u agatha-server -f
#
# O start espera os dois containers ficarem saudáveis: LLM respondendo e servidor ouvindo na 8765
# (STT e TTS carregados, prompt de sistema no cache do LLM).
set -euo pipefail

systemctl --user cat agatha-pod.service >/dev/null 2>&1 \
    || { echo "units não instaladas: rode deploy/install-systemd.sh"; exit 1; }

echo "subindo a Agatha (pode levar ~1 min)..."
if systemctl --user is-active -q agatha-pod.service; then
    systemctl --user restart agatha-pod.service
fi
if ! systemctl --user start agatha-pod.service agatha-llm.service agatha-server.service; then
    systemctl --user --no-pager status agatha-llm.service agatha-server.service | tail -30
    exit 1
fi
echo "Agatha no ar: ws://$(hostname -I | awk '{print $1}'):8765/"
