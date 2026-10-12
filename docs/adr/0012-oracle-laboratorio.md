# ADR-012: Oracle 23ai Free de laboratório e ferramentas somente leitura

- **Status:** Aceito
- **Data:** 2026-10-11

## Contexto

Como no Zabbix (ADR-011), o Reinaldo não tem um Oracle que a Agatha possa acessar e decidiu
montar um laboratório no agatha-srv. Restrições: host limpo (ADR-001), nada de GPU, ferramentas
somente leitura, medição no benchmark antes do uso (ADR-009) e o prompt cada vez maior
(ADR-010).

Opções de imagem:

- **oficial** (`container-registry.oracle.com/database/free`): exige login na Oracle e aceite de
  licença para baixar;
- **`gvenzl/oracle-free`** (Docker Hub): mantida por Gerald Venzl, gerente de produto da Oracle,
  com ~9 milhões de downloads, nas variantes normal, `slim` e `faststart`.

## Decisão

1. **Oracle Database 23ai Free** com a imagem `gvenzl/oracle-free:23.9` (decisão do Reinaldo pela
   imagem sem conta Oracle). A **variante normal**, não a `faststart`: a `faststart` traz o banco
   descompactado dentro da imagem, em `/opt/oracle/oradata`, e o volume persistente montado ali o
   esconde (`ORA-00205`). A normal extrai o banco para o volume na primeira partida (~20 s).
   Limites do Free: 2 threads, 2 GB de RAM, 12 GB de dados de usuário.

2. **Container `oracle-free`**, com dados em `/dados/agatha/lab/oracle/oradata` e a porta 1521
   publicada no host, mas **sem liberação no firewall**. Só os containers do próprio servidor
   conectam, por `host.containers.internal`. O acesso pela LAN (SQL Developer) fica para quando
   for necessário.

   Uma porta publicada só em `127.0.0.1` do host **não** é alcançável de outro container na rede
   pasta, por isso a publicação é em todas as interfaces, com o firewall protegendo.

3. **Configuração** (`deploy/oracle/setup.py`, idempotente), como **SYS/SYSDBA**: no 23ai, o
   SYSTEM não consegue conceder `SELECT` em views do SYS (`ORA-01031`).
   - usuário **`AGATHA_RO`** com o mínimo de privilégio: `CREATE SESSION` e `SELECT` só em
     `DBA_TABLESPACE_USAGE_METRICS`, `DBA_TABLESPACES` e `V$SESSION`. O script confere que ele não
     cria objetos;
   - tablespace de demonstração **`DADOS_APP`** (64 MB, sem crescimento automático) enchida até
     ~85%, para existir uma "quase cheia" de verdade. O banco não usa OMF, então o datafile vai
     para o diretório dos datafiles do PDB;
   - senhas aleatórias com modo 600 em `/dados/agatha/lab/oracle`; a conexão é gravada no
     `server/.env`.

4. **Ferramentas** (`server/agatha_server/tools/oracle.py`) com **`python-oracledb` em modo thin**
   (puro Python, sem Instant Client), num pool assíncrono pequeno:
   - `oracle_tablespaces()`: uso de cada tablespace em %, com **usado, livre e máximo já com
     unidade** ("9 MB", "884 GB"). Sem isso, o modelo tentava calcular o espaço livre com a
     calculadora e misturava MB com GB;
   - `oracle_sessoes()`: sessões de usuário abertas e ativas.

   Erros de banco viram `{"erro": ...}` explicável. As ferramentas só são registradas se
   `AGATHA_ORACLE_*` estiverem configuradas.

## Consequências

**Medido:**
- ao vivo: "tem alguma tablespace quase cheia no Oracle?" → "Tem um tablespace chamado
  DADOS_APP. Ele está com 85,2% de uso.";
- em seguida, "quanto espaço ainda sobra nela?" → "Sobram 9 MB nesse tablespace.";
- "quantas sessões tem abertas no banco?" → total e ativas reais;
- primeiro som em ~1,6–1,9 s;
- no benchmark, todos os casos do Oracle passaram.

**Positivas**

- Dados reais do banco, com um usuário que comprovadamente não altera nada.
- O laboratório serve de base para novas consultas (alert log, jobs, crescimento) com o mesmo
  padrão.

**Negativas / riscos**

- **RAM:** ~2,1 GB para o container. Com o Zabbix (~0,3 GB) e a Agatha, sobra folga nos 16 GB,
  mas o laboratório deixou de ser leve. **Disco:** ~2,9 GB em `/dados`.
- **Prompt:** com 7 ferramentas, o começo fixo chegou a **1630 tokens**, e 2283 com a memória
  cheia. Ainda sobram ~1800 tokens de contexto (~16 turnos), mas o pré-aquecimento passou de ~6 s.
  Ferramentas novas precisam de cuidado com o tamanho das regras e dos exemplos.
- **Imagem da comunidade**, não a oficial: depende da manutenção do gvenzl, que é ativa e ligada à
  Oracle.
- Por enquanto o laboratório não volta sozinho depois de reiniciar a máquina; isso entra na Fase 4.
