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

- Serviço do systemd do usuário via Quadlet (ADR-013): `deploy/install-systemd.sh` instala `deploy/quadlet/`;
  `deploy/up.sh` / `deploy/down.sh` sobem/derrubam (pod `agatha`: `agatha-llm` + `agatha-server`);
  `systemctl --user status|restart agatha-server`. Linger precisa ser ligado pelo Reinaldo (root).
- Logs: `/dados/agatha/logs/server.log` (rotação) e `turns-AAAA-MM.jsonl` (cada fala/resposta com tempos);
  resumo: `.venv/bin/python bench/turns_report.py --client agatha-client /dados/agatha/logs/turns-*.jsonl`.
  `podman logs -f agatha-server` também serve (o journald não: volátil e sem acesso do usuário).
  Mudou código do servidor → rebuild da imagem + `systemctl --user restart agatha-server` (`deploy/README.md`).
- Teste de fumaça com a Agatha no ar: `.venv/bin/python server/scripts/smoke.py` (sem memória nem laboratórios).
  CI no GitHub Actions (`.github/workflows/tests.yml`): servidor no Ubuntu, cliente no Windows, sem GPU.
- Testes ponta a ponta NUNCA podem mexer na memória real (fatos do Reinaldo em /dados/agatha/data).
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
- [x] Fase 3: wake word "Agatha", memória e primeiras ferramentas (fechada em 12/10). Vocabulário do STT +
      `AGATHA_STT_NAMES` para nomes do ambiente, wake word pela transcrição + modo conversa 20 s (ADR-008), tool
      calling com exemplos no prompt + calculadora (ADR-009), memória explícita guardar_fato/esquecer em SQLite
      (ADR-010), Zabbix 7.0 de laboratório com zabbix_problemas/zabbix_host (ADR-011), Oracle 23ai Free de
      laboratório com oracle_tablespaces/oracle_sessoes (ADR-012). 3 testes no Windows em 12/10; o último, com
      13 falas, passou inteiro: 1º som mediana 1,95 s, máx 2,17 s. `bench/tools_bench.py`: 92/105.
      Atenção ao contexto: prefixo fixo do prompt tem ~1670 tokens de 4096 com 7 ferramentas (~2300 com a
      memória cheia); cada ferramenta nova cresce o prompt e o tempo de pré-aquecimento (~6 s).
      Pontos fracos conhecidos (no bench, sem correção): pergunta sobre um fato logo depois de guardá-lo às vezes
      chama guardar_fato de novo (só um "deixa eu ver" à toa; 3 contraexemplos testados pioravam o esquecer
      vago); "pode esquecer o que eu te falei" depois de outro assunto faz ela perguntar qual; conversão
      tera/giga na calculadora erra ~6/10; pergunta logo após guardar/esquecer espera o recálculo do cache
      (7–9 s, ADR-010).
      Opcionais adiados para depois da Fase 4 (decisão do Reinaldo): openWakeWord no cliente; transcrição
      parcial para perguntas longas.
- [x] Fase 4: serviço systemd, logs, testes, README final (fechada em 12/10). Quadlet no systemd do usuário
      (ADR-013), reinício em falha testado; logs em arquivo + registro de turnos JSONL + bench/turns_report.py;
      teste de fumaça (server/scripts/smoke.py) e CI no GitHub Actions; README final (arquitetura em Mermaid,
      latência por etapa, índice dos ADRs). Linger ligado em 12/10; após reboot a Agatha subiu sozinha (pod
      saudável, VRAM 3,66 GB) e o teste de fumaça passou (fim da fala → 1º áudio p50 1,96 s, máx 2,18 s).
      Repositório fica privado por enquanto; licença (Piper GPL, voz "dii" CC BY-NC-ND, ADR-005) só se um dia
      ficar público. Não levantar de novo antes disso.
- [ ] Fase 5: barge-in com wake word no cliente — **em andamento** (escopo aprovado em 12/10). Plano revisto
      pelo ADR-014: 1 coleta ✔, 2 detectores ✔, 3 cancelamento de eco ✔ (ADR-015), 4 barge-in no cliente e no
      servidor: stream duplex (mic + saída no mesmo stream, atraso medido no início), detector só durante a fala
      dela, ao ouvir "Agatha" PAUSA a fala e o servidor confirma pela transcrição (retoma se não era com ela),
      `interrupt` cancela LLM/TTS, "Agatha, para" sem LLM; meta nome → silêncio < 300 ms. 5 teste real no
      Windows. Ele usa as **caixas de som do notebook**.
      Decisões do Reinaldo: treino no agatha-srv, sem Colab nem serviço externo; opção A do ADR-014 — o
      detector do cliente serve SÓ para o barge-in, a chamada normal continua pela transcrição no servidor
      (ADR-008). Guardar o áudio das falas do dia a dia para retreinar NÃO está autorizado (perguntar antes).
      feito: 1. coleta (`python -m agatha_client.record`, `--redo`): 141 falas em
      /dados/agatha/data/wakeword/gravacoes (manifest.jsonl: vale a última linha de cada id), microfone
      "Microfone Externo (Realtek)" do notebook. 2. benchmark (`bench/wake_bench.py`): STT do servidor 98/83/100%
      (perto/variado/longe), 2,7 falsos/h; Vosk descartado; openWakeWord treinado só com a voz dele
      (`client/wakeword/train.py`, CPU) 90/79/75%, ~1–3 falsos/h → /dados/agatha/models/wakeword/agatha.onnx.
      Sintéticos do Piper não ajudaram (desligados). Bug do hífen ("Agatha-srv") corrigido e no ar.
      3. AEC (ADR-015, `bench/aec_bench.py`): o AEC3 do WebRTC tira ~30 dB mas apaga a voz dele (31% de acerto);
      AEC linear próprio (`client/agatha_client/aec.py`, numpy, filtro persistente, atraso 289−16 ms) tira ~13 dB
      e preserva a voz; detector treinado com eco simulado: 85% (0,5) / 77% (0,9) das interrupções; preço:
      falsos disparos em áudio comum sobem (modelo final 10–23/h na validação; só importa durante a fala dela).
      ATENÇÃO: treino e testes pesados sempre com `systemd-run --user --scope -p MemoryMax=5G -p
      MemorySwapMax=0 nice -n 19` — um teste sem teto jogou o LLM no swap (1º áudio 34 s).
      4. em andamento no branch `wip/barge-in` (12/10): servidor pronto e testado (reamostragem 16 kHz em
      `audio.py`, `interruption_kind` em `wake.py`, `output_rate` no hello, `interrupt` na conversa, estado
      "confirmando" no `app.py`); AEC com detector de fala por cima novo (`dtd_ratio` 4, `dtd_hold_s` 1,5):
      nas gravações reais 85%/85% (0,5/0,9), mas ERLE caiu de ~13 para ~5,7 dB — a varredura de `dtd_ratio`
      8/16/32 (scratchpad `aec_persist.py`) foi interrompida pela queda. Falta: essa varredura, ADR-016 (o
      código já cita), cliente duplex + `barge_in`, depois o passo 5.
      **HARDWARE (12/10, 20:35): kernel panic `Bad pagetable`** — 1 bit trocado (bit 39) numa PTE, padrão de
      RAM com defeito (sem ECC); dump em /var/crash/127.0.0.1-2026-10-12-20:36:18. Até o memtest86+ passar,
      NADA de testes pesados (varredura, treino). Aviso `timerqueue.c:38` no boot: ver se repete (pode ser driver).
