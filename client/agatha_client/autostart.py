"""Iniciar a Agatha com o Windows (ADR-021): atalho na pasta de inicialização do usuário, rodando
`pythonw -m agatha_client --tray` (sem janela) na pasta do cliente, onde estão o .env e os modelos."""

import os
import subprocess
import sys
from pathlib import Path

SHORTCUT = "Agatha.lnk"


def startup_dir() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def shortcut_script(link: Path, pythonw: Path, workdir: Path, extra_args: str = "") -> str:
    """PowerShell que cria o atalho (o WScript.Shell vem com o Windows: sem dependência nova)."""
    args = f"-m agatha_client --tray {extra_args}".strip()
    return (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(str(link))}); "
            f"$s.TargetPath = {_ps_quote(str(pythonw))}; $s.Arguments = {_ps_quote(args)}; "
            f"$s.WorkingDirectory = {_ps_quote(str(workdir))}; $s.Description = 'Agatha (assistente de voz)'; "
            f"$s.Save()")


def enable(extra_args: str = "") -> Path:
    if sys.platform != "win32":
        raise RuntimeError("iniciar com o sistema só está implementado no Windows")
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    link = startup_dir() / SHORTCUT
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                    shortcut_script(link, pythonw, Path.cwd(), extra_args)], check=True)
    return link


def disable() -> bool:
    link = startup_dir() / SHORTCUT
    if link.exists():
        link.unlink()
        return True
    return False
