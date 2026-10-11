# client/

Cliente da Agatha, que roda no PC Windows. Ele captura o microfone, envia o áudio ao servidor
pelo WebSocket ([protocolo v2](../docs/protocol.md)) e **toca a resposta falada** da Agatha. Toda
a detecção de fala acontece no servidor. A wake word chega na Fase 3.

A conversa é **half-duplex** ([ADR-006](../docs/adr/0006-protocolo-v2-half-duplex.md)): enquanto
a Agatha pensa e fala, o microfone não é enviado. Ela volta a ouvir quando termina de falar. Por
enquanto não dá para interrompê-la.

## Instalação no Windows

1. **Python 3.11 ou mais novo.** No PowerShell:

   ```powershell
   py --version                                   # já tem?
   winget install -e --id Python.Python.3.12      # se não tiver
   ```

2. **Código e ambiente virtual:**

   ```powershell
   git clone https://github.com/ReinaldoLuizSilva/agatha.git
   cd agatha\client
   py -3.12 -m venv .venv
   .venv\Scripts\pip install -e .
   ```

   O `sounddevice` já traz o PortAudio no pacote para Windows, então não há nada mais a instalar.

3. **Token:** copie o `.env.example` para `.env` e preencha `AGATHA_TOKEN` com o mesmo valor do
   servidor (`~/agatha/server/.env` no `agatha-srv`):

   ```powershell
   copy .env.example .env
   notepad .env
   ```

## Uso

```powershell
.venv\Scripts\python -m agatha_client --list-devices        # microfones e saídas de som (* = padrão)
.venv\Scripts\python -m agatha_client                       # microfone e saída padrão
.venv\Scripts\python -m agatha_client --device 3            # escolhe outro microfone
.venv\Scripts\python -m agatha_client --output-device 7     # escolhe outra saída de som
```

Fale normalmente. Meio segundo depois que você parar, a Agatha começa a responder:

```
conectado a ws://192.168.1.25:8765/ (sessão 4de2af2d, resposta a 22050 Hz, latência de saída 40 ms)
Pode falar. Ctrl+C para sair.

  ▶ ouvindo... (#1)
  #1 você: Que horas são agora?  [stt 382 ms]
  agatha: São 18h30. Quer que eu ajude em mais alguma coisa?
    ⏱ fim da fala → primeiro som: 1759 ms (servidor 1700 ms + rede/reprodução 59 ms)
```

O `⏱` é a meta do projeto (menos de 2 s): do fim da sua fala até o primeiro som da resposta sair
na placa, medido no relógio do próprio PC. Se a conexão cair, o cliente tenta de novo a cada 3 s.

Para testar sem microfone, envie um arquivo WAV de 16 kHz:

```powershell
.venv\Scripts\pip install -e ".[wav]"
.venv\Scripts\python -m agatha_client --wav frase.wav
```

## Problemas comuns

| sintoma | o que verificar |
|---|---|
| `conexão ... indisponível` | o servidor está rodando? A porta está aberta? No PowerShell: `Test-NetConnection 192.168.1.25 -Port 8765` |
| `unauthorized` | o `AGATHA_TOKEN` do `.env` precisa ser idêntico ao do servidor |
| `o microfone não aceita 16 kHz mono` | escolha outra entrada com `--device`. As do tipo `[MME]` convertem a taxa automaticamente |
| nada acontece ao falar | confira o microfone padrão em Configurações › Som, ou escolha um com `--device` |
| transcreve o som do PC (vídeo, música) | o microfone padrão pode ser um dispositivo de loopback ("Stereo Mix"); escolha o microfone do headset com `--device` |
| `não foi possível abrir a saída de som a 22050 Hz` | escolha outra saída com `--output-device` (as do tipo `[MME]` convertem a taxa) |
| a Agatha não volta a ouvir | o servidor espera o fim da reprodução; se o cliente travar, ele volta sozinho depois de ~15 s |
