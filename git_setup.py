"""Cross-platform Git/GitHub CLI installation and device authentication."""

from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


IS_WINDOWS = os.name == "nt"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
ROOT_HELPER = "/usr/local/sbin/main-server-linux-setup"
DEVICE_URL = "https://github.com/login/device"
DEVICE_CODE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b")

STATE: dict[str, Any] = {
    "busy": False,
    "action": "",
    "ok": None,
    "message": "",
    "device_url": DEVICE_URL,
    "device_code": "",
    "log": [],
    "started_at": 0.0,
    "finished_at": 0.0,
}
LOCK = threading.Lock()


def _candidate(executable: str) -> str | None:
    found = shutil.which(executable)
    if found:
        return found
    if not IS_WINDOWS:
        return None
    roots = [Path(os.environ.get("ProgramFiles", r"C:\Program Files"))]
    if os.environ.get("LOCALAPPDATA"):
        roots.append(Path(os.environ["LOCALAPPDATA"]))
    relative = {
        "git": [Path("Git/cmd/git.exe"), Path("Microsoft/WinGet/Links/git.exe")],
        "gh": [Path("GitHub CLI/gh.exe"), Path("Microsoft/WinGet/Links/gh.exe")],
    }
    for root in roots:
        for item in relative.get(executable, []):
            path = root / item
            if path.is_file():
                return str(path)
    return None


def _run(command: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
    )


def _authenticated(gh: str | None) -> tuple[bool, str]:
    if not gh:
        return False, ""
    try:
        auth = _run([gh, "auth", "status", "--hostname", "github.com"], timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False, ""
    if auth.returncode:
        return False, ""
    output = "\n".join((auth.stdout, auth.stderr))
    account = re.search(r"\baccount\s+([^\s(]+)", output, re.IGNORECASE)
    return True, account.group(1) if account else ""


def _version(executable: str | None, *args: str) -> str:
    if not executable:
        return ""
    try:
        return _run([executable, *args], timeout=10).stdout.splitlines()[0].strip()
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""


def status() -> dict[str, Any]:
    git = _candidate("git")
    gh = _candidate("gh")
    authenticated, account = _authenticated(gh)
    return {
        "platform": "windows" if IS_WINDOWS else "linux",
        "git_installed": bool(git),
        "git_version": _version(git, "--version"),
        "gh_installed": bool(gh),
        "gh_version": _version(gh, "--version"),
        "authenticated": authenticated,
        "account": account,
        "state": dict(STATE),
    }


def _append_log(value: str) -> None:
    for line in str(value or "").replace("\r", "\n").splitlines():
        line = line.strip()
        if not line:
            continue
        STATE["log"].append(line)
        match = DEVICE_CODE.search(line)
        if match:
            STATE["device_code"] = match.group(0)
            STATE["message"] = "GitHub에서 기기 코드를 승인하세요"
    STATE["log"] = STATE["log"][-100:]


def _finish(ok: bool, message: str) -> None:
    STATE.update(busy=False, ok=ok, message=message, finished_at=time.time())


def _install_worker() -> None:
    try:
        STATE["message"] = "Git과 GitHub CLI 설치 중"
        if IS_WINDOWS:
            winget = shutil.which("winget")
            if not winget:
                raise RuntimeError("winget이 없습니다. Microsoft App Installer를 설치하세요")
            for package in ("Git.Git", "GitHub.cli"):
                command = [
                    winget, "install", "--id", package, "--exact", "--source", "winget",
                    "--accept-package-agreements", "--accept-source-agreements", "--silent",
                ]
                result = _run(command, timeout=1800)
                _append_log(result.stdout)
                _append_log(result.stderr)
                # winget may report an already-installed package with a nonzero
                # code on older releases, so verify the executable before failing.
                expected = "git" if package == "Git.Git" else "gh"
                if result.returncode and not _candidate(expected):
                    raise RuntimeError(f"{package} 설치 실패: {result.stderr.strip() or result.stdout.strip()}")
        else:
            if not Path(ROOT_HELPER).is_file():
                raise RuntimeError("Linux 설정 helper가 없습니다. ./setup_env.sh를 먼저 실행하세요")
            result = _run(["sudo", "-n", ROOT_HELPER, "apply", "git_tools"], timeout=1800)
            _append_log(result.stdout)
            _append_log(result.stderr)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Git 도구 설치 실패")
        if not _candidate("git") or not _candidate("gh"):
            raise RuntimeError("설치는 끝났지만 Git 또는 GitHub CLI 실행 파일을 찾지 못했습니다")
        _finish(True, "Git과 GitHub CLI 설치 완료")
    except Exception as error:
        _append_log(str(error))
        _finish(False, f"Git 도구 설치 실패: {error}")


def _auth_worker() -> None:
    process: subprocess.Popen[str] | None = None
    try:
        gh = _candidate("gh")
        git = _candidate("git")
        if not gh or not git:
            raise RuntimeError("먼저 Git과 GitHub CLI를 설치하세요")
        authenticated, account = _authenticated(gh)
        if authenticated:
            configured = _run([gh, "auth", "setup-git"], timeout=30)
            if configured.returncode:
                raise RuntimeError(configured.stderr.strip() or "Git credential 연결 실패")
            _finish(True, f"이미 GitHub 계정 {account or ''}에 인증되어 있습니다".strip())
            return

        env = os.environ.copy()
        if not IS_WINDOWS and Path("/usr/bin/true").is_file():
            # The dashboard provides the URL; do not try to launch a browser in
            # a headless CLI boot session.
            env["GH_BROWSER"] = "/usr/bin/true"
        process = subprocess.Popen(
            [gh, "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=NO_WINDOW,
        )
        if process.stdin:
            process.stdin.write("\n")
            process.stdin.flush()
        assert process.stdout is not None
        output_queue: queue.Queue[str | None] = queue.Queue()

        def read_output() -> None:
            assert process is not None and process.stdout is not None
            for output_line in process.stdout:
                output_queue.put(output_line)
            output_queue.put(None)

        threading.Thread(target=read_output, daemon=True, name="gh-auth-output").start()
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            try:
                line = output_queue.get(timeout=0.25)
            except queue.Empty:
                if process.poll() is not None:
                    break
                continue
            if line is None:
                break
            _append_log(line)
        if process.poll() is None:
            process.terminate()
            raise RuntimeError("GitHub 인증 대기 시간이 15분을 초과했습니다")
        if process.returncode:
            raise RuntimeError("GitHub 인증이 취소되었거나 실패했습니다")
        configured = _run([gh, "auth", "setup-git"], timeout=30)
        _append_log(configured.stdout)
        _append_log(configured.stderr)
        if configured.returncode:
            raise RuntimeError(configured.stderr.strip() or "Git credential 연결 실패")
        authenticated, account = _authenticated(gh)
        if not authenticated:
            raise RuntimeError("인증 완료 상태를 확인하지 못했습니다")
        _finish(True, f"GitHub 계정 {account or ''} 연결 완료".strip())
    except Exception as error:
        if process and process.poll() is None:
            process.terminate()
        _append_log(str(error))
        _finish(False, f"GitHub 인증 실패: {error}")


def _start(action: str, target) -> dict[str, Any]:
    with LOCK:
        if STATE["busy"]:
            raise RuntimeError("Git 설정 작업이 이미 진행 중입니다")
        STATE.update(
            busy=True,
            action=action,
            ok=None,
            message="시작 중",
            device_url=DEVICE_URL,
            device_code="",
            log=[],
            started_at=time.time(),
            finished_at=0.0,
        )
        threading.Thread(target=target, daemon=True, name=f"git-{action}").start()
    return dict(STATE)


def start_install() -> dict[str, Any]:
    return _start("install", _install_worker)


def start_auth() -> dict[str, Any]:
    return _start("auth", _auth_worker)
