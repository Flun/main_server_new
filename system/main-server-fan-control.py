#!/usr/bin/env python3
"""Small root helper for fail-safe Linux motherboard fan control.

Only PWM attributes belonging to an nct* hwmon device are writable.  A manual
setting is a renewable 15 second lease: the first write records the firmware
controlled state under /run, and a detached watchdog restores that state when
the manager stops renewing the lease.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


HWMON_CLASS = Path("/sys/class/hwmon")
RUNTIME_DIR = Path("/run/main-server-fan-control")
LEASE_SECONDS = 15
CHIP_RE = re.compile(r"^nct[0-9a-z_-]+$", re.IGNORECASE)
PWM_RE = re.compile(r"^pwm([1-9][0-9]*)$")
CHANNEL_RE = re.compile(r"^(nct[0-9a-z_-]+):(pwm[1-9][0-9]*)$", re.IGNORECASE)


class FanControlError(RuntimeError):
    pass


def _read_text(path: Path) -> str:
    return path.read_text(encoding="ascii").strip()


def _read_int(path: Path) -> int:
    value = _read_text(path)
    if not re.fullmatch(r"-?[0-9]+", value):
        raise FanControlError(f"숫자가 아닌 hwmon 값입니다: {path.name}")
    return int(value)


def _safe_hwmon_device(entry: Path) -> tuple[str, Path] | None:
    try:
        device = entry.resolve(strict=True)
        sys_devices = Path("/sys/devices").resolve(strict=True)
        device.relative_to(sys_devices)
        chip = _read_text(device / "name")
    except (OSError, ValueError):
        return None
    if not CHIP_RE.fullmatch(chip):
        return None
    return chip.lower(), device


def _discover_channels() -> dict[str, dict[str, Any]]:
    channels: dict[str, dict[str, Any]] = {}
    if not HWMON_CLASS.is_dir():
        return channels
    for entry in sorted(HWMON_CLASS.glob("hwmon*")):
        found = _safe_hwmon_device(entry)
        if found is None:
            continue
        chip, device = found
        for pwm_path in sorted(device.glob("pwm*")):
            match = PWM_RE.fullmatch(pwm_path.name)
            if not match:
                continue
            index = int(match.group(1))
            enable_path = device / f"pwm{index}_enable"
            if not enable_path.is_file():
                continue
            channel_id = f"{chip}:pwm{index}"
            if channel_id in channels:
                # A chip name is normally unique. Refuse an ambiguous stable ID
                # instead of ever writing to an arbitrary matching controller.
                channels[channel_id]["ambiguous"] = True
                continue
            channels[channel_id] = {
                "id": channel_id,
                "chip": chip,
                "pwm_index": index,
                "device": device,
                "pwm_path": pwm_path,
                "enable_path": enable_path,
                "mode_path": device / f"pwm{index}_mode",
                "rpm_path": device / f"fan{index}_input",
                "ambiguous": False,
            }
    return channels


def _resolve_channel(channel_id: str) -> dict[str, Any]:
    match = CHANNEL_RE.fullmatch(channel_id)
    if not match:
        raise FanControlError("채널 ID 형식은 nct칩:pwmN 이어야 합니다")
    normalized = f"{match.group(1).lower()}:{match.group(2).lower()}"
    channel = _discover_channels().get(normalized)
    if channel is None:
        raise FanControlError(f"팬 채널을 찾을 수 없습니다: {normalized}")
    if channel["ambiguous"]:
        raise FanControlError(f"중복된 hwmon 칩 이름으로 채널이 모호합니다: {normalized}")
    return channel


def _state_name(channel_id: str) -> str:
    if not CHANNEL_RE.fullmatch(channel_id):
        raise FanControlError("안전하지 않은 채널 ID입니다")
    return channel_id.replace(":", "_") + ".json"


def _prepare_runtime() -> None:
    if os.geteuid() != 0:
        raise FanControlError("팬 설정 변경은 root 권한이 필요합니다")
    RUNTIME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(RUNTIME_DIR, 0o700)


@contextmanager
def _locked() -> Iterator[None]:
    _prepare_runtime()
    lock_fd = os.open(
        RUNTIME_DIR / "lock",
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _state_path(channel_id: str) -> Path:
    return RUNTIME_DIR / _state_name(channel_id)


def _load_state(channel_id: str) -> dict[str, Any] | None:
    path = _state_path(channel_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise FanControlError(f"팬 복원 상태를 읽지 못했습니다: {error}") from error
    required = {"channel_id", "token", "expires_at", "original"}
    if not isinstance(data, dict) or not required.issubset(data) or data["channel_id"] != channel_id:
        raise FanControlError("팬 복원 상태 파일이 올바르지 않습니다")
    original = data.get("original")
    if not isinstance(original, dict) or not all(key in original for key in ("pwm", "enable")):
        raise FanControlError("팬 원상태 정보가 올바르지 않습니다")
    return data


def _save_state(channel_id: str, data: dict[str, Any]) -> None:
    path = _state_path(channel_id)
    temporary = path.with_suffix(".tmp")
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        payload = (json.dumps(data, separators=(",", ":")) + "\n").encode("utf-8")
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)


def _write_sysfs(path: Path, value: int) -> None:
    # The containing hwmon device was already resolved under /sys/devices.
    # O_NOFOLLOW prevents replacing the attribute with a symlink between lookup
    # and write; sysfs numeric attributes present as regular files.
    flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise FanControlError(f"{path.name} 열기 실패: {error}") from error
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise FanControlError(f"일반 hwmon 속성이 아닙니다: {path.name}")
        os.write(fd, f"{int(value)}\n".encode("ascii"))
    except OSError as error:
        raise FanControlError(f"{path.name} 쓰기 실패: {error}") from error
    finally:
        os.close(fd)


def _capture_original(channel: dict[str, Any]) -> dict[str, int | None]:
    mode_path = channel["mode_path"]
    return {
        "pwm": _read_int(channel["pwm_path"]),
        "enable": _read_int(channel["enable_path"]),
        "mode": _read_int(mode_path) if mode_path.is_file() else None,
    }


def _restore_locked(channel_id: str, state: dict[str, Any]) -> None:
    channel = _resolve_channel(channel_id)
    original = state["original"]
    # nct6775 rejects PWM writes while a BIOS/Smart Fan mode owns the channel.
    # Switch to manual at the currently reported duty, restore the saved duty
    # and electrical mode, then hand ownership back to the original mode last.
    if _read_int(channel["enable_path"]) != 1:
        _write_sysfs(channel["enable_path"], 1)
    _write_sysfs(channel["pwm_path"], int(original["pwm"]))
    if original.get("mode") is not None and channel["mode_path"].is_file():
        _write_sysfs(channel["mode_path"], int(original["mode"]))
    _write_sysfs(channel["enable_path"], int(original["enable"]))
    try:
        _state_path(channel_id).unlink()
    except FileNotFoundError:
        pass


def _spawn_watchdog(channel_id: str, token: str) -> None:
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_watch", channel_id, token],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


def command_set(channel_id: str, percent_text: str) -> dict[str, Any]:
    try:
        percent = int(percent_text)
    except ValueError as error:
        raise FanControlError("팬 속도는 20~100 사이 정수여야 합니다") from error
    if str(percent) != percent_text or not 20 <= percent <= 100:
        raise FanControlError("팬 속도는 20~100 사이 정수여야 합니다")
    channel = _resolve_channel(channel_id)
    normalized = channel["id"]
    raw_pwm = round(percent * 255 / 100)
    with _locked():
        state = _load_state(normalized)
        original = state["original"] if state else _capture_original(channel)
        token = secrets.token_hex(16)
        state = {
            "channel_id": normalized,
            "token": token,
            "expires_at": time.time() + LEASE_SECONDS,
            "original": original,
            "percent": percent,
        }
        _save_state(normalized, state)
        try:
            # The nct6775 driver returns EBUSY for PWM writes while Smart Fan
            # owns the channel. pwmN already reports its current duty, so the
            # manual transition retains that duty until the requested value is
            # written immediately afterwards.
            _write_sysfs(channel["enable_path"], 1)
            _write_sysfs(channel["pwm_path"], raw_pwm)
            applied_raw = _read_int(channel["pwm_path"])
            applied_enable = _read_int(channel["enable_path"])
            if applied_enable != 1:
                raise FanControlError("수동 PWM 모드 전환이 확인되지 않았습니다")
        except Exception:
            try:
                _restore_locked(normalized, state)
            finally:
                raise
    _spawn_watchdog(normalized, token)
    return {
        "ok": True,
        "id": normalized,
        "percent": round(applied_raw * 100 / 255),
        "lease_seconds": LEASE_SECONDS,
        "expires_at": state["expires_at"],
    }


def command_reset(channel_id: str | None = None) -> dict[str, Any]:
    restored: list[str] = []
    with _locked():
        if channel_id is not None:
            channel = _resolve_channel(channel_id)
            ids = [channel["id"]]
        else:
            ids = [
                path.stem.replace("_pwm", ":pwm", 1)
                for path in sorted(RUNTIME_DIR.glob("nct*_pwm*.json"))
            ]
        for current_id in ids:
            state = _load_state(current_id)
            if state is None:
                continue
            _restore_locked(current_id, state)
            restored.append(current_id)
    return {"ok": True, "restored": restored}


def _cpu_temperature() -> tuple[float | None, str | None]:
    for entry in sorted(HWMON_CLASS.glob("hwmon*")):
        try:
            device = entry.resolve(strict=True)
            if _read_text(device / "name").lower() != "k10temp":
                continue
            temperature = _read_int(device / "temp1_input") / 1000.0
            label_path = device / "temp1_label"
            label = _read_text(label_path) if label_path.is_file() else "CPU Tctl"
            return round(temperature, 1), label
        except (OSError, FanControlError, ValueError):
            continue
    return None, None


def command_status() -> dict[str, Any]:
    leases: dict[str, dict[str, Any]] = {}
    if RUNTIME_DIR.is_dir():
        for path in sorted(RUNTIME_DIR.glob("nct*_pwm*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and CHANNEL_RE.fullmatch(str(data.get("channel_id", ""))):
                    leases[data["channel_id"]] = data
            except (OSError, ValueError):
                continue
    channels = []
    now = time.time()
    for channel_id, channel in sorted(_discover_channels().items()):
        try:
            raw = _read_int(channel["pwm_path"])
            rpm = _read_int(channel["rpm_path"]) if channel["rpm_path"].is_file() else None
            enable = _read_int(channel["enable_path"])
            lease = leases.get(channel_id)
            channels.append({
                "id": channel_id,
                "name": f"{channel['chip']} PWM {channel['pwm_index']}",
                "chip": channel["chip"],
                "pwm_index": channel["pwm_index"],
                "rpm": rpm,
                "percent": round(raw * 100 / 255),
                "pwm_enable": enable,
                "manual": enable == 1,
                "lease_remaining": max(0, round(float(lease["expires_at"]) - now, 1)) if lease else 0,
                "ambiguous": bool(channel["ambiguous"]),
            })
        except (OSError, FanControlError, ValueError):
            continue
    cpu_temperature, cpu_name = _cpu_temperature()
    return {
        "ok": True,
        "available": any(not item["ambiguous"] for item in channels),
        "lease_seconds": LEASE_SECONDS,
        "cpu_temperature": cpu_temperature,
        "cpu_temperature_name": cpu_name,
        "channels": channels,
    }


def command_watch(channel_id: str, token: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        return
    while True:
        restore_failed = False
        with _locked():
            state = _load_state(channel_id)
            if state is None or state.get("token") != token:
                return
            remaining = float(state["expires_at"]) - time.time()
            if remaining <= 0:
                try:
                    _restore_locked(channel_id, state)
                except Exception:
                    # Keep the original state and retry. A transient sysfs/driver
                    # race must not turn lease expiry into permanent manual PWM.
                    restore_failed = True
                else:
                    return
        if restore_failed:
            time.sleep(1.0)
            continue
        time.sleep(min(1.0, max(0.1, remaining)))


def main(argv: list[str]) -> int:
    try:
        command = argv[1] if len(argv) > 1 else "status"
        if command == "status" and len(argv) == 2:
            result = command_status()
        elif command == "set" and len(argv) == 4:
            result = command_set(argv[2], argv[3])
        elif command == "reset" and len(argv) in (2, 3):
            result = command_reset(argv[2] if len(argv) == 3 else None)
        elif command == "_watch" and len(argv) == 4:
            command_watch(argv[2], argv[3])
            return 0
        else:
            raise FanControlError("사용법: main-server-fan-control.py status|set ID PERCENT|reset [ID]")
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (FanControlError, OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
