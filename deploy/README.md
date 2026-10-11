# deploy/

Implantação da Agatha no `agatha-srv`. Por enquanto este documento cobre a preparação do host.
Containers e serviços systemd entram nas próximas fases.

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
