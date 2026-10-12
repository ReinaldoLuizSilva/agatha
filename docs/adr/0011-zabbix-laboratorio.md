# ADR-011: Zabbix de laboratório no agatha-srv e ferramentas somente leitura

- **Status:** Aceito
- **Data:** 2026-10-11

## Contexto

A Fase 3 dá à Agatha ferramentas para consultar os sistemas com que o Reinaldo trabalha. Ele não
tem um Zabbix que ela possa acessar (o do trabalho não deve ser acessado de casa) e decidiu
**montar um laboratório no próprio agatha-srv**, monitorando o próprio servidor. Restrições:

- **host limpo** (ADR-001): o host fica só com o driver e o runtime de containers;
- a GPU está no limite (3,66 de 4 GB), mas há ~12 GB de RAM livres;
- as ferramentas da Agatha **não podem alterar nada**: nesta fase, só leitura;
- toda ferramenta nova precisa ser medida no benchmark antes de entrar em uso (ADR-009).

## Decisão

1. **Zabbix 7.0 LTS** (suporte completo até jun/2027; a 8.0 LTS ainda não foi lançada; a 7.4 não
   é LTS), com as imagens oficiais `alpine-7.0.31` e **PostgreSQL 17** (a 7.0 suporta de 13 a 18).

2. **Pod `zabbix`**, separado do pod da Agatha, com `postgres`, `zabbix-server` e
   `zabbix-web` (nginx). Os dados ficam em `/dados/agatha/lab/zabbix/postgres`. O frontend é
   publicado na porta **8081**, liberada no firewall só para a LAN (`192.168.1.0/24`). Scripts:
   `deploy/zabbix/up.sh` e `down.sh`.

3. **Agente `zabbix-agent2` em container** (decisão do Reinaldo, em vez do RPM no host). Ele usa a
   rede e os processos do host (`--network host --pid host`), o que deixa CPU, memória, rede e
   processos corretos, e monta `/dados` somente leitura. Discos só aparecem se forem montados.

4. **Rede do Podman sem root (pasta):** de dentro de um container, o IP da LAN do host aponta para
   o próprio container. Por isso:
   - o host é alcançado por `host.containers.internal` (169.254.1.2);
   - o servidor do Zabbix fala com o agente por esse nome;
   - o agente recebe a conexão com o **IP da LAN do host** como origem, e é esse IP que precisa
     estar na lista de origens permitidas;
   - a Agatha acessa a API por `http://host.containers.internal:8081/api_jsonrpc.php`.

5. **Configuração pela API** (`deploy/zabbix/setup.py`, idempotente):
   - troca a senha padrão do Admin por uma aleatória (em `/dados/agatha/lab/zabbix/admin-password`,
     modo 600);
   - cria o host `agatha-srv` com o template "Linux by Zabbix agent";
   - desativa o host padrão "Zabbix server", que geraria alertas falsos;
   - cria o usuário `agatha`, num grupo com **permissão só de leitura** e sem acesso ao frontend,
     e um token de API gravado no `server/.env`.

   O script confere que o token **não consegue** chamar `host.update`.

6. **Ferramentas da Agatha** (`server/agatha_server/tools/zabbix.py`), registradas só se
   `AGATHA_ZABBIX_URL` e `AGATHA_ZABBIX_TOKEN` estiverem configurados:
   - `zabbix_problemas(host?)`: triggers em estado de problema, com severidade em português, host
     e desde quando;
   - `zabbix_host(host)`: disponibilidade do agente, CPU, memória, discos e tempo ligado. Valores
     com **mais de 5 minutos** não são apresentados como atuais: a ferramenta informa a hora da
     última coleta.

   A busca de host aceita trechos ("agatha" → `agatha-srv`) e, como segunda tentativa,
   **semelhança**, porque o nome chega pelo reconhecimento de voz ("Agatha CRV" → `agatha-srv`).
   Se não achar, devolve a lista de hosts monitorados.

## Consequências

**Medido:**
- benchmark com as definições reais: **47/48**, com 28/28 chamadas e argumentos certos;
- ao vivo, com o servidor saudável: "como está o servidor agatha-srv?" → CPU, tempo ligado e
  disco reais;
- com um **problema real** (agente parado de propósito): o Zabbix disparou "Linux: Zabbix agent
  is not available" em 3 min, e a Agatha respondeu "Tem um problema no host agatha-srv. O agente
  do Zabbix não está disponível desde as 23h09." O primeiro som saiu em ~1,8–2,0 s.

**Positivas**

- Dados reais em vez de inventados, sem risco de alteração (token de leitura conferido).
- O laboratório reproduz um cenário de trabalho e pode crescer (mais hosts, itens próprios, GPU).
- Usa ~300 MB de RAM e nada de GPU.

**Negativas / riscos**

- **Prompt maior:** as duas ferramentas levaram o começo fixo do prompt a ~1500 tokens, e o
  pré-aquecimento a ~5,4 s (ADR-010).
- **Agente em container:** discos não montados não aparecem, e a descoberta de sistemas de arquivos
  enxerga os pontos de montagem do container.
- **Nomes de host dependem do STT;** a busca por semelhança ajuda, mas nomes muito diferentes
  ainda falham. Uma evolução possível é pôr os nomes dos hosts no vocabulário do Whisper.
- **Mais um serviço para operar:** por enquanto ele não volta sozinho depois de reiniciar a
  máquina; isso entra no systemd, na Fase 4. Credenciais ficam em arquivos com modo 600 em
  `/dados/agatha/lab/zabbix`.
