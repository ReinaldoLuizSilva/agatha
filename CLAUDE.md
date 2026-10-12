# Agatha — assistente virtual por voz

Projeto pessoal do Reinaldo (GitHub: ReinaldoLuizSilva). Assistente de voz estilo Jarvis/Alexa,
rodando 100% local num servidor caseiro. Objetivo: respostas rápidas, precisas e úteis no dia a dia
e no trabalho (infra, Oracle, Zabbix). Também é peça de portfólio Cloud/DevOps.

## Como trabalhar comigo

- Responda e comente sempre em português (pt-BR). Código, nomes de arquivos e commits podem ser em inglês.
- Explique o que um comando faz antes de rodar qualquer coisa que altere o sistema.
- Toda decisão de arquitetura relevante vira um ADR em `docs/adr/` (formato: contexto, decisão, consequências).
- Commits pequenos, mensagens no padrão Conventional Commits.
- Não avance para uma nova fase sem eu aprovar.

## Servidor: agatha-srv (192.168.1.25)

- Oracle Linux 10.2, kernel UEK 8 (6.12.x), boot Legacy BIOS (sem Secure Boot)
- Intel i5-7400 (4 núcleos, sem HT, AVX2), 16 GB DDR4
- GPU: NVIDIA GTX 1050 Ti, 4 GB VRAM, arquitetura Pascal
- Discos: `/` 70 GB, `/home` 200 GB, `/dados` ~924 GB
- Usuário de serviço: `agatha` (não usar root para rodar o projeto)
- Diretórios de runtime: `/dados/agatha/{models,data,logs}` — modelos e dados NUNCA vão para o git

## Restrições críticas de GPU (não violar)

- Driver NVIDIA fixado no branch **580** (repo `epel-nvidia-580` do negativo17). O branch 590+ não suporta Pascal.
- Não adicionar repos NVIDIA que tragam driver 590+, nem usar os módulos open da NVIDIA (não suportam Pascal).
- CUDA **12.x** para compilar/rodar (o `nvidia-smi` mostra "CUDA 13.0", mas isso é só o máximo do driver; o toolkit 13 não suporta Pascal).
- Orçamento de VRAM: 4 GB. STT (faster-whisper `small`, int8) ~0,4 GB (ADR-002) + LLM Gemma 4 E4B Q4_K_M com
  ctx 4096 ~3,27 GB (ADR-004) = pico de 3,66 GB. Sobra ~0,4 GB: qualquer coisa nova na GPU precisa ser medida antes.
- Kernel UEK atualizado exige `kernel-uek-devel` da mesma versão para o DKMS recompilar.
- GPU em container: NVIDIA Container Toolkit 1.20.1 (repo oficial `libnvidia-container`, que só tem o toolkit,
  sem driver), CDI em `/etc/cdi/nvidia.yaml` (regenerado por `nvidia-cdi-refresh.path`), SELinux
  `container_use_devices=on`. Uso: `podman run --device nvidia.com/gpu=all ...` (ver `deploy/README.md`).
- Docker CE também está instalado no host (estudo de Kubernetes do Reinaldo, fora do projeto). Não mexer; a Agatha usa Podman.

## Decisões já tomadas

1. **100% local** por custo. Nada de API paga de LLM. Migrar para cloud talvez no futuro.
2. **Cliente-servidor**: o servidor (agatha-srv) roda núcleo, STT, LLM e TTS. O cliente roda no PC
   Windows do Reinaldo: captura de áudio, wake word e reprodução. Comunicação via WebSocket na LAN.
3. **Containers com Podman + NVIDIA Container Toolkit**, usando imagens `nvidia/cuda:12.x`. Host fica só com o driver.
4. O diferencial do projeto é o **núcleo próprio** (orquestração, contexto, memória, ferramentas).
   Componentes de base são reaproveitados: openWakeWord, Silero VAD, faster-whisper, llama.cpp, Piper.
5. Meta de performance: < 2 s entre o fim da fala e o início da resposta. Streaming em todo o pipeline.
   Latência é medida desde o primeiro dia (pasta `bench/`).

## Operação (agatha-srv)

- Subir/derrubar tudo: `deploy/up.sh` / `deploy/down.sh` (pod `agatha`: `agatha-llm` + `agatha-server`).
- Logs: `podman logs -f agatha-server`. Mudou código do servidor → rebuild da imagem (ver `deploy/README.md`).
- Zabbix de laboratório: `deploy/zabbix/up.sh` / `down.sh`; frontend em :8081 (senha do Admin em
  `/dados/agatha/lab/zabbix/admin-password`). De dentro dos containers, o host é `host.containers.internal`.
- Oracle de laboratório: `deploy/oracle/up.sh` / `down.sh`; PDB FREEPDB1 :1521 (fechada no firewall);
  senhas em `/dados/agatha/lab/oracle`. RAM total dos laboratórios ~2,4 GB.
- Benchmarks de GPU e testes de STT precisam do pod parado (VRAM fica em ~3,66 de 4 GB).
- Cache do LLM: o prefixo do prompt (sistema + histórico) não pode mudar entre turnos (ADR-007).

## Estrutura do repositório

```
agatha/
├── server/    # núcleo, STT, LLM, TTS (roda no agatha-srv)
├── client/    # captura de áudio + wake word (roda no Windows)
├── tools/     # integrações: Zabbix, Oracle, GitHub…
├── deploy/    # systemd, containers (Podman)
├── bench/     # medições de latência
└── docs/adr/  # registro de decisões de arquitetura
```

## Status

- [x] Fase 0: servidor preparado (driver 580 OK, `nvidia-smi` funcionando, usuário `agatha`, diretórios em /dados)
- [x] Fase 0: repositório no GitHub clonado em `~/agatha`
- [x] Fase 0: esqueleto — estrutura de pastas, README, `.gitignore` e ADR-001 (`docs/adr/0001-arquitetura-inicial.md`)
- [x] Fase 1: loop servidor ouve → transcreve → imprime (PT-BR). Cliente Windows → WebSocket → Silero VAD →
      faster-whisper `small` na GPU (ADR-002, ADR-003). Fim da fala → texto ≈ 0,85–0,9 s com a voz do Reinaldo.
      Pendências levadas para a Fase 2:
      - testar jargão (Zabbix, Oracle); se errar, usar `AGATHA_STT_INITIAL_PROMPT` (plano B do ADR-002)
      - STT ocasionalmente ~2× mais lento em palavra curta isolada: medir fallback de temperatura vs. clock da GPU
      - pausas > 500 ms no meio da frase a quebram em duas (`min_silence_ms`); revisar o endpointing
      - medir STT e LLM juntos na GPU (VRAM e disputa)
- [x] Fase 2: conversa ponta a ponta com streaming e medição de latência. LLM Gemma 4 E4B (ADR-004), TTS Piper
      "dii" (ADR-005), protocolo v2 half-duplex (ADR-006), ajustes de latência (ADR-007), tudo num pod Podman
      (`deploy/up.sh`). Teste final do Reinaldo no Windows (14 perguntas reais, com jargão): sucesso.
      Fim da fala → primeiro som (cliente): perguntas curtas ~1,7 s; perguntas longas (10–20 s de fala) 2,3–2,9 s;
      mediana da sessão 2,37 s. A meta de 2 s vale para comandos curtos; pergunta longa custa mais STT
      (1,0–1,4 s com VAD) e mais prompt no LLM (+0,5–0,65 s).
      Pendências levadas para a Fase 3:
      - vocabulário do STT: incluir host/hosts ("roaches", "roxas"), Grafana, PostgreSQL, housekeeping, template
      - latência de perguntas longas: avaliar transcrição parcial durante a fala
      - fala descartada se ele continua falando depois que a resposta começa (half-duplex; barge-in no futuro)
      - respostas inventadas quando a pergunta chega mal transcrita (limite do 4B; ferramentas ajudam)
      - voz "dii" provisória (ele quer uma mais limpa)
- [ ] Fase 3: wake word "Agatha", memória de contexto, primeiras ferramentas — **em andamento**:
      feito: vocabulário do STT, wake word pela transcrição + modo conversa 20 s (ADR-008), ferramentas via
      tool calling com exemplos no prompt + calculadora (ADR-009), memória explícita guardar_fato/esquecer em
      SQLite (ADR-010), correção do STT sem VRAM em fala ruidosa, Zabbix 7.0 de laboratório (pod `zabbix` +
      agent2 em container, `deploy/zabbix/`) com ferramentas somente leitura zabbix_problemas/zabbix_host
      (ADR-011), Oracle 23ai Free de laboratório (container `oracle-free`, `deploy/oracle/`) com ferramentas
      somente leitura oracle_tablespaces/oracle_sessoes (ADR-012). Falta decidir: fechar a fase ou fazer os
      opcionais (openWakeWord — caminho para aperfeiçoar a wake word, decisão do Reinaldo; transcrição parcial
      para perguntas longas).
      Atenção ao contexto: prefixo fixo do prompt tem ~1670 tokens de 4096 com 7 ferramentas (2283 com a
      memória cheia); cada ferramenta nova cresce o prompt e o tempo de pré-aquecimento (~6 s).
      **Teste do Reinaldo no Windows em 12/10 (15 falas): tudo funcionou** (wake word, calculadora com contexto,
      memória guardar/consultar/esquecer, Zabbix, Oracle, conceito sem ferramenta). 1º som ~1,8–2,1 s na maioria.
      Pendências desse teste, resolvidas em 12/10 (pod já com a versão nova; falta o Reinaldo testar no Windows):
      1. lentidão de "que horas são" (3,5 s) e "o que você lembra de mim" (3,3 s): o modelo às vezes repetia o
         carimbo "[segunda-feira, …]" antes de responder (~25 tokens, ~1,4 s; o filtro só escondia da fala).
         Não era o pré-aquecimento. Correção: `logit_bias` proíbe "[" (llm.py)
      2. "esquece isso": regra do `esquecer` diz que "isso" é o último fato guardado (bench: 0/5 → 9/10); "isso"
         deixou de segurar a fala como incompleta
      3. RAM no Zabbix: o template do 7.0 usa a chave `vm.memory.util`, não `vm.memory.utilization` (agent2 ok)
      4. calculadora devolve decimal com vírgula ("62,5" → "vírgula")
      5. nomes no STT: `AGATHA_STT_NAMES` (no `server/.env`: agatha-srv, PRDORA); voz sintética 0/10 → 7/10
      **2º teste do Reinaldo no Windows em 12/10 (17 falas):** 1º som mediana 2,1 s, máx 2,5 s (sem os 3,3–3,5 s);
      PRDORA e RAM ok. Falhas corrigidas depois (pod já atualizado, falta testar):
      - "Esqueça essa informação." → pediu o número: exemplo de `esquecer` com referência vaga + regra "use o
        último fato da lista" (bench: casos de memória 29/50 → 52/60)
      - "que horas roda o backup?" chamou zabbix_problemas; "qual é mesmo o nome do meu banco?" chamava
        guardar_fato: perguntas sobre a memória são respondidas direto
      - "agatha-srv" ainda transcrito "Agatha, server" com a voz dele; a busca de host compara com as partes do
        nome ("server" ≈ "srv"), só se apontar um único host
      Não testados com a voz dele: calculadora (62,5; 10/3), tablespaces, alertas do Zabbix.
      Observações (sem correção): pergunta logo após guardar/esquecer fato ainda pode esperar o recálculo do cache
      (7–9 s, ADR-010); "pode esquecer o que eu te falei" depois de outro assunto faz ela perguntar qual;
      conversão tera/giga na calculadora erra ~6/10 (antigo).
      Próximo passo: novo teste curto no Windows e decidir fechar a Fase 3 ou fazer os opcionais.
- [ ] Fase 4: serviço systemd, logs, testes, README final
