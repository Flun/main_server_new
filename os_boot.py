"""One-shot UEFI OS selection for dual-boot Windows/Ubuntu hosts."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request


router = APIRouter(prefix="/api/os-boot", tags=["os-boot"])
IS_WINDOWS = os.name == "nt"
LINUX_HELPER = Path("/usr/local/sbin/main-server-os-boot")
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0


def _run(command: list[str], timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, capture_output=True, text=True, errors="replace", timeout=timeout,
        stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
    )


def _linux_request(action: str, target: str | None = None) -> dict[str, Any]:
    if not LINUX_HELPER.is_file():
        raise RuntimeError(f"OS 전환 helper가 설치되지 않았습니다: {LINUX_HELPER}")
    command = ["sudo", "-n", str(LINUX_HELPER), action]
    if target:
        command.append(target)
    result = _run(command, timeout=30)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "UEFI 부팅 대상 설정 실패")
    try:
        response = json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError(f"OS 전환 helper 응답을 해석하지 못했습니다: {result.stdout.strip()}") from error
    if not isinstance(response, dict):
        raise RuntimeError("OS 전환 helper 응답 형식이 올바르지 않습니다")
    return response


def _windows_firmware_entries() -> dict[str, Any]:
    # The already elevated, token-authenticated motherboard helper also owns
    # UEFI changes. This keeps the web server itself non-administrative.
    try:
        from motherboard_fan import controller
        return controller.helper.request({"command": "os_boot_status"}, timeout=15.0)
    except Exception:
        # Read-only status still works before the upgraded helper is installed.
        pass
    bcdedit = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "bcdedit.exe")
    result = _run([bcdedit, "/enum", "firmware", "/v"])
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "Windows UEFI 항목 조회 실패")
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw in result.stdout.splitlines() + [""]:
        line = raw.strip()
        identifier = re.search(r"\{[0-9a-fA-F-]{36}\}|\{(?:bootmgr|fwbootmgr)\}", line)
        if identifier:
            if current.get("id"):
                entries.append(current)
            current = {"id": identifier.group(0), "description": ""}
        elif current and line and not current.get("description"):
            # Description labels are localized, but their values contain the
            # stable vendor names Windows Boot Manager or Ubuntu.
            lowered = line.lower()
            if "windows boot manager" in lowered:
                current["description"] = "Windows Boot Manager"
            elif "ubuntu" in lowered:
                current["description"] = "Ubuntu"
    windows = next((item for item in entries if "windows boot manager" in item.get("description", "").lower()), None)
    linux = next((item for item in entries if "ubuntu" in item.get("description", "").lower()), None)
    return {
        "available": bool(windows and linux), "platform": "windows", "current": "windows",
        "targets": {"windows": windows, "linux": linux},
        "error": None if windows and linux else "Windows Boot Manager 또는 Ubuntu UEFI 항목을 찾지 못했습니다",
    }


def status() -> dict[str, Any]:
    if IS_WINDOWS:
        return _windows_firmware_entries()
    return _linux_request("status")


def _windows_set_next(target: str) -> dict[str, Any]:
    try:
        from motherboard_fan import controller
        return controller.helper.request({"command": "os_boot_set", "target": target}, timeout=15.0)
    except Exception as helper_error:
        # An elevated main_server process can still perform the operation
        # directly; normal installations use the helper path above.
        helper_detail = str(helper_error)
    state = _windows_firmware_entries()
    entry = state.get("targets", {}).get(target)
    if not state.get("available") or not entry:
        raise RuntimeError(state.get("error") or f"{target} UEFI 항목이 없습니다")
    bcdedit = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "bcdedit.exe")
    result = _run([bcdedit, "/set", "{fwbootmgr}", "bootsequence", entry["id"]])
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            "Windows 관리자 OS 전환 helper를 사용할 수 없습니다. setup_env.bat를 다시 실행해야 합니다."
            + (f" (helper: {helper_detail})" if helper_detail else "")
            + (f" ({detail})" if detail else "")
        )
    return {"ok": True, "target": target, "entry": entry}


@router.get("")
def os_boot_status():
    try:
        return status()
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        return {
            "available": False, "platform": "windows" if IS_WINDOWS else "linux",
            "current": "windows" if IS_WINDOWS else "linux", "targets": {}, "error": str(error),
        }


@router.post("")
def os_boot_set(payload: dict | None, request: Request):
    values = payload or {}
    target = str(values.get("target") or "")
    action = str(values.get("action") or "reboot")
    if target not in {"windows", "linux"}:
        raise HTTPException(400, "target은 windows 또는 linux여야 합니다")
    if action not in {"schedule", "reboot"}:
        raise HTTPException(400, "action은 schedule 또는 reboot여야 합니다")
    if action == "reboot" and request.headers.get("X-OS-Boot-Confirm") != "confirmed":
        raise HTTPException(409, "OS 전환 재부팅 확인 헤더가 필요합니다")
    try:
        result = _windows_set_next(target) if IS_WINDOWS else _linux_request("set", target)
        if action == "reboot":
            if IS_WINDOWS:
                shutdown = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "shutdown.exe")
                subprocess.Popen(
                    [shutdown, "/r", "/t", "3", "/d", "p:0:0", "/c", f"main_server: {target} 전환"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=NO_WINDOW,
                )
            else:
                _linux_request("reboot")
        return {**result, "action": action, "rebooting": action == "reboot"}
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        raise HTTPException(500, str(error)) from error


@router.post("/system-power")
def system_power(payload: dict | None, request: Request):
    action = str((payload or {}).get("action") or "")
    if action not in {"restart", "shutdown"}:
        raise HTTPException(400, "action은 restart 또는 shutdown이어야 합니다")
    if request.headers.get("X-System-Power-Confirm") != "confirmed":
        raise HTTPException(409, "시스템 전원 작업 확인 헤더가 필요합니다")
    try:
        if IS_WINDOWS:
            shutdown = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "shutdown.exe")
            switch = "/r" if action == "restart" else "/s"
            subprocess.Popen(
                [shutdown, switch, "/t", "3", "/d", "p:0:0", "/c", f"main_server: system {action}"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW,
            )
        else:
            _linux_request("reboot" if action == "restart" else "shutdown")
        return {"ok": True, "action": action, "scheduled": True}
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        raise HTTPException(500, str(error)) from error
