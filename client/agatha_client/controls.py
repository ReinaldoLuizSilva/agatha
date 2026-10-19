"""Estado que a bandeja muda com o cliente rodando (ADR-021).

A bandeja roda na thread principal (exigência do Windows) e o cliente num loop do asyncio em outra
thread; os dois só compartilham isto: dois booleanos lidos a cada bloco de áudio e um aviso de estado.
"""

from collections.abc import Callable
from dataclasses import dataclass

CONNECTING = "conectando"
LISTENING = "ouvindo"
SPEAKING = "falando"
OFFLINE = "desconectado"
ERROR = "erro"


@dataclass
class Controls:
    muted: bool = False  # microfone pausado: o servidor recebe silêncio
    barge_in: bool = True  # interromper pelo nome enquanto ela fala
    on_status: Callable[[str], None] | None = None
    state: str = CONNECTING

    def status(self, state: str) -> None:
        self.state = state
        if self.on_status is not None:
            self.on_status(state)
