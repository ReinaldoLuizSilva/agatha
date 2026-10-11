# ADR-001: Arquitetura inicial da Agatha

- **Status:** Aceito (item 5 e a linha STT da tabela substituídos pelo [ADR-002](0002-stt-faster-whisper-small-gpu.md); wake word no cliente substituída pela detecção na transcrição no [ADR-008](0008-wake-word-pela-transcricao.md))
- **Data:** 2026-10-11

## Contexto

A Agatha é um assistente virtual por voz para uso pessoal e profissional (infraestrutura, Oracle,
Zabbix). Ela também serve de peça de portfólio Cloud/DevOps. As restrições que orientam as decisões:

- **Custo:** o projeto é pessoal, então APIs pagas de LLM, STT ou TTS estão fora.
- **Hardware disponível:** um servidor caseiro (`agatha-srv`) com Intel i5-7400 (4 núcleos, AVX2),
  16 GB de RAM e uma NVIDIA GTX 1050 Ti com 4 GB de VRAM (arquitetura Pascal).
- **Limitações da GPU:** a geração Pascal só é suportada pelo driver NVIDIA até o branch 580 e pelo
  CUDA Toolkit 12.x. O branch 590+, os módulos open da NVIDIA e o CUDA 13 não suportam Pascal.
- **Experiência de uso:** um assistente de voz só é útil se responder rápido. A sensação de
  conversa se perde quando a espera passa de uns 2 segundos.
- **Uso real:** o usuário trabalha num PC Windows, onde ficam o microfone e o alto-falante.

## Decisão

1. **Execução 100% local.** Nenhuma API paga de LLM. A migração para cloud pode ser reavaliada no
   futuro, com um novo ADR.

2. **Arquitetura cliente-servidor na LAN.**
   - **Servidor (`agatha-srv`):** núcleo, STT, LLM e TTS.
   - **Cliente (PC Windows):** captura de áudio, detecção da wake word e reprodução.
   - **Comunicação:** WebSocket na rede local, com áudio e texto trafegando em streaming.

3. **Containers com Podman + NVIDIA Container Toolkit**, usando imagens base `nvidia/cuda:12.x`.
   O host mantém só o driver NVIDIA (branch 580, repositório `epel-nvidia-580`). Todo o resto
   (CUDA, bibliotecas, modelos de runtime) fica nos containers.

4. **Núcleo próprio, componentes de base reaproveitados.** O valor do projeto está no núcleo:
   orquestração do pipeline, gerenciamento de contexto, memória e ferramentas. Para as peças de
   base, usamos projetos open source consolidados:

   | Função     | Componente     | Onde roda          |
   |------------|----------------|--------------------|
   | Wake word  | openWakeWord   | Cliente (Windows)  |
   | VAD        | Silero VAD     | Servidor           |
   | STT        | faster-whisper | Servidor (CPU)     |
   | LLM        | llama.cpp      | Servidor (GPU)     |
   | TTS        | Piper          | Servidor           |

5. **Orçamento de VRAM (plano inicial):** LLM de 3–4B parâmetros quantizado na GPU e STT na CPU,
   para caber nos 4 GB. Só mudamos essa divisão depois de medir.

6. **Meta de performance:** menos de 2 s entre o fim da fala do usuário e o início da resposta
   falada. Para chegar lá, há streaming em todo o pipeline (STT → LLM → TTS), e a latência é medida
   desde o primeiro dia, com scripts e resultados na pasta `bench/`.

## Consequências

**Positivas**

- Custo recorrente zero, e os dados (voz, conversas, credenciais de ferramentas) não saem da rede local.
- Separar cliente e servidor deixa o PC Windows leve e concentra o processamento pesado num só lugar.
- Containers isolam as versões de CUDA e bibliotecas, e o host continua simples e estável.
- Reaproveitar componentes maduros deixa o esforço concentrado no núcleo, que é o diferencial.
- Medir latência desde o início evita descobrir tarde demais que a arquitetura não atende à meta.

**Negativas / riscos**

- **Qualidade limitada pelo hardware:** um LLM de 3–4B quantizado responde pior que modelos
  grandes ou APIs comerciais, principalmente em raciocínio e em PT-BR.
- **GPU presa a versões antigas:** driver 580 e CUDA 12.x. Atualizações de kernel UEK exigem o
  `kernel-uek-devel` correspondente para o DKMS recompilar o módulo. Não dá para adicionar
  repositórios que tragam o driver 590+.
- **4 GB de VRAM** limitam o tamanho do modelo e o contexto, e impedem rodar STT e LLM juntos na GPU
  sem medição cuidadosa.
- **Dependência da LAN:** o assistente só funciona com cliente e servidor na mesma rede (acesso
  remoto exigiria VPN ou outro ADR).
- **Mais peças móveis:** cliente, servidor, protocolo WebSocket e vários containers aumentam a
  complexidade de deploy e depuração em comparação com uma solução monolítica.
