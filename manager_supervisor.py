"""Invisible, single-instance Windows supervisor for app.py."""

from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import sys
import time


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 8999
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0)


def _manager_online() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=1):
            return True
    except OSError:
        return False


def _single_instance() -> object | None:
    if os.name != "nt":
        return object()
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel32.CreateMutexW(None, False, "Local\\MainServerManagerSupervisor")
    if not handle or kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        if handle:
            kernel32.CloseHandle(handle)
        return None
    return handle


def main() -> int:
    mutex = _single_instance()
    if mutex is None:
        return 0
    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    # The frontend starts immediately. Device and NAS readiness is handled by
    # app.py background initialization, so no fixed boot delay is required.
    time.sleep(0.5)
    while True:
        if _manager_online():
            time.sleep(5)
            continue
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        executable = pythonw if os.path.isfile(pythonw) else sys.executable
        with open(os.path.join(BASE_DIR, "logs", "manager.log"), "a", encoding="utf-8", errors="replace") as log:
            process = subprocess.Popen(
                [executable, os.path.join(BASE_DIR, "app.py")], cwd=BASE_DIR,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                creationflags=NO_WINDOW | DETACHED,
            )
            process.wait()
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
