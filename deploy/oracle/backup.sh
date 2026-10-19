#!/usr/bin/env bash
# Backup do Oracle de laboratório (ADR-020): RMAN completo e comprimido + archivelogs (apagados
# depois de copiados), com retenção de 2 backups (CONFIGURE RETENTION POLICY TO REDUNDANCY 2) na
# área de recuperação /opt/oracle/oradata/fra (= /dados/agatha/lab/oracle/oradata/fra, teto de 10 GB).
# Roda todo dia pelo oracle-backup.timer (deploy/install-labs.sh); à mão: deploy/oracle/backup.sh
set -euo pipefail

podman exec -i oracle-free nice -n 19 rman target / <<'RMAN'
BACKUP DATABASE PLUS ARCHIVELOG DELETE INPUT;
DELETE NOPROMPT OBSOLETE;
RMAN
