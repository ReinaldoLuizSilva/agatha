"""A Agatha em segundo plano no Windows (ADR-021): ícone na bandeja, sem janela de terminal.

    .venv\\Scripts\\pythonw -m agatha_client --tray

O cliente (o mesmo do modo terminal) roda num loop do asyncio em outra thread; a bandeja (pystray)
fica na thread principal. O que o modo terminal imprime vai, linha a linha, para o log em
%LOCALAPPDATA%\\Agatha\\client.log (com rotação). O ícone mostra o estado pela cor:
verde ouvindo, azul falando, âmbar microfone pausado, cinza conectando ou sem conexão, vermelho erro.
"""

import asyncio
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .controls import CONNECTING, ERROR, LISTENING, OFFLINE, SPEAKING, Controls

log = logging.getLogger("agatha.client")

COLORS = {LISTENING: (46, 158, 106), SPEAKING: (59, 130, 246), CONNECTING: (138, 143, 152),
          OFFLINE: (138, 143, 152), ERROR: (214, 69, 69), "pausado": (224, 160, 48)}
LABELS = {LISTENING: "ouvindo", SPEAKING: "falando", CONNECTING: "conectando...", OFFLINE: "sem conexão",
          ERROR: "erro (veja o log)", "pausado": "microfone pausado"}


def log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "Agatha"


class LineLogger:
    """Arquivo falso para o sys.stdout: junta o que é impresso aos pedaços (print com end=" ") e
    manda cada linha completa para o log."""

    def __init__(self, logger: logging.Logger, level: int):
        self.logger, self.level, self._buffer = logger, level, ""

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self.logger.log(self.level, line.rstrip())
        return len(text)

    def flush(self) -> None:
        pass


def setup_logging(folder: Path | None = None) -> Path:
    folder = folder or log_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "client.log"
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    sys.stdout = LineLogger(log, logging.INFO)  # no pythonw, o stdout original é None
    sys.stderr = LineLogger(log, logging.ERROR)
    return path


def icon_image(state: str, size: int = 64):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, size - 3, size - 3), fill=COLORS.get(state, COLORS[OFFLINE]) + (255,))
    # um "A" de traços (sem depender de fonte instalada)
    w = max(3, size // 12)
    top, left, right, bottom = size * 0.22, size * 0.28, size * 0.72, size * 0.78
    draw.line([(left, bottom), (size / 2, top), (right, bottom)], fill=(255, 255, 255, 255), width=w, joint="curve")
    draw.line([(size * 0.37, size * 0.58), (size * 0.63, size * 0.58)], fill=(255, 255, 255, 255), width=w)
    return img


def shown_state(controls: Controls) -> str:
    return "pausado" if controls.muted and controls.state in (LISTENING, SPEAKING) else controls.state


class TrayApp:
    def __init__(self, client_main, has_barge_in: bool, log_path: Path):
        """`client_main(controls)` é a corrotina do cliente (o `run` do modo terminal)."""
        self.client_main = client_main
        self.has_barge_in = has_barge_in
        self.log_path = log_path
        self.controls = Controls(on_status=lambda state: self._refresh(), barge_in=has_barge_in)
        self.icon = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None

    def run(self) -> None:
        import pystray

        item = pystray.MenuItem
        menu = pystray.Menu(
            item(lambda _: f"Agatha: {LABELS.get(shown_state(self.controls), self.controls.state)}", None,
                 enabled=False),
            pystray.Menu.SEPARATOR,
            item("Pausar o microfone", self._toggle_mute, checked=lambda _: self.controls.muted),
            item("Interromper pelo nome (barge-in)", self._toggle_barge_in, checked=lambda _: self.controls.barge_in,
                 enabled=self.has_barge_in),
            item("Abrir o log", self._open_log),
            pystray.Menu.SEPARATOR,
            item("Sair", self._quit),
        )
        self.icon = pystray.Icon("agatha", icon_image(CONNECTING), "Agatha: conectando...", menu)
        threading.Thread(target=self._client_thread, name="agatha-client", daemon=True).start()
        self.icon.run()

    def _client_thread(self) -> None:
        from .audio import FatalError

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._task = self._loop.create_task(self.client_main(self.controls))
        try:
            self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        except FatalError as e:
            log.error("erro: %s", e)
            self.controls.status(ERROR)
            self._notify(str(e).splitlines()[0])
        except Exception:
            log.exception("o cliente parou com um erro inesperado")
            self.controls.status(ERROR)
            self._notify("o cliente parou com um erro; veja o log")

    def _refresh(self) -> None:
        if self.icon is None:
            return
        state = shown_state(self.controls)
        self.icon.icon = icon_image(state)
        self.icon.title = f"Agatha: {LABELS.get(state, state)}"
        self.icon.update_menu()

    def _notify(self, message: str) -> None:
        if self.icon is not None:
            try:
                self.icon.notify(message, "Agatha")
            except Exception:  # noqa: BLE001 (notificação é cortesia; o log já tem o erro)
                pass

    def _toggle_mute(self, icon, item) -> None:
        self.controls.muted = not self.controls.muted
        log.info("microfone %s", "pausado" if self.controls.muted else "ligado de novo")
        self._refresh()

    def _toggle_barge_in(self, icon, item) -> None:
        self.controls.barge_in = not self.controls.barge_in
        log.info("barge-in %s", "ligado" if self.controls.barge_in else "desligado")
        self._refresh()

    def _open_log(self, icon, item) -> None:
        if sys.platform == "win32":
            os.startfile(self.log_path)  # noqa: S606 (abre no editor padrão)

    def _quit(self, icon, item) -> None:
        log.info("saindo pela bandeja")
        if self._loop is not None and self._task is not None:
            self._loop.call_soon_threadsafe(self._task.cancel)
        icon.stop()
