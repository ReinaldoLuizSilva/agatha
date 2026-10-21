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
- Laboratórios no systemd do usuário (ADR-017, `deploy/install-labs.sh`, `--uninstall` para tirar): sobem no
  boot e voltam se caírem; ~2,4 GB de RAM fixos. Os `up.sh`/`down.sh` abaixo viram `systemctl --user`.
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
- [x] Fase 5: barge-in com wake word no cliente (fechada em 18/10; ADR-014, ADR-015, ADR-016).
      Coleta de 141 falas do Reinaldo (`python -m agatha_client.record`) em /dados/agatha/data/wakeword/gravacoes;
      detector openWakeWord treinado só com a voz dele (`client/wakeword/train.py`, CPU) →
      /dados/agatha/models/wakeword/{agatha,melspectrogram,embedding_model}.onnx (o cliente copia para
      `client\models`); serve SÓ para o barge-in, a chamada normal continua pela transcrição (opção A do ADR-014).
      AEC linear próprio (`client/agatha_client/aec.py`): o AEC3 do WebRTC apagava a voz dele. Cliente com
      `DuplexAudio` (mic + caixas num stream a 16 kHz, servidor reamostra), atraso do eco medido pela fala dela
      (GCC-PHAT), detector só durante a resposta (limiar 0,6), ao ouvir "Agatha" PAUSA e manda ~1,5 s de áudio de
      antes do nome; servidor confirma pela transcrição em até 2 s: stop ("Agatha, para", sem LLM), pergunta nova
      ou resume. `--no-barge-in` volta ao modo antigo; sem o extra `[barge-in]` ou os modelos, o cliente avisa.
      Teste real em 13/10 (ADR-016, "Teste real"): o Windows/Realtek já cancela o eco no microfone e apaga a voz
      dele junto quando o mic fica perto das caixas; com o mic afastado funcionou ("Agatha para" pausou aos 2,4 s).
      Cada resposta imprime `ⓘ barge-in:` (nota máx do detector, eco antes/depois do AEC) para diagnóstico.
      Decisões do Reinaldo: treino no agatha-srv, sem serviço externo; guardar o áudio das falas do dia a dia para
      retreinar NÃO está autorizado (perguntar antes); barge-in "melhora aos poucos".
      Pendências (melhorar aos poucos):
      - testar com as melhorias de áudio do microfone desligadas no Windows (o AEC linear assume o eco)
      - medir nome → silêncio (meta < 300 ms) e falsos disparos durante a fala dela; CPU do cliente agora sai
        na linha `ⓘ barge-in` (ms por bloco de 32 ms): conferir no próximo teste
      - feito em 18/10: "Agatha, para" com ela calada não vai mais ao LLM (`wake.is_idle_stop`; "esquece" e
        "deixa" ficam de fora, são da memória)
      - varredura de `dtd_ratio` do AEC nas gravações reais (ERLE ~5,7 dB contra ~13,5) — liberada (memtest ok)
      - voz a 16 kHz no modo barge-in perde agudos (alternativa: stream a 48 kHz)
      ATENÇÃO: treino e testes pesados sempre com `systemd-run --user --scope -p MemoryMax=5G -p
      MemorySwapMax=0 nice -n 19` — um teste sem teto jogou o LLM no swap (1º áudio 34 s).
      HARDWARE: kernel panic `Bad pagetable` em 12/10, 20:35 (1 bit trocado numa PTE; dump em
      /var/crash/127.0.0.1-2026-10-12-20:36:18). **Memtest86+ 7.00 em 18/10: 2 passagens, 0 erros** (entrada BLS
      "Memory test", `grub2-reboot <machine-id>-0-memtest86+`). Testes pesados liberados, sempre com teto de
      memória. O aviso `timerqueue.c:38` aparece em todo boot no mesmo ponto (~11 s): bug fixo de driver/kernel,
      não memória. Se outro panic aparecer, rodar o memtest por mais tempo (noite inteira) antes de culpar o software.
- [x] Fase 6: precisão (fechada em 18/10; ADR-018).
      Benchmarks: `bench/precision_bench.py` (falas reais como o STT entregou, rotuladas: responder / confirmar /
      ferramenta / não consigo; `--rescore`) e `bench/stt_jargon_bench.py` (40 frases de jargão na voz dele em
      /dados/agatha/data/stt/gravacoes-stt, pipeline do servidor no CPU, com teto de memória).
      LLM: prompt v5 (conceito se responde; "não sei" só para estado que nenhuma ferramenta mostra; termo
      estranho → confirma o termo parecido; glossário; ferramentas dão acesso real) → 53/90 para 75/90, invenções
      6 → 0; tools_bench 94/105; prefixo 1784 → 1983 tokens. v6/v7 (corrigir "você quis dizer" em excesso)
      pioraram, ficou a v5. `AGATHA_ZABBIX_DEFAULT_HOST=agatha-srv` ("o servidor").
      STT sem mudança: vocabulário em lista (produção) 81% do jargão; frase, hotwords, lista ampliada e volume
      normalizado não ganham; beam 5 +1 termo e +13% de tempo. Erros restantes são acústicos (Whisper small no
      limite; modelo maior não cabe na VRAM). Fim de fala segue em 500 ms (2 cortes em 74 falas, ambos juntados).
      Agatha sem swap (MemorySwapMax=0, ADR-013 item 8).
      Para acompanhar no uso: o registro de turnos mostra se as invenções sumiram de verdade; "você quis
      dizer" às vezes aparece com sigla conhecida (STT); o STT erra ~1 em 5 termos de jargão.
- [x] Fase 7: voz nova (fechada em 19/10; ADR-019): a voz continua a dii. Teste de audição
      (`bench/voice_samples.py`): dii a 16/22 kHz e com menos ruído soaram iguais; Kokoro pf_dora bem mais natural.
      Kokoro na GPU não roda (onnxruntime-gpu do PyPI é CUDA 13; o build CUDA 12 falha no cuDNN 9.27 na LSTM,
      Pascal obsoleto desde o cuDNN 9.9) e não caberia na VRAM. No CPU, no ar, 1º som ~2,9 s (2,2–3,8) contra
      ~2,0 s: o Reinaldo escolheu **ficar com a dii**. Kokoro fica como opção (`AGATHA_TTS_ENGINE=kokoro` no
      server/.env; padrão piper). Pronúncia: horas no feminino ("vinte e uma e dois") e siglas Oracle (RMAN,
      AWR, ASH, ASM, CDB, PDB, SPFILE) em `text.ACRONYMS`, ajustáveis de ouvido. Plano B guardado: destilar a
      pf_dora numa voz Piper (noites de treino na GPU, qualidade incerta) — só com aprovação.
- [x] Fase 8: mais ferramentas (fechada em 19/10; ADR-020) — tudo no ar, inclusive o GitHub (token fine-grained
      configurado e testado em 19/10; ele expira: quando vencer, gerar outro). `oracle_banco` (aberto, archivelog, papel/Data Guard, versão, no ar há, locks) e
      `oracle_backups` (últimos jobs do RMAN); duas conexões: AGATHA_RO na PDB e o usuário comum C##AGATHA_RO
      no CDB (a PDB não enxerga os backups). Laboratório em ARCHIVELOG com backup RMAN diário às 02:30
      (`deploy/oracle/backup.sh`, `oracle-backup.timer`, FRA de 10 GB). `zabbix_problemas` com
      `severidade_minima`. `github_repo` (commits, PRs, issues, última CI) liga com `AGATHA_GITHUB_TOKEN` +
      `AGATHA_GITHUB_REPOS` no server/.env (token fine-grained só leitura, criado pelo Reinaldo). Sem exemplos de
      conversa nas novas; dados em texto, não booleano (o `archivelog: true` era ignorado). Prefixo 2311 tokens
      (limite prático: antes de outra ferramenta, enxugar regras ou juntar). tools_bench 118/132, precision
      95/111, invenções 0, 1º áudio p50 ~2,07 s. GitHub com 1 nova tentativa se a conexão falhar (a rede oscilou
      no 1º teste). Ponto fraco: pergunta sobre fato da memória às vezes chama uma ferramenta antes (ex.:
      "nome do banco de produção" → oracle_banco), só atrasa ~1 s.
- [x] Fase 9: cliente funcional (fechada em 21/10; escopo aprovado em 19/10; ADR-014, ADR-021).
      B) segundo plano: **feito e testado no Windows** (ADR-021): `pythonw -m agatha_client --tray` (ícone por
      estado, pausar mic = manda silêncio, barge-in on/off, abrir log, sair), log em %LOCALAPPDATA%\Agatha,
      `--autostart ligar|desligar`, uma instância só (mutex). Extra `[tray]` (pystray, Pillow); cliente 0.5.0.
      A) barge-in: diagnóstico de 20/10 (`--barge-debug`, `--duplex-latency`, `python -m agatha_client.wakeword
      <wav>`): ele usa **headset** ("Fones de ouvido" + "Microfone Externo" = mic do headset), não há eco; a voz
      dele chega ao mic durante a fala dela, mas o detector quase nunca dispara. Causa: o detector foi treinado
      só com o **mic do notebook** (12/10); com o mic do headset acerta 3/6 gravações (era 90%). Quando dispara,
      pausa 453 ms após o começo da fala + ~96 ms de saída (ok). Mic do headset baixo (picos −31 a −40 dBFS) e
      com um "tranco" de −6 dBFS ao abrir o stream. Corrigido no caminho: disparo não espera mais a medição do
      atraso do eco (com fone travava ~6 s); nota NaN agora avisa.
      Retreino de 20/10 (ADR-014, "Retreino com o headset"): 94 falas com o headset em
      /dados/agatha/data/wakeword/gravacoes-headset. Causa real: o `train.py` calculava os embeddings no clipe
      inteiro e o cliente em fluxo; no silêncio quase digital do headset (−90 dBFS) eles divergem (avaliação
      91–100%, cliente ~45%). Agora o treino é em fluxo; modelo novo no ar no servidor (`agatha.onnx`; o antigo
      em `agatha-20261012.onnx`): headset perto 44% → 100%, variado 48% → 95%, falsos/h validação 21,6 → 15,7.
      O gravador abre o mic a cada fala e o do headset só acorda ~0,84 s depois, com degrau de −10 dBFS: o
      treino filtra/corta; para gravar mais, o certo é o gravador manter o stream aberto (não feito).
      Teste no Windows em 20/10 (headset, modelo novo): barge-in 2/2 (disparo 485 ms após o começo da fala),
      0 disparos à toa; "muito melhor". Seguir medindo no uso (amostra pequena).
      Visto no teste: STT entregou "OWR" (AWR) e ela inventou "Oracle WebLogic Repository". Corrigido em 20/10
      (ADR-018, "Revisão de 20/10"): só inventava com histórico (sozinha 5/5 confirma); `precision_bench
      --history`; prompt **v8** (sigla de Oracle/Zabbix fora do glossário → confirma) no ar: com histórico 93 →
      95/114, invenções 2 → 0; sem histórico 97 → 96; tools_bench 119/132; prefixo 2354 tokens. STT sem mudança.
      2º teste no Windows (21/10, 12 falas): interrupções 3/3; 1 disparo à toa (ruído sem fala, descartado
      pela transcrição vazia, ela continuou). Somando: 5/5, 1 à toa. "paras", "espera", "espere" viraram
      palavras de parar (iam ao LLM). 1ª resposta da sessão às vezes mais lenta (LLM 2,35 s, depois ~1,3–1,5 s).
      Melhorar aos poucos (não bloqueiam): varredura do AEC (só com as caixas do notebook); gravador mantendo o
      stream aberto (só para gravar outra rodada); falsos disparos em ruído sem fala; "Fato guardado. Já estava
      na memória." (frase contraditória ao guardar um fato repetido).
- [ ] Fase 10: integração com a internet — **em andamento** (escopo aprovado em 21/10).
      Ordem: 0) enxugar o prompt (2354 tokens; meta ~2000 sem piorar tools_bench nem precision_bench com e sem
      histórico); 1) clima (Open-Meteo, sem chave); 2) notícias por RSS (G1, Folha e Tecnoblog); 3) busca na web
      com SearXNG no agatha-srv, restrita a documentação oficial para dúvida técnica. Cada uma com casos no
      tools_bench, latência medida e ADR; se o prompt apertar, juntar clima e notícias numa ferramenta.
      Decisões do Reinaldo: cidade do clima na memória (fato 9: "A cidade do Reinaldo para o clima é Novo
      Hamburgo (RS)", gravado em 21/10); ok que cidade, buscas e pedidos de notícia saiam para a internet (o
      processamento continua local).
      0) prompt fica na v8 (v9/v10 cortavam 108–157 tokens e pioravam o tools_bench; ADR-018); maior prompt real
      visto 2887/4096. 1) **clima no ar** (21/10, ADR-022): `clima(cidade)` no Open-Meteo, cidade pela memória,
      cache de 10 min; tools_bench clima 18/21 (falhas eram da régua), antigos 114/132, precision 95 e 93/114;
      prefixo 2488 tokens. DNS do pod `timeout:1 attempts:2` (os DNS IPv4 do provedor perdem ~1 em 5: 5–8 s).
      Próximo: ele testa o clima no Windows; depois notícias (RSS).
