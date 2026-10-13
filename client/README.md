# client/

Cliente da Agatha, que roda no PC Windows. Ele captura o microfone, envia o áudio ao servidor
pelo WebSocket ([protocolo v2](../docs/protocol.md)) e **toca a resposta falada** da Agatha. Toda
a detecção de fala acontece no servidor, inclusive a wake word "Agatha", reconhecida pela
transcrição ([ADR-008](../docs/adr/0008-wake-word-pela-transcricao.md)).

A conversa é **half-duplex** ([ADR-006](../docs/adr/0006-protocolo-v2-half-duplex.md)): enquanto
a Agatha pensa e fala, o microfone não é enviado. Ela volta a ouvir quando termina de falar. A
exceção é o **barge-in** ([ADR-016](../docs/adr/0016-barge-in.md)): dizer "Agatha" enquanto ela
fala a interrompe (veja abaixo).

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

   Para o **barge-in**, instale o extra e copie os modelos do detector (ficam fora do git):

   ```powershell
   .venv\Scripts\pip install -e ".[barge-in]"
   mkdir models
   scp root@192.168.1.25:/dados/agatha/models/wakeword/*.onnx models\
   ```

   São três arquivos: `agatha.onnx` (treinado com a sua voz), `melspectrogram.onnx` e
   `embedding_model.onnx`. Sem eles, o cliente avisa e funciona como antes, sem interromper.

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

### Interromper a Agatha (barge-in)

Com o extra `[barge-in]` e os modelos, o cliente abre o microfone e as caixas **no mesmo stream**
e continua ouvindo enquanto ela fala: o cancelamento de eco tira a voz dela do microfone
([ADR-015](../docs/adr/0015-cancelamento-de-eco-linear.md)) e o detector procura "Agatha". Ao
ouvir o nome, a fala **pausa na hora** e o servidor confere pela transcrição:

| você diz | o que acontece |
|---|---|
| "Agatha, para" / "Agatha, chega" / só "Agatha!" | ela para e fica ouvindo (modo conversa) |
| "Agatha, e o host do banco?" | ela para e responde à pergunta nova |
| (disparo falso: não era com ela) | ela continua de onde parou |

```
  agatha: O tablespace DADOS_APP está com 85% de uso. Ele tem
  ✋ ouvi "Agatha" (0.97): pausando...
  ▶ ouvindo... (#3)
  #3 você: Agatha, para.  [stt 401 ms]
  ■ interrompida
```

Nos primeiros ~2 s em que ela fala, o cliente **mede o atraso do eco** (aparece
`barge-in: eco medido, atraso ... ms`); antes disso o detector ainda não dispara. Opções:

| opção | para quê |
|---|---|
| `--barge-threshold 0.5` | interrompe mais fácil (acerta ~85% em vez de ~77%), mas dispara mais à toa com a voz dela |
| `--echo-delay-ms 120` | usa um atraso fixo em vez de medir |
| `--no-barge-in` | volta ao modo antigo, sem ouvir durante a resposta |
| `--wake-model PASTA` | outra pasta com os três `.onnx` (padrão: `models`) |

O detector foi treinado com o **"Microfone Externo (Realtek)" do notebook** e as caixas dele; use
os mesmos dispositivos, de preferência os dois do tipo `[MME]`.

Para testar sem microfone, envie um arquivo WAV de 16 kHz:

```powershell
.venv\Scripts\pip install -e ".[wav]"
.venv\Scripts\python -m agatha_client --wav frase.wav
```

## Gravar amostras para a wake word (Fase 5)

O `agatha_client.record` é um gravador guiado. Ele mostra uma frase, você fala, e cada fala vira
um WAV de 16 kHz em `gravacoes\`, com um `manifest.jsonl` descrevendo tudo. Essas amostras servem
para escolher e treinar o detector de "Agatha" no cliente e para testar a interrupção com as
caixas de som. São ~140 falas, cerca de 30 min. Dá para parar com `q` a qualquer momento: rodar de
novo continua de onde parou.

1. **Baixe as respostas da Agatha** (usadas na parte `eco`, em que ela fala e você a interrompe):

   ```powershell
   scp -r root@192.168.1.25:/dados/agatha/data/wakeword/respostas .
   ```

2. **Grave** com o microfone e as caixas de som do notebook, no volume de sempre:

   ```powershell
   .venv\Scripts\python -m agatha_client.record --list --replies respostas   # o roteiro
   .venv\Scripts\python -m agatha_client.record --replies respostas          # grava tudo
   .venv\Scripts\python -m agatha_client.record --part longe                 # só uma parte
   ```

   | parte | o que fazer |
   |---|---|
   | `perto` | "Agatha" em frases variadas, a ~50 cm do notebook, voz normal |
   | `variado` | o jeito vem indicado em cada frase: baixinho, rápido, firme, chamando... |
   | `longe` | a 2–3 m do notebook, como chamaria de outro canto do cômodo |
   | `negativas` | frases **sem** o nome, inclusive as parecidas ("a gata", "agora") |
   | `eco` | a Agatha fala pelas caixas; quando pedir, diga a frase **no meio da fala dela**, ou fique calado |
   | `ambiente` | 10 min de TV, rádio ou podcast tocando **em outro aparelho**; ninguém chama a Agatha |

   Depois de cada fala ele mostra o pico do som. Se avisar que ficou baixo, estourado ou que "não
   parece voz" (estalo de conector mal encaixado), ele oferece gravar de novo. Para regravar falas
   que já foram salvas, use `--redo` com ids ou intervalos:

   ```powershell
   .venv\Scripts\python -m agatha_client.record --redo perto-001..perto-020
   ```

3. **Envie a pasta** para o servidor:

   ```powershell
   scp -r gravacoes root@192.168.1.25:/dados/agatha/data/wakeword/
   ```

   No servidor, como root: `chown -R agatha:agatha /dados/agatha/data/wakeword`.

As gravações têm a sua voz: ficam só no notebook e em `/dados`, nunca no git (`*.wav` está no
`.gitignore`).

## Problemas comuns

| sintoma | o que verificar |
|---|---|
| `conexão ... indisponível` | o servidor está rodando? A porta está aberta? No PowerShell: `Test-NetConnection 192.168.1.25 -Port 8765` |
| `unauthorized` | o `AGATHA_TOKEN` do `.env` precisa ser idêntico ao do servidor |
| `o microfone não aceita 16 kHz mono` | escolha outra entrada com `--device`. As do tipo `[MME]` convertem a taxa automaticamente |
| nada acontece ao falar | confira o microfone padrão em Configurações › Som, ou escolha um com `--device` |
| transcreve o som do PC (vídeo, música) | o microfone padrão pode ser um dispositivo de loopback ("Stereo Mix"); escolha o microfone do headset com `--device` |
| `não foi possível abrir a saída de som a 22050 Hz` | escolha outra saída com `--output-device` (as do tipo `[MME]` convertem a taxa) |
| gravador, parte `eco`: `não foi possível abrir microfone e caixas juntos` | escolha os dois do mesmo tipo, de preferência `[MME]`: `--device N --output-device M` (veja `--list-devices`) |
| a Agatha não volta a ouvir | o servidor espera o fim da reprodução; se o cliente travar, ele volta sozinho depois de ~15 s |
| `barge-in desligado: faltam em models ...` | copie os três `.onnx` (Instalação, passo 2) ou indique a pasta com `--wake-model` |
| `barge-in desligado: No module named 'openwakeword'` | `.venv\Scripts\pip install -e ".[barge-in]"` |
| `microfone e saída de som precisam aceitar 16000 Hz` | escolha os dois `[MME]` com `--device` e `--output-device`, ou use `--no-barge-in` |
| ela pausa sozinha às vezes (`✋` sem você falar) | disparo falso com a voz dela; o servidor confere e ela continua. Se for frequente, suba `--barge-threshold` (ex.: 0.95) |
| diz "Agatha" e ela não para | fale um pouco mais alto ou mais perto; ou baixe `--barge-threshold` (ex.: 0.5) |
