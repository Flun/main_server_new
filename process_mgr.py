import os
import signal
import subprocess
import time

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

class Service:
    """한 서비스(ComfyUI / llama / bot / watcher) 프로세스 관리.

    Linux: start_new_session으로 완전 분리(headless), 로그는 파일로.
    Windows: CREATE_NO_WINDOW로 창 숨김.
    """

    def __init__(self, name):
        self.name = name
        self.pid = None
        self.started_at = None
        self.device = None
        os.makedirs(LOG_DIR, exist_ok=True)
        self.log_file = os.path.join(LOG_DIR, f"{name}.log")
        self._pidfile = os.path.join(LOG_DIR, f"{name}.pid")

    # ---- 상태 ----
    def read_pidfile(self):
        try:
            with open(self._pidfile) as f:
                return int(f.read().strip())
        except Exception:
            return None

    def _pid_alive(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    def running(self):
        pid = self.read_pidfile()
        if pid and self._pid_alive(pid):
            self.pid = pid
            if self.started_at is None:
                try:
                    import psutil
                    self.started_at = psutil.Process(pid).create_time()
                except Exception:
                    pass
            return True
        self.pid = None
        self.started_at = None
        return False

    def info(self):
        if self.running():
            uptime = time.time() - self.started_at if self.started_at else 0
            return {"running": True, "pid": self.pid, "uptime": round(uptime)}
        return {"running": False, "pid": None, "uptime": 0}

    # ---- 시작/종료 ----
    def start(self, cmd, cwd=None, env=None, device=None):
        if self.running():
            raise RuntimeError(f"{self.name} 서비스가 이미 실행 중입니다 (PID {self.pid})")
        os.makedirs(LOG_DIR, exist_ok=True)
        env_full = dict(os.environ)
        if env:
            env_full.update(env)
        if device is not None:
            if isinstance(device, (list, tuple)):
                visible_devices = ",".join(str(item).strip() for item in device if str(item).strip())
            else:
                visible_devices = str(device).strip()
            if visible_devices:
                env_full["CUDA_VISIBLE_DEVICES"] = visible_devices
        log_f = open(self.log_file, "a", encoding="utf-8", errors="replace")
        log_f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 시작: {' '.join(cmd)} =====\n")
        log_f.flush()
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW
        p = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env_full,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=(os.name != "nt"),
            creationflags=creationflags,
        )
        self.pid = p.pid
        self.started_at = time.time()
        self.device = list(device) if isinstance(device, (list, tuple)) else device
        with open(self._pidfile, "w") as f:
            f.write(str(p.pid))
        return p.pid

    def stop(self):
        if not self.running():
            return False
        pid = self.pid
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            else:
                os.killpg(pid, signal.SIGTERM)
                deadline = time.time() + 6
                while time.time() < deadline and self._pid_alive(pid):
                    time.sleep(0.2)
                if self._pid_alive(pid):
                    os.killpg(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        finally:
            for f in (self._pidfile,):
                try:
                    os.remove(f)
                except OSError:
                    pass
            self.pid = None
            self.started_at = None
        return True

    def force_kill(self):
        if not self.running():
            return False
        pid = self.pid
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            else:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
        finally:
            for f in (self._pidfile,):
                try:
                    os.remove(f)
                except OSError:
                    pass
            self.pid = None
            self.started_at = None
        return True


def tail(path, n=300):
    """파일 끝에서 n줄 읽기 (바이트 기반으로 안전하게)."""
    if not os.path.exists(path):
        return []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > 1_000_000:
                f.seek(size - 1_000_000)
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()
        return lines[-n:]
    except Exception:
        return []


def find_process(pattern):
    """패턴(정규식, cmdline 매칭)에 해당하는 PID 목록 — 우리가 관리 안 하는 외부 프로세스 감지용."""
    if os.name == "nt":
        return []
    import re

    out = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pid, args = parts
        try:
            if re.search(pattern, args):
                found.append(int(pid))
        except re.error:
            continue
    return found
