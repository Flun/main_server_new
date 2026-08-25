"""Frontend-facing orchestration for the Windows CMP 170HX unlock controller."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from config import BASE_DIR, IS_WINDOWS


ROOT = Path(BASE_DIR)
STATE_PATH = ROOT / "logs" / "cmp170_direct_state.json"
LOG_PATH = ROOT / "logs" / "cmp170_direct_unlock.log"
CONTROLLER_PATH = ROOT / "cmp170_direct_unlock.py"
ELEVATED_SCRIPT = ROOT / "manage_cmp170_unlock.ps1"
TASK_NAME = "CMP170HXUnlock"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0

_operation_lock = threading.Lock()
_operation = {"running": False, "action": "", "started_at": 0.0, "error": ""}
_staged_cache = {"value": "", "checked_at": 0.0}


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _tail(path: Path, limit: int = 35) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-limit:]
    except Exception:
        return []


def _cmp_memory() -> int | None:
    if not IS_WINDOWS:
        return None
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=pci.device_id,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8, creationflags=NO_WINDOW,
        )
        for line in result.stdout.splitlines():
            fields = [item.strip() for item in line.split(",")]
            if len(fields) >= 2 and "20c2" in fields[0].lower():
                return int(float(fields[1]))
    except Exception:
        pass
    return None


def _staged_inf() -> str:
    """Find the retained 170_boot package without depending on localized labels."""
    if not IS_WINDOWS:
        return ""
    if time.time() - _staged_cache["checked_at"] < 60:
        return _staged_cache["value"]
    try:
        result = subprocess.run(
            ["pnputil", "/enum-drivers"], capture_output=True, text=True,
            timeout=15, creationflags=NO_WINDOW, errors="replace",
        )
        blocks = re.split(r"\r?\n\s*\r?\n", result.stdout)
        for block in blocks:
            if "170_boot.inf" in block.lower() or "170_boot" in block.lower():
                match = re.search(r"\boem\d+\.inf\b", block, re.IGNORECASE)
                if match:
                    _staged_cache.update(value=match.group(0).lower(), checked_at=time.time())
                    return _staged_cache["value"]
    except Exception:
        pass
    state = _read_json(STATE_PATH)
    saved = str(state.get("temporary_inf") or "")
    if re.fullmatch(r"oem\d+\.inf", saved, re.IGNORECASE):
        _staged_cache.update(value=saved.lower(), checked_at=time.time())
        return _staged_cache["value"]
    _staged_cache.update(value="", checked_at=time.time())
    return ""


def autostart_enabled() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", TASK_NAME, "/XML"],
            capture_output=True, text=True, timeout=8, creationflags=NO_WINDOW,
        )
        # Task Scheduler omits <Enabled> when it has the schema-default value
        # (true).  Only an explicit false means disabled.
        return result.returncode == 0 and "<Enabled>false</Enabled>" not in result.stdout
    except Exception:
        return False


def status(include_log: bool = True) -> dict:
    state = _read_json(STATE_PATH)
    memory = _cmp_memory()
    with _operation_lock:
        operation = dict(_operation)
    unlocked = memory is not None and memory >= 60000
    detected = memory is not None or "20C2" in str(state.get("instance_id", "")).upper()
    message = "CMP 170HX 64GB 언락 완료" if unlocked else (
        "CMP 170HX가 감지됐지만 64GB 언락 상태가 아닙니다" if detected else "CMP 170HX를 찾지 못했습니다"
    )
    if operation["running"]:
        labels = {"run": "언락", "register": "자동 적용 등록", "unregister": "자동 적용 해제"}
        message = f"{labels.get(operation['action'], operation['action'])} 작업 진행 중"
    elif operation["error"]:
        message = operation["error"]
    return {
        "available": IS_WINDOWS and CONTROLLER_PATH.exists() and ELEVATED_SCRIPT.exists(),
        "detected": detected,
        "unlocked": unlocked,
        "memory_mib": memory,
        "staged_inf": _staged_inf(),
        "autostart": autostart_enabled(),
        "running": operation["running"],
        "action": operation["action"],
        "status": state.get("status", "unknown"),
        "updated_at": state.get("updated_at"),
        "message": message,
        "last_log": _tail(LOG_PATH) if include_log else [],
    }


def wait_until_unlocked(timeout: int = 180) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        memory = _cmp_memory()
        if memory is not None and memory >= 60000:
            return True
        time.sleep(2)
    return False


def _pythonw_path() -> Path:
    executable = Path(sys.executable).resolve()
    candidate = executable.with_name("pythonw.exe")
    return candidate if candidate.exists() else executable


def _run_operation(action: str) -> None:
    try:
        command = [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(ELEVATED_SCRIPT), "-Action", action,
            "-PythonPath", str(_pythonw_path()), "-ControllerPath", str(CONTROLLER_PATH),
            "-WorkingDirectory", str(ROOT),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=300, creationflags=NO_WINDOW)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "관리자 권한 작업이 취소되거나 실패했습니다").strip()
            raise RuntimeError(detail[-800:])
    except Exception as error:
        with _operation_lock:
            _operation["error"] = str(error)
    finally:
        with _operation_lock:
            _operation["running"] = False


def start(action: str) -> dict:
    if not IS_WINDOWS:
        raise RuntimeError("Windows에서만 사용할 수 있습니다")
    if action not in {"run", "register", "unregister"}:
        raise ValueError("지원하지 않는 작업입니다")
    if not CONTROLLER_PATH.exists() or not ELEVATED_SCRIPT.exists():
        raise RuntimeError("CMP 170HX 컨트롤러 파일이 없습니다")
    with _operation_lock:
        if _operation["running"]:
            raise RuntimeError("다른 CMP 170HX 작업이 이미 진행 중입니다")
        _operation.update(running=True, action=action, started_at=time.time(), error="")
    threading.Thread(target=_run_operation, args=(action,), name=f"cmp170-{action}", daemon=True).start()
    return status(include_log=False)
