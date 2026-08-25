"""Detached helper: wait for port 8999 to be released, then relaunch app.py.

/api/restart(Windows)가 이 프로세스를 DETACHED_PROCESS로 띄운 뒤 기존 manager는
즉시 종료됩니다. 새 manager가 포트 선점에 성공할 때까지 대기해 orphan/중복
프로세스를 방지합니다 (Linux에서는 systemd Restart=always가 같은 역할을 합니다).
"""

import os
import socket
import subprocess
import sys
import time

PORT = 8999
WAIT_SECONDS = 60


def _port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main():
    base = os.path.dirname(os.path.abspath(__file__))
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        if _port_free(PORT):
            break
        time.sleep(0.5)
    else:
        print(f"[manager_restarter] {PORT} 포트가 {WAIT_SECONDS}s 안에 비어지지 않아 재시작을 포기합니다")
        return 1

    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.isfile(pyw) else sys.executable
    log_dir = os.path.join(base, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log = open(os.path.join(log_dir, "manager_restart.log"), "a", encoding="utf-8")
    subprocess.Popen(
        [exe, os.path.join(base, "app.py")],
        cwd=base,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=(
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        ),
    )
    print(f"[manager_restarter] {time.strftime('%Y-%m-%d %H:%M:%S')} manager 재기동 요청 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
