# ADR-021: Cliente em segundo plano — ícone na bandeja do Windows

- **Status:** Aceito
- **Data:** 2026-10-19
- **Complementa:** [ADR-003](0003-protocolo-websocket.md) (o cliente) e [ADR-016](0016-barge-in.md) (barge-in)

## Contexto

Até a Fase 8, o cliente era um programa de terminal: para usar a Agatha, o Reinaldo abria o
PowerShell, entrava na pasta e rodava `python -m agatha_client`, e a janela precisava ficar aberta.
Para o uso do dia a dia isso é atrito. A Fase 9 pede que o cliente rode em segundo plano, inicie com
o Windows e mostre o estado sem ocupar a tela, sem perder o modo terminal, que é o de diagnóstico.

## Decisão

1. **Ícone na bandeja com o `pystray`** (com o Pillow para desenhar os ícones, sem arquivos de
   imagem), num extra opcional `[tray]`. A bandeja fica na thread principal, como o Windows exige, e
   o cliente roda num loop do asyncio em outra thread. **É o mesmo cliente do modo terminal**: os dois
   só compartilham um objeto `Controls` com o microfone pausado, o barge-in ligado e o aviso de
   estado.
2. **O estado pela cor:** verde ouvindo, azul falando, âmbar microfone pausado, cinza conectando ou
   sem conexão, vermelho erro, com notificação. O menu tem pausar o microfone, ligar ou desligar o
   barge-in, abrir o log e sair.
3. **Microfone pausado manda silêncio**, em vez de parar de mandar: uma frase já começada se fecha
   no servidor, e a contagem de posição do áudio, que o protocolo usa para medir latência, continua
   certa. Com o microfone pausado, o barge-in também não dispara.
4. **Sem terminal, o que seria impresso vai para o log** em `%LOCALAPPDATA%\Agatha\client.log`, com
   rotação de 4 arquivos de 1 MB. Um objeto no lugar do `sys.stdout` junta os pedaços de linha
   (`print(..., end=" ")`). Assim nenhuma mensagem precisou ser reescrita, e o modo terminal ficou
   idêntico.
5. **Só uma instância por vez:** um mutex nomeado do Windows (`Local\AgathaClient`), que some sozinho
   se o processo morrer. Fora do Windows, um `flock`. Dois clientes disputariam o microfone.
6. **Iniciar com o Windows:** `--autostart ligar` cria um atalho na pasta de inicialização do usuário
   com `pythonw -m agatha_client --tray`, rodando na pasta do cliente, onde estão o `.env` e os
   modelos. O atalho é criado pelo `WScript.Shell` via PowerShell, sem dependência nova.
   `--autostart desligar` apaga o atalho.
7. **Erros visíveis:** se o microfone não abrir ou o servidor recusar o token, a bandeja fica
   vermelha e mostra uma notificação. Sem isso, o erro só existiria no log.

### Alternativas consideradas

| alternativa | por que não |
|---|---|
| Serviço do Windows | serviços não acessam bem o áudio da sessão do usuário nem mostram ícone na bandeja |
| Agendador de Tarefas no logon | funciona, mas a pasta de inicialização é mais simples de ligar, desligar e entender |
| Interface gráfica própria (janela) | o objetivo é não ocupar a tela; a bandeja basta para estado e controles |
| Executável empacotado (PyInstaller) | instalação e atualização ficaram fora do escopo da Fase 9 (decisão do Reinaldo) |

## Consequências

**Positivas**

- Uso sem terminal: liga com o Windows, mostra o estado e se recupera sozinho de quedas do
  servidor, pela reconexão a cada 3 s.
- O modo terminal continua igual para diagnóstico, e os dois modos compartilham todo o código do
  cliente.

**Negativas / riscos**

- **Mais duas dependências no cliente** (pystray e Pillow), opcionais.
- **A bandeja não roda na CI** (o GitHub Actions não tem área de trabalho): os testes cobrem o log,
  os estados, os ícones, a instância única e o atalho, mas não o ícone em si. O teste é manual, no
  notebook.
- O atalho aponta para o `pythonw` do ambiente virtual e para a pasta atual. Se a pasta do cliente
  mudar de lugar, é preciso rodar `--autostart ligar` de novo.
