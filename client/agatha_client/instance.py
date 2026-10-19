"""Uma instância só do cliente: duas disputariam o microfone e a conexão (ADR-021)."""

import os
import sys
from pathlib import Path

ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    """`acquire()` devolve False se outro cliente já está rodando. No Windows, um mutex nomeado
    (some sozinho quando o processo morre); nos outros sistemas, `flock` num arquivo de trava."""

    def __init__(self, name: str = "AgathaClient", lock_dir: Path | None = None):
        self.name = name
        self.lock_dir = lock_dir
        self._handle = None

    def acquire(self) -> bool:
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            handle = kernel32.CreateMutexW(None, False, f"Local\\{self.name}")
            if not handle or ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
                if handle:
                    kernel32.CloseHandle(ctypes.c_void_p(handle))
                return False
            self._handle = handle
            return True
        import fcntl

        lock_dir = self.lock_dir or Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
        f = open(lock_dir / f"{self.name}.lock", "w")  # noqa: SIM115 (fica aberto enquanto o cliente roda)
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._handle = f
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        if sys.platform == "win32":
            import ctypes

            ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(self._handle))
        else:
            self._handle.close()
        self._handle = None
