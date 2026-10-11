# ADR-003: Protocolo WebSocket entre cliente e servidor

- **Status:** Aceito
- **Data:** 2026-10-11

## Contexto

O ADR-001 definiu a arquitetura cliente-servidor: o PC Windows captura o áudio e o `agatha-srv`
faz VAD, STT, LLM e TTS, com comunicação via WebSocket na LAN. Falta definir o contrato entre os
dois lados. Os requisitos são:

- **Streaming:** o áudio precisa chegar ao servidor enquanto o usuário fala, e não depois, para
  cumprir a meta de menos de 2 s.
- **Cliente simples:** o cliente roda no Windows e deve ter o mínimo de lógica possível.
- **Medição:** os tempos de cada etapa precisam ser medidos de forma consistente.
- **Evolução:** na Fase 2 o servidor passa a devolver áudio (TTS) e a aceitar interrupção, e na
  Fase 3 entra a wake word. O protocolo precisa crescer sem quebrar.
- **Segurança básica:** o servidor fica exposto na rede de casa, onde há outros dispositivos.

## Decisão

1. **Uma conexão WebSocket por cliente, com frames de dois tipos:**
   - **binário:** áudio PCM (cliente → servidor);
   - **texto:** mensagens de controle em JSON, com campo `type`, nos dois sentidos.

2. **Formato do áudio: PCM `s16le`, 16 kHz, mono**, em blocos de 512 amostras (32 ms). É o formato
   nativo de microfones, o que o Whisper espera depois da conversão para float, e o bloco nativo do
   Silero VAD. O consumo é de 256 kbit/s.

3. **VAD e detecção de fim de fala no servidor.** O cliente só transmite. Toda a lógica de
   segmentação e seus parâmetros ficam num lugar só, onde também são medidos os tempos.

4. **Handshake versionado:** a primeira mensagem é um `hello` com a versão do protocolo, o formato
   do áudio e o token. O servidor responde `ready` ou um `error` e fecha a conexão.

5. **Autenticação por token compartilhado** (`AGATHA_TOKEN` no `.env`, fora do git), comparado em
   tempo constante. Porta **8765/tcp**, liberada no firewall apenas para a rede local
   (`192.168.1.0/24`). Sem TLS por enquanto: o tráfego não sai da LAN.

6. A especificação completa fica em [`docs/protocol.md`](../protocol.md), que é atualizado a cada
   mudança de versão.

### Alternativas consideradas

| alternativa                              | por que não                                                                  |
|------------------------------------------|------------------------------------------------------------------------------|
| HTTP, um upload por frase                | sem streaming. O cliente teria de detectar o fim da fala sozinho             |
| gRPC (streaming bidirecional)            | mais complexo de gerar e manter no cliente Windows, sem ganho real na LAN    |
| TCP/UDP cru                              | enquadramento, reconexão e controle teriam de ser feitos à mão               |
| áudio em float32                         | o dobro da banda, sem ganho de qualidade (o microfone já entrega 16 bits)    |
| Opus (comprimido)                        | custo de CPU e complexidade de codec. Desnecessário na LAN; pode voltar para acesso remoto |
| VAD no cliente                           | duplicaria a lógica e os parâmetros e espalharia a medição entre duas máquinas |

## Consequências

**Positivas**

- O cliente fica mínimo: capturar, enviar e mostrar o que chega. A wake word da Fase 3 é a única
  lógica de áudio que vai para o cliente.
- O endpointing pode ser ajustado e medido sem atualizar o cliente.
- O campo `protocol` no `hello` permite evoluir (áudio de volta, interrupção) e rejeitar clientes
  incompatíveis com um erro claro.
- JSON nas mensagens de controle facilita depuração e log.

**Negativas / riscos**

- **Enquanto o cliente está ouvindo, o áudio flui o tempo todo** (256 kbit/s) e o servidor roda o
  VAD continuamente. É irrelevante na LAN. Na Fase 3, a wake word no cliente passa a liberar o
  envio só depois de detectada.
- **Token em texto claro** na conexão sem TLS: qualquer um que capture o tráfego da LAN consegue
  lê-lo. É aceitável para uma rede doméstica. Acesso fora da LAN exige TLS (ou VPN) e um novo ADR.
- **Os tempos são medidos no servidor:** a latência de rede entre cliente e servidor não entra no
  `total_ms`. Na LAN ela é de poucos milissegundos, mas deve ser medida ponta a ponta na Fase 2.
