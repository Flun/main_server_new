"""First-run PawnIO installer for the standalone Windows fan backend."""

from __future__ import annotations

import ctypes
import base64
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any


IS_WINDOWS = os.name == "nt"
VERSION = "2.2.0"
DOWNLOAD_URL = f"https://github.com/namazso/PawnIO.Setup/releases/download/{VERSION}/PawnIO_setup.exe"
SHA256 = "1F519A22E47187F70A1379A48CA604981C4FCF694F4E65B734AAA74A9FBA3032"
REGISTRY_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO"
TASK_NAME = "MainServerFanHelper"
BASE_DIR = Path(__file__).resolve().parent
HELPER_PATH = BASE_DIR / "fan_helper" / "dist" / "MainServer.FanHelper.exe"
TOKEN_PATH = HELPER_PATH.parent / "fan_helper_secret.txt"
ELEVATED_SCRIPT = BASE_DIR / "fan_helper" / "bootstrap_elevated.ps1"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
POWERSHELL = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")

_lock = threading.RLock()
_thread: threading.Thread | None = None
_state: dict[str, Any] = {
    "status": "idle", "installed": False, "version": None,
    "task_registered": False, "message": None, "error": None, "reboot_required": False,
}


def installed_version() -> str | None:
    if not IS_WINDOWS:
        return None
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, REGISTRY_KEY, 0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            return str(winreg.QueryValueEx(key, "DisplayVersion")[0])
    except OSError:
        return None


def task_registered() -> bool:
    if not IS_WINDOWS:
        return False
    result = subprocess.run(
        ["schtasks.exe", "/Query", "/TN", TASK_NAME, "/XML"],
        capture_output=True, text=True,
        timeout=15, creationflags=NO_WINDOW,
    )
    if result.returncode != 0 or not TOKEN_PATH.is_file():
        return False
    xml = result.stdout.lower()
    return "--tcp" in xml and "--token-file" in xml


def start_task() -> bool:
    if not task_registered():
        return False
    result = subprocess.run(
        ["schtasks.exe", "/Run", "/TN", TASK_NAME],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=15, creationflags=NO_WINDOW,
    )
    return result.returncode == 0


def get_status() -> dict[str, Any]:
    version = installed_version()
    task = task_registered()
    with _lock:
        result = dict(_state)
    if version:
        result.update({"installed": True, "version": version, "task_registered": task})
        if task and result["status"] not in {"installing", "downloading"}:
            result["status"] = "ready"
            result["message"] = "PawnIO 및 관리자 팬 헬퍼 준비 완료"
            result["error"] = None
        elif not task and result["status"] not in {"installing", "downloading"}:
            result["status"] = "idle"
            result["message"] = "관리자 팬 헬퍼 등록 준비 중"
    return result


def _set(**values: Any) -> None:
    with _lock:
        _state.update(values)


def _verify_signature(path: Path) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest().upper()
    if digest != SHA256:
        raise RuntimeError(f"PawnIO 설치파일 SHA-256 불일치: {digest}")
    script = (
        "$s=Get-AuthenticodeSignature -LiteralPath $args[0];"
        "if($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch 'namazso'){"
        "Write-Error ($s.Status.ToString()+' / '+$s.SignerCertificate.Subject); exit 1}"
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script, str(path)],
        capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError(f"PawnIO Authenticode 서명 검증 실패: {(result.stderr or result.stdout).strip()}")


def _run_elevated_bootstrap(installer: Path | None) -> int:
    is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    if is_admin:
        command = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ELEVATED_SCRIPT), "-HelperPath", str(HELPER_PATH)]
        if installer:
            command += ["-InstallerPath", str(installer)]
        result = subprocess.run(command, timeout=240, creationflags=NO_WINDOW)
        return result.returncode

    # A signed kernel driver cannot be installed without elevation. This creates
    # the one expected UAC prompt on first launch and waits for the signed setup.
    def ps_literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    elevated = f"& {ps_literal(str(ELEVATED_SCRIPT))} -HelperPath {ps_literal(str(HELPER_PATH))}"
    if installer:
        elevated += f" -InstallerPath {ps_literal(str(installer))}"
    encoded = base64.b64encode(elevated.encode("utf-16le")).decode("ascii")
    script = (
        f"$p=Start-Process -FilePath {ps_literal(POWERSHELL)} "
        f"-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand','{encoded}' "
        "-Verb RunAs -WindowStyle Hidden -Wait -PassThru; exit $p.ExitCode"
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-Command", script],
        capture_output=True, text=True, timeout=240, creationflags=NO_WINDOW,
    )
    return result.returncode


def _install() -> None:
    if not IS_WINDOWS:
        return
    current = installed_version()
    if current and task_registered():
        start_task()
        _set(status="ready", installed=True, version=current, task_registered=True, message="PawnIO 및 관리자 팬 헬퍼 준비 완료", error=None)
        return

    installer = Path(tempfile.gettempdir()) / f"main-server-PawnIO-{VERSION}.exe"
    downloaded = False
    try:
        if not current:
            _set(status="downloading", message="PawnIO 공식 설치파일 다운로드 중", error=None)
            with urllib.request.urlopen(DOWNLOAD_URL, timeout=30) as source, installer.open("wb") as target:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
            downloaded = True
            _verify_signature(installer)
        _set(status="installing", message="드라이버/관리자 팬 헬퍼 등록 권한을 요청했습니다. Windows UAC에서 허용하세요")
        exit_code = _run_elevated_bootstrap(installer if downloaded else None)
        if exit_code not in {0, 3010}:
            raise RuntimeError(f"PawnIO 설치가 완료되지 않았습니다 (exit {exit_code})")
        for _ in range(20):
            current = installed_version()
            if current:
                break
            time.sleep(0.5)
        if not current:
            raise RuntimeError("설치 프로그램은 완료됐지만 PawnIO 등록을 찾지 못했습니다")
        if not task_registered():
            raise RuntimeError("관리자 팬 헬퍼 작업을 등록하지 못했습니다")
        start_task()
        _set(
            status="ready", installed=True, version=current, task_registered=True,
            reboot_required=exit_code == 3010,
            message="PawnIO 및 관리자 팬 헬퍼 준비 완료" + (" — Windows 재시작이 필요합니다" if exit_code == 3010 else ""),
            error=None,
        )
    except Exception as error:
        _set(status="error", installed=False, message="PawnIO 자동 설치 실패", error=str(error))
    finally:
        try:
            installer.unlink(missing_ok=True)
        except OSError:
            pass


def ensure_async() -> dict[str, Any]:
    global _thread
    if not IS_WINDOWS:
        return get_status()
    with _lock:
        if _thread and _thread.is_alive():
            return get_status()
        if installed_version() and task_registered():
            current = installed_version()
            start_task()
            _set(status="ready", installed=True, version=current, task_registered=True, message="PawnIO 및 관리자 팬 헬퍼 준비 완료", error=None)
            return get_status()
        _thread = threading.Thread(target=_install, name="pawnio-bootstrap", daemon=True)
        _thread.start()
    return get_status()
