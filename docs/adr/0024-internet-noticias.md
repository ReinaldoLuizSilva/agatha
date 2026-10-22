# ADR-024: Internet, parte 2 — notícias pelos feeds RSS do G1, da Folha e do Tecnoblog

- **Status:** Aceito
- **Data:** 2026-10-22
- **Fase:** 10 (integração com a internet; escopo aprovado em 21/10)
- **Complementa:** [ADR-022](0022-internet-clima.md) (clima, DNS do pod) e
  [ADR-023](0023-historico-compacto-de-ferramentas.md) (resultado grande vira marcador no histórico)

## Contexto

Segundo item da Fase 10: manchetes por voz, das fontes que o Reinaldo escolheu em 21/10 (G1, Folha
e Tecnoblog). Os três têm RSS aberto, sem chave. Conferido em 22/10 a partir do agatha-srv:

| fonte | feeds | itens | observações |
|---|---|---|---|
| G1 | `g1.globo.com/rss/g1/` e seções | 100 por feed | sempre em gzip; o feed geral é ~25% regional (SP, MT, RJ...) e traz a programação "ao vivo" das afiliadas; o resumo vem em `atom:subtitle` |
| Folha | `feeds.folha.uol.com.br/<seção>/rss091.xml` | 100 | ISO-8859-1; o resumo é o 1º parágrafo da matéria, com links |
| Tecnoblog | `tecnoblog.net/feed/` | 50 | ~1 MB (matéria inteira); a seção "achados" é de ofertas |

O G1 ciência e saúde mistura itens de 2023 e o feed do ge é quase todo página de jogo ("acompanhe
tudo sobre X x Y"): ficaram de fora.

## Decisão

1. **Ferramenta `noticias(assunto?, fonte?)`** ([`tools/news.py`](../../server/agatha_server/tools/news.py)),
   sem exemplo de conversa, como as da Fase 8:
   - sem assunto: as mais recentes do G1 e da Folha;
   - assunto que é **seção** (tecnologia, economia, política, mundo, esporte, Rio Grande do Sul, com
     sinônimos como "RS", "mercado", "futebol"): os feeds dela;
   - qualquer outro assunto é **tema** ("a falha do PHP"): busca nos títulos e resumos de todos os
     feeds baixados, nos últimos 7 dias. A busca é local: **o tema não sai para a internet**, só os
     pedidos dos feeds.
   - `fonte` (enum G1, Folha, Tecnoblog) restringe; seção que a fonte não tem vira erro curto
     ("esporte só vem da Folha").
2. **Resultado compacto**: seção → 4 manchetes, uma de cada fonte por vez (`"G1: ..."`; com uma
   fonte só, sem o prefixo); tema → até 3 notícias com o resumo de uma frase. ~100–150 tokens; no
   histórico vira o marcador do ADR-023.
3. **Filtros**: G1 sem as praças regionais (só o RS fica) e sem "ao vivo", sem os sufixos "; VÍDEO";
   no feed do RS, só links `/rs/` (ele traz SC junto); Tecnoblog só `/noticias/`. Busca por palavra
   inteira, plural ou começo comum de no mínimo 5 letras ("eleição" acha "eleições"; com 4,
   "Oracle" achava "orações"); todas as palavras do tema precisam aparecer.
4. **Cache de 5 min por feed**, downloads em paralelo (`httpx.AsyncClient`), até 3 tentativas por
   conexão, parse numa thread. Fonte que não responde vira aviso e as outras seguem.
   `AGATHA_NOTICIAS=0` desliga.
5. **Regra no prompt**: "notícias e manchetes de agora: use noticias, com o assunto e a fonte só se
   ele disser; fatos passados, como quem ganhou uma Copa, você responde direto". Sem a segunda
   parte, "Quem ganhou a Copa do Mundo de 2002?" chamava `noticias(esporte)` em 3 de 3 e ela
   respondia "não encontrei".
6. **Fala**: "R$ 7,1 bilhões" saía "sete reais,um bilhões" e "US$" ficava cru. Agora moedas (R$,
   US$, €) com escala ("sete vírgula um bilhões de reais"), singular ("um real") e siglas comuns
   em manchete (G1, IA, EUA, STF, TSE, PT, PL, PHP, AWS) em `text.ACRONYMS`, ajustáveis de ouvido.

## Resultados

- Feeds a partir do pod (DNS com `timeout:1`, ADR-022): ~150 ms para a seção geral (2 feeds) ou
  tecnologia (3); quando o DNS do provedor perde uma consulta, 1,1–2,2 s. A busca por tema baixa os
  13 feeds (~4,7 MB descomprimidos) em 1,2–2,1 s; com o cache, milissegundos.
- `tools_bench`, 3 rodadas: casos de notícias 39/42 (escolha da ferramenta 41/42 e argumentos certos);
  das 3 falhas, 2 eram respostas certas sem a palavra que o caso exigia e 1 foi "me conta mais sobre
  essa do PHP" respondido sem consultar. Copa de 2002 e Muro de Berlim sem ferramenta, 6/6. Casos
  antigos 150/165, igual à v12.
- `precision_bench`: 113/129 sem histórico (v12 110) e 113/129 com histórico (v12 106), invenções 0.
- Prefixo 2558 → 2705 tokens (esquema ~110, regra ~35). O pré-aquecimento do prompt ao subir passou
  a ~10 s.
- Ponta a ponta (voz sintética, 22/10): teste de fumaça ok (1º áudio p50 2,0 s); "quais são as
  notícias de hoje?" respondida em 1,5 s e "tem alguma notícia de tecnologia?" em 1,9 s, do fim da
  fala ao 1º áudio; "fara do PHP" (STT) virou "Você quis dizer falha no PHP?".

### Teste no Windows (22/10, 12 falas, mic e caixas do notebook)

"Muito bom". Notícias de hoje, "e de tecnologia", "e na Folha", Tecnoblog ("Tecna Blog" no STT),
"Regrângio do Sul", "de economia" e esporte: ferramenta e argumentos certos em todas, e tudo o que
ela disse estava nas manchetes (conferido no `server.log`). Duas leituras frouxas: "clubes do
Brasileirão acumulam compromissos em atraso" virou "atrasos de jogos"; "trabalhadora grávida impedida
de pausar para vomitar" virou "impedida de pausar atendimento". Muro de Berlim sem ferramenta, certo.
1º som 1,9–2,1 s em 11 de 12 falas (ferramenta 1–266 ms); Muro de Berlim 3,2 s porque ela não abriu
com a frase curta (1º token 1,65 s, a 1ª frase inteira saiu aos 2,7 s).
O que ele sentiu como "travou": na resposta do RS, o barge-in disparou duas vezes com a **voz dela**
voltando pelo microfone (notas 0,89 e 0,96; transcrições "e outros destaques em Google Cloud"):
ela pausou ~2–3 s em cada e retomou. É o ponto pendente do barge-in com as caixas do notebook (AEC
tirou 6,3 dB, eco a −29 dBFS no mic); com o headset não acontece.

### Visto no teste: conhecimento geral depois de um histórico

No mesmo teste, "Quem ganhou a Copa do Mundo de 2002?" saiu "A Coreia do Sul e a Itália". Medido
com o `tools_bench` (5 vezes cada): sem histórico, Brasil 5/5; depois de clima e tablespaces, 1/5;
depois de uma conversa sem ferramenta nenhuma, 3/5; depois de notícias, 0/5 a 4/5 conforme a troca.
Não é das notícias: o modelo de 4B hesita nesse fato (Brasil ou Alemanha) e qualquer histórico o
desequilibra. Fica para acompanhar com o precision_bench com histórico; não foi corrigido aqui.

### Alternativas consideradas

| alternativa | por que não |
|---|---|
| Só seções, sem busca por tema | "tem notícia sobre a falha do PHP?" e "conta mais sobre essa" ficariam sem resposta |
| Mandar o tema para um buscador | é o item 3 da Fase 10 (SearXNG); aqui a busca local basta e não expõe o tema |
| Resumo em todas as manchetes | ~2× os tokens no turno, para algo que ela não fala; o resumo vem só na busca por tema |
| Feed do ge para esporte | quase só páginas de jogo; a Folha esporte é notícia |

## Consequências

**Positivas**

- Manchetes por voz das três fontes, por seção, fonte ou tema, sem chave e sem custo.
- Acompanhamentos ("e de tecnologia?", "e na Folha?", "conta mais sobre essa do PHP") consultam de
  novo, e o cache de 5 min responde em milissegundos.

**Negativas / riscos**

- "Mais recentes" não é "mais importantes": os feeds são cronológicos, e uma manchete de cultura
  pode vir antes de política.
- A busca por tema só acha o que está nos feeds (100 itens por feed, de horas a poucos dias); time
  de futebol, por exemplo, quase nunca aparece no feed de esporte da Folha.
- Prefixo do prompt +147 tokens (2558 → 2705).
- Depende do formato dos feeds; se um mudar, a fonte some do resultado com um aviso no log.
