# deploy/

Implantação da Agatha no `agatha-srv`: preparação do host, imagens e o pod que roda tudo, como
serviço do systemd do usuário `agatha` ([ADR-013](../docs/adr/0013-systemd-quadlet.md)).

```
deploy/
├── install-systemd.sh   # instala as units do Quadlet e sobe o pod
├── install-labs.sh      # instala os laboratórios no systemd (ADR-017; --uninstall para tirar)
├── quadlet/             # agatha.pod, agatha-llm, agatha-server e agatha-search (.container)
│   └── lab/             # zabbix.pod, zabbix-*.container, oracle-free.container
├── up.sh                # sobe (ou reinicia) o pod "agatha" pelo systemd
├── down.sh              # derruba o pod
├── llama/Containerfile  # imagem do llama-server (CUDA 12, sm_61)
├── server/Containerfile # imagem do servidor da Agatha
├── searxng/settings.yml # buscadores do SearXNG da ferramenta buscar_documentacao (ADR-025)
├── zabbix/              # Zabbix de laboratório: up.sh, down.sh, setup.py (ADR-011)
└── oracle/              # Oracle 23ai Free de laboratório: up.sh, down.sh, setup.py (ADR-012)
```

## Rodando a Agatha

```bash
deploy/install-systemd.sh       # uma vez (e a cada mudança em deploy/quadlet/)
sudo loginctl enable-linger agatha   # uma vez, como root: sobe no boot sem sessão aberta

deploy/up.sh                    # sobe (ou reinicia) e espera os serviços ficarem saudáveis
podman logs -f agatha-server    # transcrições, respostas e tempos de cada etapa
podman logs -f agatha-llm       # llama-server
tail -f /dados/agatha/logs/server.log   # o mesmo, persistente
systemctl --user status agatha-server agatha-llm
systemctl --user restart agatha-server   # ex.: depois de reconstruir a imagem do servidor
deploy/down.sh                  # derruba
```

### Busca (SearXNG)

O `agatha-search` (ADR-025) roda o SearXNG dentro do pod, só em `127.0.0.1:8888`: o servidor chega
por ali e nada fica aberto no host. A imagem é baixada na primeira subida (`Pull=missing`, versão
fixa na unit). O `install-systemd.sh` copia `deploy/searxng/settings.yml` para
`~/.config/agatha/searxng/` e gera a chave em `~/.config/agatha/search.env` (uma vez, fora do git);
mudou o `settings.yml`, rode o instalador de novo ou copie e `systemctl --user restart agatha-search`.

```bash
podman exec agatha-server python3.12 -c "import urllib.request; print(urllib.request.urlopen(
  'http://127.0.0.1:8888/search?q=zabbix+proxy&format=json').read()[:300])"   # teste rápido
podman logs agatha-search      # buscador suspenso aparece como "too many requests" ou "CAPTCHA"
```

### Teste de fumaça

Depois de subir ou de reconstruir a imagem, confira que a Agatha ouve, entende, usa ferramenta e
responde em voz:

```bash
.venv/bin/python server/scripts/smoke.py      # ~20 s; sai com código 1 se algo falhar
```

Ele gera duas perguntas com o Piper ("quanto é 15% de 240?" e "para que serve um backup
incremental?"), manda pelo WebSocket como o cliente faria e confere a transcrição, a calculadora,
a resposta com áudio e o 1º áudio abaixo de 3 s. Não toca na memória nem nos laboratórios.

### Logs

| onde | o que tem |
|---|---|
| `podman logs agatha-server` | o mesmo log do arquivo abaixo, mas só do container atual (some se ele for recriado) |
| `/dados/agatha/logs/server.log` | log do servidor, com rotação (10 MB × 5 arquivos) |
| `/dados/agatha/logs/turns-AAAA-MM.jsonl` | uma linha por conexão, transcrição e resposta, com todos os tempos e o 1º som medido no cliente |

```bash
.venv/bin/python bench/turns_report.py --client agatha-client /dados/agatha/logs/turns-*.jsonl
```

O relatório mostra a latência por sessão (p50, p90, máx), as falas ignoradas, as ferramentas e as
respostas mais lentas. `--client agatha-client` deixa de fora os testes com `send_wav`.

O systemd religa um container que cair (`Restart=on-failure`) e só considera o serviço no ar
quando o healthcheck passa (`Notify=healthy`): o servidor sobe depois de o LLM responder. Os logs
ficam no driver padrão do Podman, não no journald (o journal do host é volátil e o usuário
`agatha` não o lê; ver ADR-013).

| container | imagem | GPU | o que faz |
|---|---|---|---|
| `agatha-llm` | `localhost/agatha-llama:b11205` | ~3,3 GB | Gemma 4 E4B no llama-server, em `127.0.0.1:8080` dentro do pod |
| `agatha-server` | `localhost/agatha-server:0.2.0` | ~0,4 GB | VAD, STT, núcleo e TTS; WebSocket em `:8765` |

- **Rede:** os dois containers ficam no mesmo pod e se falam por `127.0.0.1`. O pod publica a
  porta **8765** (WebSocket, liberada no firewall só para a LAN) e a **8080** apenas em
  `127.0.0.1` do host, para os benchmarks.
- **Modelos:** `/dados/agatha/models` é montado **somente leitura**. Nada de modelo dentro das imagens.
- **Dados:** `/dados/agatha/data` é montado **com escrita** em `/data` no servidor: é onde fica a
  memória da Agatha (`memory/agatha.db`, [ADR-010](../docs/adr/0010-memoria-explicita.md)).
- **Logs:** `/dados/agatha/logs` é montado com escrita em `/logs` (`AGATHA_LOG_DIR`).
- **Configuração:** o token vem de `server/.env` (`EnvironmentFile=` na unit); os caminhos dos modelos já estão
  na imagem do servidor.
- **Fuso horário:** `Timezone=local`. Sem isso, os containers ficam em UTC e a Agatha erra a hora.
- **VRAM:** o pico com os dois serviços é de ~3,66 GB de 4 GB. Pare o pod antes de rodar os
  benchmarks de STT e LLM, ou os testes de STT, que precisam da GPU livre.

## Laboratórios no systemd

Desde o [ADR-017](../docs/adr/0017-laboratorios-no-systemd.md), o Zabbix e o Oracle de laboratório
são serviços do systemd do usuário, como a Agatha: sobem no boot e voltam se caírem.

```bash
deploy/install-labs.sh               # instala/atualiza as units e (re)inicia (depois do 1º up.sh de cada um)
deploy/install-labs.sh --uninstall   # para e remove as units (os dados ficam)
systemctl --user status oracle-free zabbix-server
```

Com as units instaladas, os `up.sh`/`down.sh` abaixo viram atalhos para `systemctl --user`, e um
`down.sh` vale até o próximo boot. As senhas ficam nos arquivos de sempre; o instalador gera a partir
delas os arquivos de ambiente das units (`db.env`, `agent.env`, `oracle.env`, modo 600). Rode o
instalador de novo se o IP do servidor mudar (o agente do Zabbix precisa dele). RAM fixa: ~2,4 GB.

## Zabbix de laboratório

Zabbix 7.0 LTS monitorando o próprio agatha-srv, para as ferramentas da Agatha
([ADR-011](../docs/adr/0011-zabbix-laboratorio.md)):

```bash
deploy/zabbix/up.sh                          # pod "zabbix" (postgres, server, web) + agent2
.venv/bin/python deploy/zabbix/setup.py      # 1ª vez: senha do Admin, host, usuário/token só leitura
systemctl --user restart agatha-server       # relê o server/.env com o token
deploy/zabbix/down.sh                        # derruba (os dados ficam)
```

- **Frontend:** `http://192.168.1.25:8081`, usuário `Admin`, senha em
  `/dados/agatha/lab/zabbix/admin-password`.
- **Firewall** (uma vez, com sudo):

  ```bash
  sudo firewall-cmd --permanent --zone=public \
    --add-rich-rule='rule family="ipv4" source address="192.168.1.0/24" port port="8081" protocol="tcp" accept'
  sudo firewall-cmd --reload
  ```
- **Rede:** de dentro dos containers, o host é `host.containers.internal`. O IP da LAN aponta para
  o próprio container na rede pasta do Podman sem root.
- **RAM:** ~300 MB. Não usa GPU.

## Oracle de laboratório

Oracle Database 23ai Free (imagem `gvenzl/oracle-free:23.9`) para as ferramentas da Agatha
([ADR-012](../docs/adr/0012-oracle-laboratorio.md)):

```bash
deploy/oracle/up.sh                          # container oracle-free (1ª partida extrai o banco, ~20 s)
.venv/bin/python deploy/oracle/setup.py      # 1ª vez: usuário AGATHA_RO + tablespace DADOS_APP ~85%
systemctl --user restart agatha-server       # relê o server/.env com a conexão
deploy/oracle/down.sh                        # derruba com shutdown limpo (os dados ficam)
```

- **Conexão:** PDB `FREEPDB1` na porta 1521, **sem liberação no firewall** (só os containers do
  servidor conectam, por `host.containers.internal`). Senhas em `/dados/agatha/lab/oracle`
  (modo 600): `system-password` (SYS/SYSTEM) e `agatha_ro-password`.
- **RAM:** ~2,1 GB. **Disco:** ~2,9 GB de dados + até 10 GB de backups. Não usa GPU.
- **Usuários da Agatha** (ADR-020), só leitura: `AGATHA_RO` na PDB (tablespaces, sessões, locks) e
  `C##AGATHA_RO` no contêiner raiz (estado do banco, Data Guard, backups do RMAN, que a PDB não
  enxerga). Senhas em `agatha_ro-password` e `c_agatha_ro-password`.
- **Backup:** ARCHIVELOG + RMAN (redundância 2, comprimido, área de recuperação de 10 GB em
  `oradata/fra`). `deploy/oracle/backup.sh` roda às 02:30 pelo `oracle-backup.timer`
  (`systemctl --user list-timers`); à mão: `deploy/oracle/backup.sh`.

### Construir as imagens

```bash
# llama.cpp (só quando mudar a versão; compilar leva ~20 min)
podman build --format docker -t localhost/agatha-llama:b11205 deploy/llama

# servidor (a cada mudança de código; o contexto é a pasta server/)
podman build --format docker -f deploy/server/Containerfile -t localhost/agatha-server:0.2.0 server
podman image prune -f           # remove as camadas antigas
systemctl --user restart agatha-server
```

`--format docker` preserva o `HEALTHCHECK`, que o formato OCI ignora. A imagem do servidor
instala o extra `gpu` do `pyproject`, ou seja, as mesmas versões de cuBLAS e cuDNN validadas no
desenvolvimento.

## Preparação do host

O host fica só com o driver NVIDIA e o runtime de containers. CUDA, bibliotecas e modelos de
runtime ficam nos containers ([ADR-001](../docs/adr/0001-arquitetura-inicial.md)).

### Restrições da GPU (GTX 1050 Ti, Pascal)

- Driver NVIDIA no branch **580** (repositório `epel-nvidia-580` do negativo17). O branch 590+
  não suporta Pascal.
- Nada de repositórios NVIDIA que tragam driver 590+, nem os módulos open da NVIDIA.
- CUDA **12.x** nos containers. O `nvidia-smi` mostra "CUDA 13.0", mas isso é só o máximo que o
  driver suporta: o toolkit 13 não suporta Pascal.
- Atualização do kernel UEK exige o `kernel-uek-devel` da mesma versão, para o DKMS recompilar o módulo.

### NVIDIA Container Toolkit (GPU no Podman via CDI)

O repositório oficial `libnvidia-container` publica **apenas** os pacotes do toolkit
(`nvidia-container-toolkit*`, `libnvidia-container*`), sem driver, módulo de kernel ou CUDA.
Por isso ele é compatível com a restrição acima.

```bash
# repositório (a seção "experimental" já vem desabilitada)
sudo curl -sSL -o /etc/yum.repos.d/nvidia-container-toolkit.repo \
  https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo

# confira a transação: só pacotes nvidia-container-* e libnvidia-container-*
sudo dnf install nvidia-container-toolkit

# especificação CDI: diz ao Podman quais dispositivos e bibliotecas da GPU expor
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
nvidia-ctk cdi list

# SELinux: permite que containers acessem dispositivos, mantendo o isolamento
# (preferível a --security-opt label=disable em cada container)
sudo setsebool -P container_use_devices on
```

O pacote instala o `nvidia-cdi-refresh.path`, habilitado por padrão, que regenera o
`/etc/cdi/nvidia.yaml` quando o driver muda.

Teste com o usuário de serviço `agatha` (Podman sem root):

```bash
podman run --rm --device nvidia.com/gpu=all docker.io/nvidia/cuda:12.9.1-base-ubi9 nvidia-smi
```

Estado validado em 2026-10-11: Oracle Linux 10.2, UEK 6.12.0-207, driver 580.178.04,
Podman 5.8.2 (sem root, cgroups v2, crun), NVIDIA Container Toolkit 1.20.1.

### Firewall

A porta do servidor WebSocket fica aberta só para a rede local ([ADR-003](../docs/adr/0003-protocolo-websocket.md)):

```bash
sudo firewall-cmd --permanent --zone=public \
  --add-rich-rule='rule family="ipv4" source address="192.168.1.0/24" port port="8765" protocol="tcp" accept'
sudo firewall-cmd --reload
```

### Outros softwares no host

O Docker CE também está instalado no `agatha-srv`, para um estudo de Kubernetes (minikube) que
não tem relação com este projeto. A Agatha usa **Podman**. Os dois convivem sem conflito, e o
toolkit acima não altera a configuração do Docker.
