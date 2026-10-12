# ADR-010: Memória explícita (fatos guardados por pedido, em SQLite)

- **Status:** Aceito
- **Data:** 2026-10-11

## Contexto

Até aqui, a Agatha só lembrava o que estava no histórico da conversa atual (ADR-007), que se
perde quando a conexão fecha. O Reinaldo quer que ela guarde fatos sobre ele e o trabalho dele,
como o padrão de nomes dos hosts do Zabbix ou o nome do banco de produção, e decidiu começar pela
**memória explícita**: ela só guarda o que ele pedir. Guardar coisas por conta própria (memória
automática) fica para depois.

Restrições:

- o cache do prompt no llama.cpp depende de o começo do prompt não mudar (ADR-007);
- o contexto é de **4096 tokens**, e o começo fixo do prompt já ocupa ~1160 com as regras e os
  exemplos de três ferramentas (ADR-009);
- o modelo de 4B precisa de exemplos para usar ferramentas de forma confiável (ADR-009).

## Decisão

1. **Armazenamento:** SQLite em `/dados/agatha/data/memory/agatha.db`, fora do git e montado com
   escrita no container, em `/data`. Uma tabela de fatos com texto, chave normalizada (para
   detectar repetição, ignorando acentos, pontuação e maiúsculas) e data. Há um comando de
   administração: `python -m agatha_server.memory list|add|forget`.

2. **Duas ferramentas** (ADR-009), cada uma com regra e exemplo próprios:
   - `guardar_fato(fato)`: grava um fato novo, numa frase curta, na 3ª pessoa. Repetidos não são
     duplicados;
   - `esquecer(numero)`: apaga pelo número.

   **O nome não é `lembrar`:** em português, "lembrar" também significa "recordar". Com esse nome,
   perguntas como "qual padrão eu uso?" faziam o modelo chamar a ferramenta para regravar um fato
   que já estava guardado.

3. **Os fatos entram numerados no prompt de sistema, logo depois da apresentação da Agatha**, com
   instruções de uso: usar ao responder, contar quando ele perguntar o que ela sabe, apagar pelo
   número e não regravar. No fim de um prompt longo, depois das regras das ferramentas, o modelo
   ignorava os fatos. "O que você sabe sobre mim?" não precisa de ferramenta.

4. **Cache do prompt:**
   - o começo do prompt fica **congelado durante o turno**: uma mudança na memória vale a partir
     do turno seguinte, sem reprocessar o prompt no meio de uma resposta;
   - depois de uma mudança, o cache do novo prompt é recalculado em segundo plano, como no corte
     do histórico (ADR-007).

5. **Limites para caber no contexto:**
   - até **30 fatos** (~700 tokens) de no máximo 200 caracteres;
   - se o llama-server recusar a requisição por falta de contexto (`exceed_context_size_error`),
     o histórico é cortado para os 2 últimos turnos e a requisição é refeita.

6. **O prompt avisa que as primeiras trocas são só exemplos de formato.** Assim o modelo não trata
   como fato os exemplos de memória, como o notebook "rei-note" de um exemplo.

## Consequências

**Medido** no `bench/tools_bench.py`, agora usando o prompt real do servidor (3 rodadas, 24
perguntas, com 3 fatos na memória): **68/72 respostas totalmente certas**, nenhuma chamada de
ferramenta indevida (30/30) e nenhuma menção ao fato fictício do exemplo. Ao vivo, em duas sessões
separadas:

- "lembra que o servidor de testes se chama lab zero um" → `guardar_fato` → "Certo, anotei.";
- numa conexão nova, "qual é o nome do meu servidor de testes?" → "Seu servidor de teste é Lab01.";
- "esquece o nome do servidor de testes" → `esquecer(1)` → a memória ficou vazia.

**Positivas**

- Os fatos valem entre sessões e reinícios, ficam na máquina e podem ser auditados ou editados pelo
  terminal.
- Usar números para esquecer evita apagar o fato errado por semelhança de texto.

**Negativas / limites**

- **Cada mudança na memória invalida o cache inteiro:** o prompt é reprocessado (~4–5 s com o
  prefixo atual). Isso acontece em segundo plano, mas uma pergunta feita logo em seguida espera na
  fila. Medido: "o que você sabe sobre mim?" logo depois de guardar um fato levou ~4 s até o
  primeiro som. Como a memória muda pouco, o custo foi aceito.
- **Referências vagas para apagar** ("apaga aquilo da reunião") falharam em 2 de 3 rodadas, mas
  **do lado seguro**: ela pede para dizer qual fato, em vez de apagar o errado.
- **"O que você sabe sobre mim?"** às vezes lista só parte dos fatos, misturados com o perfil fixo.
- **O prompt cresce com a memória e com cada ferramenta.** Com Zabbix e Oracle, o começo fixo deve
  passar de ~1700 tokens. Pode ser preciso reduzir exemplos, aumentar o contexto (VRAM apertada,
  ADR-004) ou quantizar o cache KV.
- **Memória só explícita:** ela não aprende sozinha com as conversas. A memória automática é uma
  evolução prevista.
