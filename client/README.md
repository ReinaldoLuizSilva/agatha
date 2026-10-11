# client/

Cliente da Agatha, que roda no PC Windows. Na Fase 1 ele captura o microfone, envia o áudio ao
servidor pelo WebSocket ([protocolo v1](../docs/protocol.md)) e mostra as transcrições. Toda a
detecção de fala acontece no servidor. A wake word chega na Fase 3.

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
.venv\Scripts\python -m agatha_client --list-devices   # lista os microfones (* = padrão)
.venv\Scripts\python -m agatha_client                  # usa o microfone padrão
.venv\Scripts\python -m agatha_client --device 3       # escolhe outro microfone
```

Fale normalmente. Cerca de meio segundo depois que você parar, a transcrição aparece:

```
conectado a ws://192.168.1.25:8765/ (sessão 4de2af2d)
Pode falar. Ctrl+C para sair.

  ▶ ouvindo... (#1)
  » Qual o tamanho da tablespace USERS?
    stt 420 ms · total 930 ms (fim da fala → texto)
```

O `total` é o tempo entre o fim da sua fala e o texto pronto no servidor. Se a conexão cair, o
cliente tenta de novo a cada 3 s.

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
