# ADR-013: Agatha como serviço do systemd com Quadlet

- **Status:** Aceito
- **Data:** 2026-10-12

## Contexto

Até a Fase 3, a Agatha subia com `deploy/up.sh`: um `podman pod create` com dois `podman run`
(`agatha-llm` e `agatha-server`, ADR-001 e ADR-004). Isso não sobrevive a um reboot nem a uma
queda de processo, e a Fase 4 pede um serviço de verdade.

Restrições:

- **sem root no dia a dia:** o projeto roda como o usuário `agatha`, com Podman rootless;
- **GPU por CDI** (`--device nvidia.com/gpu=all`) e os volumes com `:z` do SELinux, como já
  funcionavam no `up.sh`;
- **ordem de partida:** o servidor põe o prompt de sistema no cache do LLM ao subir (ADR-007), então
  o LLM precisa estar respondendo antes.

Opções:

- **`podman generate systemd`**: obsoleto desde o Podman 4.4, em favor do Quadlet;
- **units escritas à mão** chamando `podman run`: funcionam, mas reimplementam o que o Quadlet já
  faz (`--replace`, `--sdnotify`, cgroups, limpeza);
- **`podman kube play`** com um YAML de Kubernetes: aproxima do estudo de Kubernetes do Reinaldo, mas
  GPU por CDI e healthcheck com notificação ao systemd ficam menos diretos;
- **Quadlet** (Podman 5.8.2 no host): arquivos `.pod` e `.container` declarativos, que o systemd
  transforma em serviços.

## Decisão

1. **Quadlet no systemd do usuário `agatha`.** As units ficam versionadas em `deploy/quadlet/` e
   `deploy/install-systemd.sh` as copia para `~/.config/containers/systemd/`:
   - `agatha.pod` → `agatha-pod.service`: o pod e as portas (8765 para a LAN, 8080 só em
     `127.0.0.1`);
   - `agatha-llm.container` → `agatha-llm.service`;
   - `agatha-server.container` → `agatha-server.service`, com `Wants=` e `After=` no LLM.
2. **`Notify=healthy`** nos dois containers: o serviço só fica "ativo" quando o healthcheck passa
   (`/health` do llama-server; porta 8765 aberta no servidor, o que só acontece depois de carregar
   STT e TTS e de pôr o prompt no cache). Assim, a ordem de partida é real e não um `sleep`.
   Healthcheck a cada 10 s, para a partida não esperar os 30 s do padrão das imagens.
3. **`Restart=on-failure`** com 5 s de espera, e `TimeoutStartSec=300` para a carga dos modelos.
4. **`deploy/up.sh` e `deploy/down.sh` viram atalhos** para `systemctl --user start/stop`. O pod
   criado à mão deixa de existir: o instalador o remove antes de o systemd assumir.
5. **Linger** (`sudo loginctl enable-linger agatha`, uma vez, pelo Reinaldo): sem ele, os serviços do
   usuário só existem enquanto há uma sessão aberta e não sobem no boot.
6. **Logs no driver padrão do Podman (`k8s-file`)**, e não no journald: o journal do host é volátil
   (não existe `/var/log/journal`) e o usuário `agatha` não lê o journal do sistema; com
   `LogDriver=journald`, nem o `podman logs` mostrava nada. Logs persistentes da aplicação ficam em
   `/dados/agatha/logs` (próxima entrega da Fase 4).
7. **Laboratórios fora do systemd** (Zabbix e Oracle continuam com `deploy/zabbix/` e
   `deploy/oracle/`): não fazem parte da Agatha, e as ferramentas já tratam o laboratório fora do ar.

## Consequências

**Medido:**
- instalação até os dois serviços saudáveis: 33 s;
- `podman kill agatha-server`: o systemd religou o servidor em ~10 s (`NRestarts=1`), e o prompt
  voltou ao cache do LLM em 125 ms, porque o LLM continuou no ar;
- conversa pelo WebSocket depois disso (STT, ferramentas e TTS): normal, 1º som em 1,6–1,9 s.

**Positivas**

- Sobe no boot (com linger) e se recupera de quedas sem intervenção.
- A configuração do serviço é declarativa e versionada, e a ordem LLM → servidor é garantida pelo
  healthcheck.
- Operação padrão: `systemctl --user status|restart agatha-server`.

**Negativas / riscos**

- **Linger exige root uma vez.** Até o Reinaldo ligar, a Agatha para quando a sessão do usuário
  `agatha` fecha.
- **Se só o LLM reiniciar, o cache do prompt se perde:** a primeira pergunta depois disso paga ~6 s
  de processamento do prompt. O servidor não é reiniciado junto (`Wants=`, não `Requires=`), para
  não derrubar uma conversa por causa disso.
- **Os containers usam `--rm`:** depois de uma queda, o `podman logs` do container anterior some.
  Por isso os logs da aplicação precisam ir para arquivo.
- **O healthcheck a cada 10 s abre uma conexão no WebSocket**, que a biblioteca `websockets`
  registra no log ("connection closed"). Tratado na entrega de logs.
- Caminhos (`/dados/agatha/...`) e tags das imagens ficam fixos nas units; trocar a versão da imagem
  exige editar a unit e reinstalar.
