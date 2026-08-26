"""Cross-platform NAS credential storage and SMB/CIFS mount automation."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import posixpath
import re
import subprocess
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from config import BASE_DIR


SETTINGS_FILE = Path(BASE_DIR) / ("nas_windows_settings.json" if os.name == "nt" else "nas_linux_settings.json")
CREDENTIALS_FILE = Path(BASE_DIR) / ("nas_credentials_windows.json" if os.name == "nt" else "nas_credentials_linux")
LOCK = threading.RLock()
LAST_ERROR = ""
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "server": "192.168.1.119",
    # The supplied ComfyUI path establishes `homes` as the SMB share. SMB
    # cannot mount a bare server address, so both platforms mount that share.
    "share": "homes",
    "comfyui_subdir": "Flux/Photos/ComfyUI",
    "windows_drive": "N:",
    "linux_mount": "/mnt/nas",
    "linux_comfy_mount": "/opt/ComfyUI/output",
    "smb_version": "3.0",
}


class MountError(RuntimeError):
    pass


def _read_json(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError, TypeError):
        return default


def _write_json(path: Path, value: Any, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    if private and os.name != "nt":
        os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    if private and os.name != "nt":
        os.chmod(path, 0o600)


def load_settings() -> dict[str, Any]:
    result = dict(DEFAULTS)
    saved = _read_json(SETTINGS_FILE, {})
    if isinstance(saved, dict):
        result.update({key: value for key, value in saved.items() if key in result})
    return result


def save_settings(values: dict[str, Any]) -> dict[str, Any]:
    result = load_settings()
    result.update({key: value for key, value in values.items() if key in result})
    result["server"] = _normalize_server(result["server"])
    result["share"] = _single_component(result["share"], "NAS 공유 이름")
    result["comfyui_subdir"] = _relative_path(result["comfyui_subdir"], "ComfyUI NAS 경로")
    result["enabled"] = bool(result["enabled"])
    drive = str(result["windows_drive"] or "").strip().upper().rstrip("\\/")
    if not re.fullmatch(r"[A-Z]:", drive):
        raise MountError("Windows 드라이브는 N: 형식이어야 합니다")
    result["windows_drive"] = drive
    for key, label in (("linux_mount", "Linux NAS 마운트"), ("linux_comfy_mount", "Linux ComfyUI 마운트")):
        path = posixpath.normpath(str(result[key] or "").strip().replace("\\", "/"))
        if not path.startswith("/"):
            raise MountError(f"{label} 경로는 /로 시작해야 합니다")
        result[key] = path
    if not re.fullmatch(r"\d+(?:\.\d+)?", str(result["smb_version"])):
        raise MountError("SMB 버전 형식이 올바르지 않습니다")
    _write_json(SETTINGS_FILE, result)
    return result


def _normalize_server(value: Any) -> str:
    server = str(value or "").strip().replace("\\", "/").strip("/")
    if "/" in server or not server or re.search(r"[\s,]", server):
        raise MountError("NAS 서버에는 IP 주소 또는 호스트 이름만 입력하세요")
    return server


def _single_component(value: Any, label: str) -> str:
    component = str(value or "").strip().strip("\\/")
    if not component or re.search(r"[\\/,\r\n]", component):
        raise MountError(f"{label}이 올바르지 않습니다")
    return component


def _relative_path(value: Any, label: str) -> str:
    path = str(value or "").strip().replace("\\", "/").strip("/")
    parts = path.split("/") if path else []
    if not parts or any(part in {"", ".", ".."} or re.search(r"[,\r\n]", part) for part in parts):
        raise MountError(f"{label}가 올바르지 않습니다")
    return "/".join(parts)


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi_encrypt(value: str) -> str:
    raw = value.encode("utf-8")
    buffer = ctypes.create_string_buffer(raw)
    incoming = _DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    outgoing = _DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(incoming), "MainServer NAS", None, None, None, 0, ctypes.byref(outgoing)):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(outgoing.pbData, outgoing.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(outgoing.pbData)


def _dpapi_decrypt(value: str) -> str:
    raw = base64.b64decode(value)
    buffer = ctypes.create_string_buffer(raw)
    incoming = _DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    outgoing = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(incoming), None, None, None, None, 0, ctypes.byref(outgoing)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(outgoing.pbData)


def save_credentials(username: str, password: str) -> None:
    username = str(username or "").strip()
    password = str(password or "")
    if not username or not password:
        raise MountError("NAS 계정과 암호를 모두 입력하세요")
    if re.search(r"[\r\n]", username + password):
        raise MountError("NAS 계정과 암호에는 줄바꿈을 사용할 수 없습니다")
    payload = {"username": username, "password": _dpapi_encrypt(password) if os.name == "nt" else password}
    _write_json(CREDENTIALS_FILE, payload, private=True)


def _load_credentials() -> tuple[str, str]:
    payload = _read_json(CREDENTIALS_FILE, {})
    username, password = str(payload.get("username") or ""), str(payload.get("password") or "")
    if os.name == "nt" and password:
        password = _dpapi_decrypt(password)
    if not username or not password:
        raise MountError("저장된 NAS 계정/암호가 없습니다")
    return username, password


def delete_credentials() -> None:
    try:
        CREDENTIALS_FILE.unlink()
    except FileNotFoundError:
        pass


def _run(command: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=timeout, creationflags=NO_WINDOW)
    except FileNotFoundError as error:
        raise MountError(f"필수 명령을 찾을 수 없습니다: {command[0]}") from error


def _linux_command(command: list[str]) -> list[str]:
    if os.geteuid() == 0:
        return command
    return ["sudo", "-n", *command]


def _is_linux_mounted(path: str) -> bool:
    target = os.path.realpath(path)
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as stream:
            for line in stream:
                fields = line.split()
                if len(fields) > 4 and fields[4].replace("\\040", " ") == target:
                    return True
    except OSError:
        pass
    return False


def _linux_mount_source(path: str) -> tuple[str, str] | None:
    """Return (filesystem, source) for an exact Linux mount point."""
    target = os.path.realpath(path)
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as stream:
            for line in stream:
                fields = line.split()
                if len(fields) <= 6 or fields[4].replace("\\040", " ") != target or "-" not in fields:
                    continue
                separator = fields.index("-")
                if len(fields) > separator + 2:
                    return fields[separator + 1], fields[separator + 2].replace("\\040", " ")
    except OSError:
        pass
    return None


def _friendly_smb_error(output: str, username: str, remote: str) -> str:
    text = str(output or "").strip()
    upper = text.upper()
    if "NT_STATUS_LOGON_FAILURE" in upper or "STATUS_LOGON_FAILURE" in upper:
        return f"NAS 로그인 거부: 저장된 계정 '{username}'의 계정명 또는 암호를 확인하세요 ({remote})"
    if "NT_STATUS_ACCOUNT_DISABLED" in upper:
        return f"NAS 계정 '{username}'이 비활성화되어 있습니다"
    if "NT_STATUS_PASSWORD_EXPIRED" in upper:
        return f"NAS 계정 '{username}'의 암호가 만료되었습니다"
    if "NT_STATUS_BAD_NETWORK_NAME" in upper:
        return f"NAS 공유 폴더를 찾을 수 없습니다: {remote}"
    if "NT_STATUS_ACCESS_DENIED" in upper or "PERMISSION DENIED" in upper:
        return f"NAS 계정 '{username}'에 공유 폴더 접근 권한이 없습니다: {remote}"
    if "CONNECTION REFUSED" in upper or "NO ROUTE TO HOST" in upper:
        return f"NAS SMB 서버에 연결할 수 없습니다: {remote}"
    return text or f"NAS 연결에 실패했습니다: {remote}"


def status() -> dict[str, Any]:
    configured = load_settings()
    saved_credentials = _read_json(CREDENTIALS_FILE, {})
    remote = f"//{configured['server']}/{configured['share']}"
    if os.name == "nt":
        main_mounted = os.path.exists(configured["windows_drive"] + "\\")
        comfy_mounted = main_mounted
        main_local = configured["windows_drive"] + "\\"
        comfy_local = configured["windows_drive"] + "\\" + configured["comfyui_subdir"].replace("/", "\\")
        display_remote = remote.replace("/", "\\")
    else:
        main_mounted = _is_linux_mounted(configured["linux_mount"])
        comfy_mounted = _is_linux_mounted(configured["linux_comfy_mount"])
        main_local, comfy_local, display_remote = configured["linux_mount"], configured["linux_comfy_mount"], remote
    return {
        "platform": "windows" if os.name == "nt" else "linux",
        "settings": configured,
        "credentials_saved": CREDENTIALS_FILE.is_file(),
        "credentials_username": str(saved_credentials.get("username") or ""),
        "mounted": main_mounted and comfy_mounted,
        "main_mounted": main_mounted,
        "comfyui_mounted": comfy_mounted,
        "remote": display_remote,
        "comfyui_remote": display_remote.rstrip("\\/") + ("\\" if os.name == "nt" else "/") + configured["comfyui_subdir"].replace("/", "\\" if os.name == "nt" else "/"),
        "main_local": main_local,
        "comfyui_local": comfy_local,
        "last_error": LAST_ERROR,
    }


def mount() -> dict[str, Any]:
    global LAST_ERROR
    with LOCK:
        try:
            configured = load_settings()
            if not configured["enabled"]:
                raise MountError("NAS 자동 마운트가 꺼져 있습니다")
            username, password = _load_credentials()
            if os.name == "nt":
                remote = rf"\\{configured['server']}\{configured['share']}"
                drive = configured["windows_drive"]
                # Clear only our configured drive mapping. Do not disconnect other
                # sessions to the same NAS because they may belong to the user.
                _run(["net", "use", drive, "/delete", "/y"], timeout=15)
                result = _run(["net", "use", drive, remote, password, f"/user:{username}", "/persistent:yes"])
                if result.returncode != 0:
                    raise MountError((result.stderr or result.stdout or "Windows NAS 연결 실패").strip())
            else:
                _mount_linux(configured, username, password)
            LAST_ERROR = ""
            return status()
        except Exception as error:
            LAST_ERROR = str(error)
            raise


def _mount_linux(configured: dict[str, Any], username: str, password: str) -> None:
    remote = f"//{configured['server']}/{configured['share']}"
    # mount.cifs and smbclient share this private, short-lived auth file. This
    # keeps the password out of process arguments and removes it after use.
    credential_fd, credential_name = tempfile.mkstemp(prefix="main-server-nas-", suffix=".auth")
    mount_credentials = Path(credential_name)
    with os.fdopen(credential_fd, "w", encoding="utf-8") as stream:
        stream.write(f"username={username}\npassword={password}\n")
    os.chmod(mount_credentials, 0o600)
    uid, gid = os.getuid(), os.getgid()
    common = f"credentials={mount_credentials},rw,uid={uid},gid={gid},iocharset=utf8,vers={configured['smb_version']},file_mode=0666,dir_mode=0777,noserverino"
    main_target = configured["linux_mount"]
    comfy_target = configured["linux_comfy_mount"]
    mounted_main_here = False
    try:
        # Preflight returns the real SMB status (wrong password, missing share,
        # access denied) instead of mount(8)'s misleading generic dmesg text.
        preflight = _run([
            "smbclient", remote, "-A", str(mount_credentials), "-m", "SMB3", "-c", "quit",
        ], timeout=20)
        if preflight.returncode != 0:
            raise MountError(_friendly_smb_error(preflight.stderr or preflight.stdout, username, remote))

        for target in (main_target, comfy_target):
            if not Path(target).is_dir():
                result = _run(_linux_command(["mkdir", "-p", target]))
                if result.returncode != 0:
                    raise MountError((result.stderr or result.stdout or f"마운트 폴더 생성 실패: {target}").strip())

        existing = _linux_mount_source(main_target)
        if existing:
            filesystem, source = existing
            if filesystem != "cifs" or source.rstrip("/").lower() != remote.lower():
                raise MountError(f"다른 파일시스템이 이미 마운트되어 있습니다: {main_target} ({filesystem} {source})")
        else:
            result = _run(_linux_command(["mount", "-t", "cifs", remote, main_target, "-o", common]))
            if result.returncode != 0:
                raise MountError(_friendly_smb_error(result.stderr or result.stdout, username, remote))
            mounted_main_here = True

        comfy_source = Path(main_target).joinpath(*configured["comfyui_subdir"].split("/"))
        if not comfy_source.is_dir():
            raise MountError(f"NAS 안에서 ComfyUI 폴더를 찾을 수 없습니다: {comfy_source}")
        existing = _linux_mount_source(comfy_target)
        if existing:
            filesystem, source = existing
            normalized_source = source.rstrip("/").lower()
            normalized_remote = remote.lower()
            # A CIFS bind mount is reported as //server/share[/sub/path] in
            # /proc/self/mountinfo, not as the local bind source path.
            if filesystem != "cifs" or not (
                normalized_source == normalized_remote
                or normalized_source.startswith(normalized_remote + "[")
            ):
                raise MountError(f"다른 파일시스템이 이미 마운트되어 있습니다: {comfy_target} ({filesystem} {source})")
        else:
            result = _run(_linux_command(["mount", "--bind", str(comfy_source), comfy_target]))
            if result.returncode != 0:
                raise MountError((result.stderr or result.stdout or f"ComfyUI NAS 연결 실패: {comfy_target}").strip())
    except Exception:
        if mounted_main_here and _is_linux_mounted(main_target):
            _run(_linux_command(["umount", main_target]))
        raise
    finally:
        try:
            mount_credentials.unlink()
        except OSError:
            pass


def unmount() -> dict[str, Any]:
    with LOCK:
        configured = load_settings()
        if os.name == "nt":
            result = _run(["net", "use", configured["windows_drive"], "/delete", "/y"])
            if result.returncode not in (0, 2, 2250):
                raise MountError((result.stderr or result.stdout or "Windows NAS 연결 해제 실패").strip())
        else:
            for target in (configured["linux_comfy_mount"], configured["linux_mount"]):
                if _is_linux_mounted(target):
                    result = _run(_linux_command(["umount", target]))
                    if result.returncode != 0:
                        raise MountError((result.stderr or result.stdout or f"마운트 해제 실패: {target}").strip())
        return status()


def auto_mount() -> bool:
    configured = load_settings()
    if not configured["enabled"] or not CREDENTIALS_FILE.is_file():
        return False
    mount()
    print("[nas] 자동 마운트 완료")
    return True


def auto_mount_async(attempts: int = 6, retry_seconds: int = 10) -> None:

    def worker() -> None:
        for attempt in range(1, max(1, attempts) + 1):
            try:
                auto_mount()
                return
            except Exception as error:
                print(f"[nas] 자동 마운트 실패 ({attempt}/{attempts}): {error}")
                if attempt < attempts:
                    time.sleep(max(1, retry_seconds))

    threading.Thread(target=worker, name="nas-auto-mount", daemon=True).start()
