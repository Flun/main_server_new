"""Selectable, repeatable Ubuntu bootstrap exposed by the integrated settings page."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from config import BASE_DIR
import nas_mount


ROOT_HELPER = "/usr/local/sbin/main-server-linux-setup"
SERVICE_TEMPLATE = Path(BASE_DIR) / "system" / "main_server.service"
SERVICE_FILE = Path.home() / ".config" / "systemd" / "user" / "main_server.service"
MODEL_MOUNT = Path("/mnt/main-server-models")
COMFY_MODEL_MOUNT = Path("/mnt/main-server-comfy")
GRUB_CONSOLE_CONFIG = Path("/etc/default/grub.d/99-main-server-consoleblank.cfg")
ALLOWED = {
    "packages", "cli_boot", "console_blank", "ssh", "linger",
    "manager_service", "model_mounts", "nas", "gpu_services",
}

STATE: dict[str, Any] = {
    "busy": False, "ok": None, "message": "", "log": [],
    "started_at": 0.0, "finished_at": 0.0,
}
LOCK = threading.Lock()


def _run(command: list[str], *, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, capture_output=True, text=True, errors="replace", timeout=timeout,
    )


def _systemctl(*args: str, user: bool = False) -> bool:
    command = ["systemctl"]
    if user:
        command.append("--user")
    return _run([*command, *args], timeout=30).returncode == 0


def _mount_source(target: Path) -> str:
    result = _run(["findmnt", "-n", "-o", "SOURCE", "--target", str(target)], timeout=10)
    return result.stdout.strip() if result.returncode == 0 else ""


def status() -> dict[str, Any]:
    if os.name == "nt":
        return {"available": False, "platform": "windows", "state": dict(STATE), "items": {}}
    console_runtime = ""
    try:
        console_runtime = Path("/sys/module/kernel/parameters/consoleblank").read_text().strip()
    except OSError:
        pass
    linger = _run(["loginctl", "show-user", str(os.getuid()), "-p", "Linger", "--value"], timeout=10)
    nas_state = nas_mount.status()
    items = {
        "packages": {
            "label": "Linux 필수 패키지", "applied": all(shutil.which(name) for name in ("ffmpeg", "mount.cifs", "smbclient", "cmake", "ninja")),
            "detail": "Python venv/dev · ffmpeg · CIFS/SMB · NTFS · 빌드 도구",
        },
        "cli_boot": {
            "label": "CLI 기본 부팅", "applied": _run(["systemctl", "get-default"], timeout=10).stdout.strip() == "multi-user.target",
            "detail": "재부팅 시 GUI 대신 tty1로 시작",
        },
        "console_blank": {
            "label": "CLI 모니터 5분 절전", "applied": "consoleblank=300" in GRUB_CONSOLE_CONFIG.read_text(errors="ignore") if GRUB_CONSOLE_CONFIG.is_file() else False,
            "detail": f"현재 커널 consoleblank={console_runtime or '?'}초",
        },
        "ssh": {
            "label": "SSH 원격 접속", "applied": _systemctl("is-enabled", "ssh.service") and _systemctl("is-active", "ssh.service"),
            "detail": "openssh-server 설치 및 부팅 자동 시작",
        },
        "linger": {
            "label": "로그인 없는 사용자 서비스", "applied": linger.stdout.strip() == "yes",
            "detail": "systemd linger 활성화",
        },
        "manager_service": {
            "label": "main_server 자동 실행", "applied": _systemctl("is-enabled", "main_server.service", user=True),
            "detail": "사용자 systemd 서비스 등록",
        },
        "model_mounts": {
            "label": "모델 디스크 고정 마운트", "applied": bool(_mount_source(MODEL_MOUNT) and _mount_source(COMFY_MODEL_MOUNT)),
            "detail": f"{MODEL_MOUNT} · {COMFY_MODEL_MOUNT}",
        },
        "nas": {
            "label": "NAS 자동 마운트", "applied": bool(nas_state.get("mounted")),
            "detail": "SMB 도구·권한·마운트 지점 및 저장 계정으로 연결",
        },
        "gpu_services": {
            "label": "GPU·팬 부팅 설정", "applied": _systemctl("is-enabled", "gpu-tune.service"),
            "detail": "GPU 전력/클럭 및 nct6775 메인보드 팬",
        },
    }
    return {
        "available": True, "platform": "linux", "helper_installed": Path(ROOT_HELPER).is_file(),
        "state": dict(STATE), "items": items,
    }


def _install_manager_service() -> None:
    source = SERVICE_TEMPLATE.read_text(encoding="utf-8")
    source = source.replace("/srv/main_server_new", str(BASE_DIR))
    SERVICE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = SERVICE_FILE.with_suffix(".service.tmp")
    temporary.write_text(source, encoding="utf-8")
    os.replace(temporary, SERVICE_FILE)
    result = _run(["systemctl", "--user", "daemon-reload"], timeout=30)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "사용자 systemd reload 실패")
    result = _run(["systemctl", "--user", "enable", "main_server.service"], timeout=30)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "main_server 서비스 활성화 실패")


def _append_log(text: str) -> None:
    for line in str(text or "").splitlines():
        STATE["log"].append(line)
    STATE["log"] = STATE["log"][-300:]


def _worker(selected: list[str]) -> None:
    try:
        root_actions = [item for item in selected if item not in {"manager_service"}]
        if root_actions:
            STATE["message"] = "시스템 설정 적용 중"
            result = _run(["sudo", "-n", ROOT_HELPER, "apply", *root_actions])
            _append_log(result.stdout)
            _append_log(result.stderr)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Linux 시스템 설정 실패")
        if "manager_service" in selected:
            STATE["message"] = "main_server 서비스 등록 중"
            _install_manager_service()
            _append_log("main_server 사용자 서비스 등록 완료")
        if "nas" in selected:
            configured = nas_mount.save_settings({"enabled": True})
            if nas_mount.status().get("credentials_saved"):
                STATE["message"] = "NAS 연결 중"
                snapshot = nas_mount.mount()
                _append_log(f"NAS 연결 완료: {snapshot.get('remote')} -> {snapshot.get('main_local')}")
            else:
                _append_log("NAS 계정이 아직 저장되지 않아 자동 연결은 건너뜁니다")
        STATE.update(ok=True, message="선택한 Linux 설정 적용 완료")
    except Exception as error:
        _append_log(str(error))
        STATE.update(ok=False, message=f"Linux 설정 실패: {error}")
    finally:
        STATE.update(busy=False, finished_at=time.time())


def start(values: dict[str, Any]) -> dict[str, Any]:
    if os.name == "nt":
        raise RuntimeError("Linux에서만 지원합니다")
    selected = [key for key, enabled in values.items() if enabled is True]
    invalid = sorted(set(selected) - ALLOWED)
    if invalid:
        raise RuntimeError("허용되지 않은 설정 항목: " + ", ".join(invalid))
    if not selected:
        raise RuntimeError("적용할 항목을 하나 이상 선택하세요")
    if not Path(ROOT_HELPER).is_file():
        raise RuntimeError("Linux 설정 helper가 없습니다. 터미널에서 ./setup_env.sh를 먼저 실행하세요")
    with LOCK:
        if STATE["busy"]:
            raise RuntimeError("Linux 설정 작업이 이미 진행 중입니다")
        STATE.update(busy=True, ok=None, message="대기 중", log=[], started_at=time.time(), finished_at=0.0)
        threading.Thread(target=_worker, args=(selected,), daemon=True, name="linux-setup").start()
    return dict(STATE)
