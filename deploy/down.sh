#!/usr/bin/env bash
# Derruba a Agatha (pod agatha: LLM + servidor). Volta com deploy/up.sh ou no próximo boot.
set -euo pipefail
systemctl --user stop agatha-pod.service && echo "Agatha parada"
