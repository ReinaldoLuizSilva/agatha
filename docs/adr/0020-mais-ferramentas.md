# ADR-020: Mais ferramentas — estado e backups do Oracle, severidade no Zabbix, GitHub

- **Status:** Aceito
- **Data:** 2026-10-19
- **Complementa:** [ADR-009](0009-ferramentas-tool-calling.md) (ferramentas),
  [ADR-011](0011-zabbix-laboratorio.md) (Zabbix), [ADR-012](0012-oracle-laboratorio.md) (Oracle)

## Contexto

Nas falas reais e no roteiro de jargão (ADR-018), o Reinaldo perguntou coisas que a Agatha não
tinha como saber:

- o Data Guard está sincronizado?
- tem lock no banco?
- o banco está em archivelog?
- o backup de ontem rodou?
- quais são os problemas de alta prioridade?

Ele também quis consultar o GitHub. As restrições são as de sempre: tudo somente leitura, cada
ferramenta medida nos benchmarks antes de entrar, e o prompt cresce a cada ferramenta (1983
tokens, de um contexto de 4096). O laboratório Oracle não tinha backup nem archivelog, então não
havia o que consultar.

## Decisão

1. **`oracle_banco`**, uma ferramenta para várias perguntas: se o banco está aberto, modo
   archivelog, papel (primário ou standby), Data Guard (destinos de standby em
   `V$ARCHIVE_DEST_STATUS`), versão, há quanto tempo está no ar e sessões bloqueadas por locks
   (`V$SESSION.blocking_session`).
2. **`oracle_backups`**: os últimos 5 jobs do RMAN (`V$RMAN_BACKUP_JOB_DETAILS`), com tipo,
   status, tamanho e duração, e "ontem às 02h31" em vez de uma data crua.
3. **Duas conexões ao Oracle.** O RMAN registra os backups no controlfile, que a PDB não enxerga:
   lá dentro a visão vem vazia. O padrão dos plugins de monitoração, como o do Zabbix, é um
   **usuário comum `C##AGATHA_RO` no contêiner raiz**, só com `CREATE SESSION` e `SELECT` em
   quatro visões. O `AGATHA_RO` na PDB continua para tablespaces, sessões e locks. O
   `deploy/oracle/setup.py` cria os dois e confirma que nenhum consegue criar nada.
4. **Laboratório com backup de verdade:** ARCHIVELOG, área de recuperação de 10 GB no volume,
   RMAN com redundância 2, autobackup do controlfile e backups comprimidos. O
   `deploy/oracle/backup.sh` (completo + archivelogs, apaga os obsoletos) roda todo dia às 02:30
   pelo `oracle-backup.timer` (`Persistent`), instalado pelo `deploy/install-labs.sh`. Primeiro
   backup: 437 MB em 70 s.
5. **`zabbix_problemas` com `severidade_minima`**: "alta prioridade" vira "alta ou desastre",
   sem ferramenta nova.
6. **`github_repo`**, uma ferramenta e uma chamada: últimos commits do branch principal, PRs e
   issues abertos (os PRs que a API de issues devolve ficam de fora) e a última execução da CI,
   com as consultas em paralelo. O token é *fine-grained*, só de leitura (Actions, Commit statuses,
   Contents, Issues, Pull requests), nos repositórios que o Reinaldo escolheu. Ele fica em
   `AGATHA_GITHUB_TOKEN`, fora do git, e o repositório pode ser dito pelo nome curto. Sem token, a
   ferramenta não é registrada.
7. **Só regras, sem exemplos de conversa** nas ferramentas novas: o exemplo é o que mais custa no
   prompt (58 tokens sem, até 219 com), e o benchmark mostrou que não fez falta.
8. **Dados em texto, não booleanos:** com `"archivelog": true` o modelo pulava o campo e não
   respondia a pergunta. `"modo_archivelog": "ligado"` resolveu.

## Resultados

| | antes da fase | depois (10 ferramentas) |
|---|---|---|
| prefixo do prompt | 1983 tokens | **2311 tokens** (+328) |
| `tools_bench` (tudo certo) | 94/105 (89,5%) | 118/132 (89%), casos novos 15/15 + GitHub 11/12 |
| `precision_bench` | 81/99 | 95/111, invenções conhecidas 0 |
| 1º áudio, teste de fumaça (p50) | ~2,05 s | ~2,07 s |

Os quatro casos que antes eram "não consigo" (Data Guard, locks, backup de ontem, archivelog)
agora usam a ferramenta. "Reinicia o listener" continua sendo "não consigo", porque é escrita.

## Consequências

**Positivas**

- Ela responde as perguntas de DBA que mais apareceram no uso, com dado real do laboratório.
- O laboratório agora tem uma rotina de backup como a de um banco de verdade, que também serve
  de estudo.
- O usuário comum no CDB segue o padrão de mercado e fica documentado e reproduzível.

**Negativas / riscos**

- **O prompt chegou a ~2300 tokens.** Com a memória cheia (+~650), sobram ~1100 para histórico e
  resposta, uns 9 a 10 turnos. Antes de outra ferramenta, vale enxugar regras ou juntar
  ferramentas.
- **O GitHub depende da internet** e de um token que expira. Sem rede ou com o token vencido,
  ela avisa, mas a consulta falha.
- **Confusão nova, rara:** "qual o nome do meu banco de produção?" chamou o `github_repo` em 1 de
  3 rodadas. Continua o ponto fraco conhecido da memória (`guardar_fato` em pergunta sobre fato
  guardado).
- **O backup diário usa disco e CPU às 02:30** (~1 minuto, prioridade baixa). A área de
  recuperação tem teto de 10 GB e o RMAN apaga o que fica obsoleto.
- Com o Data Guard sem standby, ela só pode dizer "não configurado". O caminho com standby está
  implementado, mas não foi testado no laboratório.
