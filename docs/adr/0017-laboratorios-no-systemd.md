# ADR-017: Laboratórios (Zabbix e Oracle) como serviços do systemd

- **Status:** Aceito
- **Data:** 2026-10-18
- **Substitui:** a decisão 7 do [ADR-013](0013-systemd-quadlet.md) ("laboratórios fora do systemd")

## Contexto

O ADR-013 pôs a Agatha no systemd do usuário com Quadlet e deixou o Zabbix (ADR-011) e o Oracle
(ADR-012) de laboratório de fora: eles subiam à mão com `deploy/zabbix/up.sh` e
`deploy/oracle/up.sh`, porque não fazem parte da Agatha e as ferramentas já tratam o laboratório
fora do ar.

Na prática, os laboratórios ficaram **6 dias desligados** sem ninguém perceber. Pararam nos
reinícios do servidor (12/10 e 13/10), e no uso de 18/10 as perguntas sobre o Zabbix e o Oracle
responderam "não consegui acessar". As ferramentas trataram o erro como deviam, mas deixaram de
servir para alguma coisa. O Reinaldo pediu que eles subam sozinhos no boot, como a Agatha.

## Decisão

1. **Units Quadlet em `deploy/quadlet/lab/`**, instaladas por `deploy/install-labs.sh` em
   `~/.config/containers/systemd/`, ao lado das da Agatha:
   - `zabbix.pod` (porta 8081) com `zabbix-postgres`, `zabbix-server` e `zabbix-web`, na mesma
     ordem dos scripts: o server depois do banco, o frontend depois do server;
   - `zabbix-agent2` fora do pod, com a rede e os processos do host;
   - `oracle-free` (porta 1521, fechada no firewall).
2. **`Notify=healthy`** onde a imagem permite: `pg_isready` no postgres, `curl` no frontend e o
   `healthcheck.sh` da imagem do Oracle. O `start` só termina com o serviço respondendo.
   `Restart=on-failure` em todos. O Oracle tem `StopTimeout=60`, para o shutdown limpo do banco.
3. **As senhas não vão para as units nem para o git.** O Quadlet não lê arquivos de senha, então o
   instalador gera `db.env`, `agent.env` e `oracle.env` (permissão 600) em `/dados/agatha/lab`, a
   partir dos arquivos de senha que os `up.sh` já criavam. O `agent.env` leva o IP da LAN do host,
   que o agente precisa aceitar. Se o IP mudar, basta rodar o instalador de novo.
4. **Os `up.sh`/`down.sh` continuam valendo**: com as units instaladas, viram atalhos para
   `systemctl --user restart/stop`. Sem elas, funcionam como antes. Um `down.sh` vale até o
   próximo boot. Para os laboratórios não voltarem mais, use `deploy/install-labs.sh --uninstall`,
   já que as units do Quadlet não têm "disable".
5. **Sem limite de memória nos containers.** O Oracle Free já se limita a 2 GB (SGA + PGA), e um
   teto no container arriscaria matar o banco na partida. O custo fica fixo: ~2,4 GB de RAM a mais,
   sempre (Oracle ~2,1 GB, Zabbix ~0,3 GB).

## Consequências

**Positivas**

- Os laboratórios sobrevivem a reinícios e quedas, e as ferramentas do Zabbix e do Oracle ficam
  sempre úteis.
- Mesmo padrão da Agatha (Quadlet, `systemctl --user`), mais uma peça de portfólio de operação.

**Negativas / riscos**

- **~2,4 GB de RAM ocupados o tempo todo**: com a Agatha, ~5,5–6,5 GB de 16. Em 18/10, logo
  depois da instalação, ~200 MB de páginas paradas foram para o swap (swappiness 10), e o primeiro
  áudio da Agatha não mudou (p50 1,81 s). Treinos e testes pesados continuam com teto de memória.
- O boot fica mais pesado: o Oracle leva ~1 min para ficar saudável e disputa disco e CPU com a
  carga dos modelos da Agatha. A Agatha não depende dele (as ferramentas conectam sob demanda).
- As versões das imagens ficam em dois lugares (units e `up.sh` sem systemd); ao atualizar uma
  versão, mude os dois.
