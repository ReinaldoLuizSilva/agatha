# ADR-022: Internet, parte 1 — clima pelo Open-Meteo (e DNS do pod)

- **Status:** Aceito
- **Data:** 2026-10-21
- **Fase:** 10 (integração com a internet; escopo aprovado em 21/10)

## Contexto

A Fase 10 traz informação de fora para a Agatha: clima, notícias (G1, Folha, Tecnoblog) e busca na
web. O processamento continua local; o Reinaldo autorizou que a cidade, os termos de busca e os
pedidos de notícia saiam para a internet.

Antes das ferramentas, a meta era enxugar o prompt (2354 tokens). As duas tentativas economizavam
108–157 tokens e pioravam a escolha de ferramentas de forma medida (ADR-018, "Tentativas de
enxugar"): ficou a v8. O maior prompt visto no uso era 2887 de 4096 tokens, e o servidor já corta o
histórico se o contexto estourar (nunca aconteceu). Cada ferramenta nova entra enxuta e sem
exemplo de conversa, como as da Fase 8.

## Decisão

1. **Ferramenta `clima(cidade)`** ([`tools/weather.py`](../../server/agatha_server/tools/weather.py)):
   Open-Meteo, grátis e sem chave. Devolve o tempo agora (temperatura, sensação, condição) e 3 dias
   (mínima, máxima, chance de chuva, condição), em texto e inteiros, em poucos tokens. Os códigos
   WMO viram palavras ("garoa fraca", "pancadas de chuva").
2. **A cidade vem do LLM**, da pergunta ou da memória: o fato "A cidade do Reinaldo para o clima é
   Novo Hamburgo (RS)" foi gravado a pedido dele. O parâmetro é obrigatório e a regra diz "se não
   souber a cidade, pergunte". O estado (entre parênteses, depois de vírgula ou hífen, sigla ou
   nome) separa as homônimas; sem ele, vale a mais populosa do Brasil.
3. **Cache:** coordenadas da cidade por todo o processo e previsão por 10 min (um "e amanhã?" logo
   depois sai em 1 ms). Cliente HTTP numa thread, conexões reaproveitadas e até 3 tentativas, como o
   do GitHub (ADR-020). `AGATHA_CLIMA=0` desliga.
4. **DNS do pod com `timeout:1 attempts:2`** (`deploy/quadlet/agatha.pod`). Medido em 21/10: os DNS
   IPv4 do provedor (172.16.255.16 e .17) perdem ~1 consulta em 5; com o timeout padrão de 5 s, a
   resolução seguinte a uma perdida levava 5–8 s, no contêiner e no host. Provável causa também das
   falhas de conexão com o GitHub da Fase 8. O host fica como está (mexer nele pede root).

## Resultados

- API real a partir do agatha-srv: 1ª consulta ~1,7 s (duas conexões novas com TLS até a Alemanha,
  ~220 ms de ida e volta); outra cidade ~435 ms; mesma cidade 1 ms.
- `tools_bench`, 3 rodadas: casos de clima 18/21 (as 3 falhas eram da régua do caso, "o tempo hoje"
  respondeu certo com a temperatura atual em vez da máxima; corrigida). "O que é o El Niño?" e
  "diferença entre clima e tempo" não chamam a ferramenta; "E em Curitiba?" troca a cidade. Casos
  antigos 114/132 (v8: 119 e 117); só a conta de tera/giga, ponto fraco conhecido, mudou além do ruído.
- `precision_bench`: 95/114 sem histórico (v8 96), 93/114 com histórico (v8 95), invenções 0.
- Prefixo 2354 → 2488 tokens (esquema 90, regra ~30, fato da cidade ~20).
- Ponta a ponta: "Vai chover amanhã?" → `clima("Novo Hamburgo (RS)")` → "Amanhã tem chance de garoa
  fraca. A previsão indica 39% de chance de chuva." (na fala, "trinta e nove por cento").

### Alternativas consideradas

| alternativa | por que não |
|---|---|
| Cidade fixa no `server/.env` | o Reinaldo preferiu a memória; assim ela muda por voz e vale para "e em Curitiba?" |
| Parâmetro `quando` (agora, hoje, amanhã) | mais uma escolha para o modelo errar; 3 dias cabem em ~100 tokens |
| OpenWeatherMap, INMET | chave de API ou API instável; o Open-Meteo não pede cadastro |
| Resolver o DNS no host (`ipv4.dns-options` no NetworkManager) | pede root; a opção do pod resolve a Agatha sem tocar no sistema |

## Consequências

**Positivas**

- Clima por voz com a cidade dele, sem chave e sem custo.
- O DNS do pod deixa de travar 5–8 s nas consultas externas (clima, GitHub e as próximas).

**Negativas / riscos**

- A 1ª consulta depois de um tempo parada custa ~1,7 s (o "Deixa eu ver" cobre).
- Prefixo maior: 2488 tokens. Notícias e busca ainda vão crescer; acompanhar o maior prompt real e,
  se passar de ~3500, cortar exemplos de conversa ou juntar ferramentas.
- Depende de um serviço externo gratuito: se sair do ar, a ferramenta responde com o erro e ela diz
  que não conseguiu consultar.
