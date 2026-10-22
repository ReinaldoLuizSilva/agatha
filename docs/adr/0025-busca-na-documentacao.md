# ADR-025: Internet, parte 3 — busca na documentação oficial com SearXNG no pod

- **Status:** Aceito
- **Data:** 2026-10-22
- **Fase:** 10 (integração com a internet; escopo aprovado em 21/10)
- **Complementa:** [ADR-022](0022-internet-clima.md), [ADR-024](0024-internet-noticias.md) e
  [ADR-013](0013-systemd-quadlet.md) (pod no systemd do usuário)

## Contexto

Último item da Fase 10: busca na web, **restrita à documentação oficial, para dúvida técnica**
(decisão do Reinaldo em 21/10; os termos de busca podem sair para a internet). O modelo de 4B sabe
os conceitos, mas erra detalhe exato (parâmetro, sintaxe, novidade de versão). Sem API paga: um
metabuscador próprio, o SearXNG, que consulta buscadores públicos e devolve JSON.

Medido em 22/10 com um contêiner de teste:

- Com `site:` na consulta, os resultados ficam no site pedido (Bing, Yahoo, DuckDuckGo, Brave e
  Google CSE respeitam). Sem `site:`, "porta padrão do zabbix agent" trazia Brainly e Medium.
- Os trechos (`content`) da documentação respondem bem: "ALTER DATABASE ... ARCHIVELOG",
  "level 1 incremental backup ... SCN", "DNSOption=ndots:1".
- **Limite dos buscadores**: com ~20 consultas em poucos minutos, Brave e Google CSE responderam
  "too many requests" e o DuckDuckGo pediu CAPTCHA. Google e Qwant negaram acesso desde a 1ª
  consulta. Bing e Yahoo seguiram respondendo, em ~0,5 s.
- RAM: ~130–155 MB.

## Decisão

1. **SearXNG dentro do pod `agatha`** (`deploy/quadlet/agatha-search.container`, imagem
   `searxng/searxng:2026.10.19-81371861f`), ouvindo só em `127.0.0.1:8888`: o servidor chega pelo
   localhost do pod, nada fica aberto no host nem na LAN, e ele herda o DNS do pod (`timeout:1`,
   ADR-022). Unit própria, com healthcheck, `Restart=on-failure`, `MemoryMax=512M` e sem swap. O
   servidor não depende dele para subir: se a busca cair, ela diz que não conseguiu pesquisar.
2. **Buscadores** (`deploy/searxng/settings.yml`): Bing, Yahoo, DuckDuckGo, Brave e Google CSE;
   suspensão local de 5–10 min (o padrão era 1 h por CAPTCHA). Só JSON, sem limiter (ninguém de fora
   chega), sem proxy de imagens. A chave secreta é gerada pelo `install-systemd.sh` em
   `~/.config/agatha/search.env` (fora do git).
3. **Busca a pedido, feita pelo servidor, fora do tool calling**
   ([`tools/search.py`](../../server/agatha_server/tools/search.py), `search_request`): dispara com o
   verbo "pesquisar" (não o substantivo: "a pesquisa Datafolha" não conta), com "procura/busca/olha"
   junto de "documentação", "internet" ou "web" ("busca os problemas do Zabbix" continua no Zabbix),
   ou com "o que mudou"/"novidades" e um número de versão. A consulta é a fala sem "Agatha", sem o
   verbo e sem "na documentação"; "pesquisa isso" (sem assunto) usa a fala anterior.
4. **Na conversa** ([`conversation.py`](../../server/agatha_server/conversation.py)): frase de espera,
   busca (até 10 s) e o resultado entra no turno como uma **chamada de ferramenta já respondida**
   (`buscar_documentacao`, não declarada às ferramentas). No histórico ficam só a pergunta e a
   resposta.
5. **Resultado**: o produto citado escolhe o site (Oracle → `docs.oracle.com`, Zabbix →
   `zabbix.com/documentation`, Podman, Docker, Kubernetes, PostgreSQL, MySQL, Grafana, Prometheus,
   systemd, Red Hat, Python, Git, GitHub, AWS, Azure, GCP, Terraform, Ansible, nginx, Linux →
   man7.org) e só ficam resultados dele (nem todo buscador respeita o `site:`); sem produto, só os
   domínios dessa lista. A mesma página em várias versões (`/6.0/`, `/7.4/`, `/latest/`,
   `v1-32.docs.kubernetes.io`) fica uma vez, a mais nova. Até 3 trechos de ~250 caracteres.

### Por que não é uma ferramenta do LLM

A 1ª versão era a ferramenta `buscar_documentacao(consulta)`, com a regra "pedido explícito ou
detalhe exato de produto; o que é, como funciona e diferenças você responde direto". Nos casos novos
do `tools_bench` foi 44/45, mas os benchmarks completos mostraram o custo:

| | sem busca (só notícias) | com a ferramenta |
|---|---|---|
| `tools_bench`, casos antigos | 189/207 | 182/207 |
| `precision_bench` sem histórico | 113/129 | 108/129 (9 buscas em perguntas de conceito: "pra que serve o RMAN", "trigger nos abics") |
| `precision_bench` com histórico | 113/129 | 109/129 |
| prefixo | 2716 tokens | 2865 |

"Me conta mais sobre essa do PHP", depois de notícias, ia para a busca em vez das notícias. Quatro
redações medidas (12 perguntas × 3; deveria buscar em 3, não em 8): a regra só para pedido
explícito, a descrição "só quando ele pede", o nome `pesquisar_documentacao` e uma regra curta. A
melhor ainda buscava à toa em 7 de 24: com a ferramenta à vista, o modelo de 4B a chama para
quase qualquer pergunta técnica. Cada busca à toa custa 0,5–2 s numa resposta que saía em ~2 s e
gasta a cota dos buscadores.

Com a detecção na fala, a busca nunca é chamada à toa, o prefixo não cresce (2716) e os benchmarks
antigos ficam exatamente como na versão só com notícias (mesmo prompt, mesmas ferramentas).

Como entregar o resultado ao modelo também foi medido: anexado à mensagem do usuário, ele chamava
outra ferramenta depois (`noticias`, `zabbix_problemas`); com `tool_choice: "none"` o cache vale,
mas ele escreve a chamada como texto (seria falada). Como chamada de ferramenta já respondida, 16 de
16 respostas usaram os trechos ("ALTER DATABASE ... com o banco montado", "10050 TCP", "não
encontrei" quando a busca volta vazia) e nenhuma chamou outra ferramenta; o cache vale (só o turno
novo é processado).

## Resultados

- SearXNG no pod: ~130 MB de RAM; não alcançável do host (porta só no localhost do pod).
- Busca a partir do servidor: 0,5–0,9 s com Bing e Yahoo (a 1ª depois de subir, ~2–3 s).
- `search_request`: 7 pedidos reconhecidos e 7 falas que não são pedido ignoradas ("a pesquisa
  Datafolha", "busca os problemas do Zabbix", "novidades do Tecnoblog", "procura um filme").
- Ponta a ponta (voz sintética, 22/10): 1º som ~0,95 s (a frase de espera) nas falas com busca;
  "pesquise isso na documentação" depois de uma pergunta buscou a pergunta anterior; notícias e
  conceito sem busca. O STT da voz sintética estragou várias consultas ("Ativaro Achieve Love" para
  archivelog); com a voz dele o STT é melhor.
- Visto: com um trecho fora de contexto ("se a porta não for definida, será utilizada a padrão
  (10051)", do ServerActive do agente), ela disse "10051 para o agente" (a do agente é 10050). Os
  trechos vêm do buscador, não da página.
- Sem a busca, "Qual a porta padrão do Zabbix agent?" é "não sei" (3 de 3, modelo de 4B): o caminho
  é ele dizer "pesquisa isso".

### Alternativas consideradas

| alternativa | por que não |
|---|---|
| SearXNG como serviço separado, como os laboratórios | precisaria publicar uma porta no host para o servidor chegar; no pod fica no localhost e sem exposição |
| API de busca (Brave Search API, Google Programmable Search) | chave e cota; o projeto evita serviço pago ou cadastrado |
| Busca aberta, sem restringir a documentação oficial | Brainly, Medium e fóruns; o Reinaldo pediu fonte oficial |
| Baixar e ler a página do 1º resultado | mais 1–2 s e muito texto para o modelo de 4B; os trechos já respondem a maioria dos casos medidos |
| Buscar sempre que a pergunta for técnica | +0,5–2 s em toda pergunta de conceito, que o modelo já responde bem |
| Ferramenta do LLM (`buscar_documentacao`) | medida: buscava à toa em 7–15 de 24 perguntas de conceito e piorava o precision 113 → 108 (veja acima) |

## Consequências

**Positivas**

- Detalhes exatos da documentação oficial por voz, sem chave e sem custo, com o tema saindo só para
  os buscadores; o prompt fixo não cresce e nenhuma pergunta de conceito fica mais lenta.
- Nada exposto: o SearXNG só existe para o servidor, dentro do pod.

**Negativas / riscos**

- **Buscadores limitam** instâncias próprias: rajadas suspendem Brave, Google CSE e DuckDuckGo; o
  uso normal (poucas buscas por hora) deve ficar só no Bing e no Yahoo quando eles caírem. Se todos
  caírem, ela diz que não achou. Acompanhar no `podman logs agatha-search`.
- Ele precisa pedir ("pesquisa..."): ela não busca sozinha quando não sabe. Se o STT perder o verbo,
  não há busca.
- Os trechos vêm do buscador, não da página: às vezes falam do assunto sem dar a resposta, e ela
  completa com o que sabe.
- A versão da imagem é fixa: atualizar o SearXNG é trocar a tag na unit e testar (os buscadores
  mudam e o SearXNG acompanha).
