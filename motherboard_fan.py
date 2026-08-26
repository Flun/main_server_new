"""Windows motherboard fan backend and GPU-temperature curve controller.

The web process never touches Super I/O registers. An elevated .NET helper owns
the LibreHardwareMonitor controls and accepts authenticated localhost commands.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import threading
import time
import socket
from pathlib import Path
from typing import Any

import psutil

import pawnio_bootstrap


IS_WINDOWS = os.name == "nt"
BASE_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = BASE_DIR / "motherboard_fan_settings.json"
HELPER_EXE = BASE_DIR / "fan_helper" / "dist" / "MainServer.FanHelper.exe"
HELPER_HOST = "127.0.0.1"
HELPER_PORT = 8997
HELPER_TOKEN_FILE = HELPER_EXE.parent / "fan_helper_secret.txt"
LINUX_HELPER = Path("/usr/local/sbin/main-server-fan-control")

DEFAULT_CURVE = [
    {"temp": 40, "percent": 40},
    {"temp": 55, "percent": 48},
    {"temp": 65, "percent": 60},
    {"temp": 75, "percent": 75},
    {"temp": 82, "percent": 90},
    {"temp": 85, "percent": 100},
]

DEFAULT_CPU_CURVE = [
    {"temp": 30, "percent": 20},
    {"temp": 45, "percent": 25},
    {"temp": 55, "percent": 35},
    {"temp": 65, "percent": 50},
    {"temp": 75, "percent": 75},
    {"temp": 85, "percent": 100},
]

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "channel_id": "",
    "channel_label": "",
    "fan_role": "cmp170hx_hbm",
    "gpu_uuid": "",
    "curve_mode": "linear",
    "min_percent": 40,
    "max_percent": 100,
    "hysteresis_c": 2,
    "down_delay_seconds": 30,
    "curve": DEFAULT_CURVE,
    "cpu": {
        "enabled": False,
        "channel_id": "",
        "channel_label": "CPU 팬",
        "fan_role": "cpu_package",
        "curve_mode": "step",
        "min_percent": 20,
        "max_percent": 100,
        "hysteresis_c": 3,
        "down_delay_seconds": 20,
        "curve": DEFAULT_CPU_CURVE,
    },
}


def _fan_control_running() -> bool:
    for process in psutil.process_iter(["name"]):
        try:
            if (process.info.get("name") or "").lower() == "fancontrol.exe":
                return True
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return False


def load_settings() -> dict[str, Any]:
    values = dict(DEFAULTS)
    values["curve"] = [dict(point) for point in DEFAULT_CURVE]
    values["cpu"] = dict(DEFAULTS["cpu"])
    values["cpu"]["curve"] = [dict(point) for point in DEFAULT_CPU_CURVE]
    try:
        loaded = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            cpu_loaded = loaded.get("cpu")
            values.update(loaded)
            if isinstance(cpu_loaded, dict):
                cpu_values = dict(DEFAULTS["cpu"])
                cpu_values["curve"] = [dict(point) for point in DEFAULT_CPU_CURVE]
                cpu_values.update(cpu_loaded)
                values["cpu"] = cpu_values
    except (OSError, ValueError):
        pass
    return values


def _validate_profile(result: dict[str, Any], *, cpu: bool = False) -> dict[str, Any]:
    result["enabled"] = bool(result.get("enabled"))
    result["channel_id"] = str(result.get("channel_id") or "")
    result["channel_label"] = str(result.get("channel_label") or "")[:80]
    result["gpu_uuid"] = str(result.get("gpu_uuid") or "")
    role = str(result.get("fan_role") or ("cpu_package" if cpu else ""))
    allowed_roles = {"cpu_package"} if cpu else {"cmp170hx_hbm", "gpu_hbm"}
    if role not in allowed_roles:
        raise ValueError("지원하지 않는 팬 역할입니다")
    result["fan_role"] = role
    curve_mode = str(result.get("curve_mode") or "linear")
    if curve_mode not in {"linear", "step"}:
        raise ValueError("커브 방식은 선형 또는 계단식이어야 합니다")
    result["curve_mode"] = curve_mode
    result["min_percent"] = int(result.get("min_percent", 40))
    result["max_percent"] = int(result.get("max_percent", 100))
    result["hysteresis_c"] = int(result.get("hysteresis_c", 2))
    result["down_delay_seconds"] = int(result.get("down_delay_seconds", 30))
    if not 20 <= result["min_percent"] <= 100:
        raise ValueError("최소 PWM은 20~100%여야 합니다")
    if not result["min_percent"] <= result["max_percent"] <= 100:
        raise ValueError("최대 PWM은 최소 PWM 이상, 100% 이하여야 합니다")
    if not 0 <= result["hysteresis_c"] <= 10:
        raise ValueError("히스테리시스는 0~10°C여야 합니다")
    if not 0 <= result["down_delay_seconds"] <= 600:
        raise ValueError("감속 지연은 0~600초여야 합니다")
    curve = result.get("curve")
    if not isinstance(curve, list) or not 2 <= len(curve) <= 12:
        raise ValueError("팬 커브는 2~12개 지점이어야 합니다")
    normalized = []
    for point in curve:
        if not isinstance(point, dict):
            raise ValueError("팬 커브 형식이 올바르지 않습니다")
        temp = int(point["temp"])
        percent = int(point["percent"])
        if not 20 <= temp <= 110 or not 0 <= percent <= 100:
            raise ValueError("팬 커브 온도는 20~110°C, PWM은 0~100%여야 합니다")
        normalized.append({"temp": temp, "percent": percent})
    normalized.sort(key=lambda point: point["temp"])
    if len({point["temp"] for point in normalized}) != len(normalized):
        raise ValueError("팬 커브 온도 지점은 중복될 수 없습니다")
    if any(normalized[i]["percent"] > normalized[i + 1]["percent"] for i in range(len(normalized) - 1)):
        raise ValueError("온도가 높아질수록 PWM이 낮아질 수 없습니다")
    result["curve"] = normalized
    if result["enabled"] and not result["channel_id"]:
        raise ValueError("활성화하려면 팬 채널을 선택해야 합니다")
    if result["enabled"] and not cpu and not result["gpu_uuid"]:
        raise ValueError("GPU 팬을 활성화하려면 연결 GPU를 선택해야 합니다")
    return result


def validate_settings(values: dict[str, Any]) -> dict[str, Any]:
    result = load_settings()
    result.update(values)
    result = _validate_profile(result)
    cpu_values = dict(load_settings()["cpu"])
    supplied_cpu = values.get("cpu")
    if isinstance(supplied_cpu, dict):
        cpu_values.update(supplied_cpu)
    cpu_values["fan_role"] = "cpu_package"
    cpu_values["gpu_uuid"] = ""
    result["cpu"] = _validate_profile(cpu_values, cpu=True)
    if result["enabled"] and result["cpu"]["enabled"] and result["channel_id"] == result["cpu"]["channel_id"]:
        raise ValueError("GPU 팬과 CPU 팬은 서로 다른 메인보드 채널을 선택해야 합니다")
    return result


def save_settings(values: dict[str, Any]) -> dict[str, Any]:
    normalized = validate_settings(values)
    temporary = SETTINGS_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, SETTINGS_FILE)
    return normalized


class HelperClient:
    def __init__(self) -> None:
        self._socket: socket.socket | None = None
        self._pipe = None
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self._lock = threading.RLock()

    def _reader(self, pipe) -> None:
        while True:
            try:
                raw = pipe.readline()
            except (OSError, ValueError):
                return
            if not raw:
                return
            try:
                line = raw.decode("utf-8", errors="replace")
                value = json.loads(line)
                self._responses.put(value if isinstance(value, dict) else {"ok": False, "error": line.strip()})
            except ValueError:
                self._responses.put({"ok": False, "error": f"헬퍼 응답 해석 실패: {line.strip()}"})

    def _start(self) -> None:
        if self._pipe is not None:
            return
        if not HELPER_EXE.is_file():
            raise RuntimeError(f"Windows 팬 헬퍼가 빌드되지 않았습니다: {HELPER_EXE}")
        pawnio = pawnio_bootstrap.get_status()
        if not pawnio.get("installed") or not pawnio.get("task_registered") or not HELPER_TOKEN_FILE.is_file():
            pawnio_bootstrap.ensure_async()
            raise RuntimeError(pawnio.get("error") or "PawnIO 자동 설치를 준비 중입니다")
        if _fan_control_running():
            raise RuntimeError("Fan Control이 실행 중입니다. Super I/O 충돌 방지를 위해 먼저 종료하세요.")
        self._responses = queue.Queue()
        pawnio_bootstrap.start_task()
        last_error = None
        for _ in range(30):
            try:
                self._socket = socket.create_connection((HELPER_HOST, HELPER_PORT), timeout=1.0)
                self._socket.settimeout(None)
                self._pipe = self._socket.makefile("rwb", buffering=0)
                break
            except (OSError, ValueError) as error:
                last_error = error
                time.sleep(0.2)
        if self._pipe is None:
            raise RuntimeError(f"관리자 팬 헬퍼에 연결하지 못했습니다: {last_error}")
        threading.Thread(target=self._reader, args=(self._pipe,), daemon=True).start()

    def request(self, command: dict[str, Any], timeout: float = 8.0) -> dict[str, Any]:
        with self._lock:
            self._start()
            assert self._pipe is not None
            try:
                payload = dict(command)
                payload["token"] = HELPER_TOKEN_FILE.read_text(encoding="utf-8").strip()
                self._pipe.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            except OSError as error:
                self.stop()
                raise RuntimeError(f"Windows 팬 헬퍼 연결이 끊어졌습니다: {error}") from error
            try:
                response = self._responses.get(timeout=timeout)
            except queue.Empty as error:
                self.stop()
                raise RuntimeError("Windows 팬 헬퍼 응답 시간이 초과되었습니다") from error
            if not response.get("ok"):
                raise RuntimeError(str(response.get("error") or "팬 헬퍼 작업 실패"))
            return response

    def stop(self) -> None:
        with self._lock:
            pipe, self._pipe = self._pipe, None
            connection, self._socket = self._socket, None
            if pipe is None:
                return
            try:
                pipe.close()
            except OSError:
                pass
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass


class LinuxHwmonHelper:
    """Restricted sudo bridge for Linux hwmon PWM channels.

    The privileged helper owns validation, original-state restoration and the
    lease watchdog.  The web process only passes a stable chip/channel id and a
    bounded percentage; it never writes arbitrary sysfs paths.
    """

    _pipe = None

    def request(self, command: dict[str, Any], timeout: float = 8.0) -> dict[str, Any]:
        if not LINUX_HELPER.is_file():
            raise RuntimeError(f"Linux 팬 helper가 설치되지 않았습니다: {LINUX_HELPER}")
        action = str(command.get("command") or "status")
        args = ["sudo", "-n", str(LINUX_HELPER)]
        if action == "status":
            args.append("status")
        elif action == "set":
            channel_id = str(command.get("id") or "")
            percent = int(command.get("percent", 0))
            args.extend(["set", channel_id, str(percent)])
        elif action == "reset":
            args.append("reset")
            channel_id = str(command.get("id") or "")
            if channel_id:
                args.append(channel_id)
        else:
            raise RuntimeError(f"Linux 팬 helper가 지원하지 않는 명령입니다: {action}")
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        raw = (result.stdout or "").strip()
        try:
            response = json.loads(raw) if raw else {}
        except ValueError as error:
            raise RuntimeError((result.stderr or raw or "Linux 팬 helper 응답 해석 실패").strip()) from error
        if result.returncode or not response.get("ok"):
            raise RuntimeError(str(response.get("error") or result.stderr.strip() or "Linux 팬 helper 작업 실패"))
        return response

    def stop(self) -> None:
        if not LINUX_HELPER.is_file():
            return
        try:
            self.request({"command": "reset"}, timeout=3.0)
        except Exception:
            pass


class FanController:
    def __init__(self) -> None:
        self.helper = HelperClient() if IS_WINDOWS else LinuxHwmonHelper()
        self._lock = threading.RLock()
        self._profile_state: dict[str, dict[str, Any]] = {
            "gpu": {"last_percent": None, "last_temp": None, "down_since": None, "channel_id": ""},
            "cpu": {"last_percent": None, "last_temp": None, "down_since": None, "channel_id": ""},
        }
        self._manual_mode = False
        self._manual_channel_id = ""
        self._manual_percent = 60
        self._helper_status_cache: dict[str, Any] = {"time": 0.0, "value": {}}
        self._runtime: dict[str, Any] = {
            "active": False, "control_mode": "auto", "temperature": None,
            "target_percent": None, "error": None,
        }
        atexit.register(self.close)

    @staticmethod
    def _curve_percent(temperature: float, curve: list[dict[str, int]], mode: str = "linear") -> float:
        if mode == "step":
            selected = curve[0]
            for point in curve[1:]:
                if temperature < point["temp"]:
                    break
                selected = point
            return float(selected["percent"])
        if temperature <= curve[0]["temp"]:
            return float(curve[0]["percent"])
        for left, right in zip(curve, curve[1:]):
            if temperature <= right["temp"]:
                span = right["temp"] - left["temp"]
                ratio = (temperature - left["temp"]) / span
                return left["percent"] + ratio * (right["percent"] - left["percent"])
        return float(curve[-1]["percent"])

    def tick(self, gpus: list[dict[str, Any]]) -> None:
        with self._lock:
            if self._manual_mode:
                try:
                    if _fan_control_running():
                        raise RuntimeError("Fan Control이 실행 중이어서 수동 팬 제어를 유지할 수 없습니다")
                    if self._manual_channel_id:
                        self.helper.request({
                            "command": "set", "id": self._manual_channel_id,
                            "percent": self._manual_percent,
                        })
                    self._runtime = {
                        "active": bool(self._manual_channel_id),
                        "control_mode": "manual",
                        "manual_channel_id": self._manual_channel_id,
                        "temperature": None,
                        "target_percent": self._manual_percent if self._manual_channel_id else None,
                        "error": None,
                        "updated_at": time.time(),
                    }
                except Exception as error:
                    self._runtime.update({
                        "active": False, "control_mode": "manual",
                        "error": str(error), "updated_at": time.time(),
                    })
                return
            config = load_settings()
            cpu_config = config.get("cpu") or {}
            if not config.get("enabled") and not cpu_config.get("enabled"):
                if self._runtime.get("active"):
                    try:
                        self.helper.request({"command": "reset"})
                    except Exception:
                        pass
                self._runtime = {
                    "active": False, "control_mode": "auto", "temperature": None,
                    "target_percent": None, "error": None,
                }
                self._reset_profile_state()
                return
            if _fan_control_running():
                error = "Fan Control이 실행 중이어서 메인보드 팬 제어를 시작하지 않았습니다"
                self._runtime = {
                    "active": False, "control_mode": "auto", "temperature": None,
                    "target_percent": None, "error": error, "profiles": {
                        "gpu": {"active": False, "error": error},
                        "cpu": {"active": False, "error": error},
                    }, "updated_at": time.time(),
                }
                return

            runtimes: dict[str, dict[str, Any]] = {}
            if config.get("enabled"):
                gpu = next((item for item in gpus if item.get("uuid") == config.get("gpu_uuid")), None)
                if not gpu:
                    runtimes["gpu"] = self._profile_error("gpu", config, "연결된 GPU를 찾을 수 없습니다")
                else:
                    temperature = gpu.get("temp_memory")
                    sensor_error = None
                    if temperature is None:
                        temperature = 110
                        sensor_error = "HBM 온도를 읽지 못해 안전상 최대 PWM을 적용했습니다"
                    runtimes["gpu"] = self._apply_profile(
                        "gpu", config, float(temperature), sensor_error,
                        {"gpu_uuid": gpu.get("uuid"), "gpu_name": gpu.get("name")},
                    )
            else:
                runtimes["gpu"] = self._disable_profile("gpu")

            if cpu_config.get("enabled"):
                try:
                    cpu_status = self._helper_status(max_age_seconds=2.0)
                    cpu_temperature = cpu_status.get("cpu_temperature")
                    sensor_error = None
                    if cpu_temperature is None:
                        cpu_temperature = 110
                        sensor_error = "CPU 온도를 읽지 못해 안전상 최대 PWM을 적용했습니다"
                    runtimes["cpu"] = self._apply_profile(
                        "cpu", cpu_config, float(cpu_temperature), sensor_error,
                        {"temperature_name": cpu_status.get("cpu_temperature_name")},
                    )
                except Exception as error:
                    runtimes["cpu"] = self._profile_error("cpu", cpu_config, str(error))
            else:
                runtimes["cpu"] = self._disable_profile("cpu")

            primary = runtimes.get("gpu") if config.get("enabled") else runtimes.get("cpu", {})
            self._runtime = {
                **(primary or {}),
                "active": any(item.get("active") for item in runtimes.values()),
                "control_mode": "auto", "profiles": runtimes,
                "error": next((item.get("error") for item in runtimes.values() if item.get("error")), None),
                "updated_at": time.time(),
            }

    def _reset_profile_state(self, key: str | None = None) -> None:
        keys = [key] if key else list(self._profile_state)
        for item_key in keys:
            self._profile_state[item_key] = {
                "last_percent": None, "last_temp": None, "down_since": None, "channel_id": "",
            }

    def _disable_profile(self, key: str) -> dict[str, Any]:
        state = self._profile_state[key]
        if state.get("channel_id"):
            try:
                self.helper.request({"command": "reset", "id": state["channel_id"]})
            except Exception:
                pass
        self._reset_profile_state(key)
        return {"active": False, "temperature": None, "target_percent": None, "error": None}

    def _profile_error(self, key: str, config: dict[str, Any], error: str) -> dict[str, Any]:
        self._profile_state[key]["channel_id"] = config.get("channel_id") or ""
        return {
            "active": False, "temperature": None, "target_percent": None,
            "channel_id": config.get("channel_id"), "error": error, "updated_at": time.time(),
        }

    def _apply_profile(
        self, key: str, config: dict[str, Any], temperature: float,
        sensor_error: str | None, extra: dict[str, Any],
    ) -> dict[str, Any]:
        state = self._profile_state[key]
        try:
            raw = self._curve_percent(temperature, config["curve"], config["curve_mode"])
            calculated = int(round(max(config["min_percent"], min(config["max_percent"], raw))))
            target = calculated
            hold_reason = None
            down_delay_remaining = 0
            now = time.monotonic()
            if state["last_percent"] is not None and target < state["last_percent"]:
                cooled = state["last_temp"] is None or temperature <= state["last_temp"] - config["hysteresis_c"]
                state["down_since"] = (state["down_since"] or now) if cooled else None
                elapsed = now - state["down_since"] if state["down_since"] else 0
                if not state["down_since"] or elapsed < config["down_delay_seconds"]:
                    hold_reason = "down_delay" if state["down_since"] else "hysteresis"
                    if state["down_since"]:
                        down_delay_remaining = max(0, int(round(config["down_delay_seconds"] - elapsed)))
                    target = state["last_percent"]
            else:
                state["down_since"] = None
            response = self.helper.request({"command": "set", "id": config["channel_id"], "percent": target})
            applied = int(round(float(response.get("percent", target))))
            if state["last_percent"] is None or applied != state["last_percent"]:
                state["last_temp"] = temperature
            state["last_percent"] = applied
            state["channel_id"] = config["channel_id"]
            return {
                "active": True, "temperature": None if sensor_error else temperature,
                "target_percent": applied, "calculated_percent": calculated,
                "curve_mode": config["curve_mode"], "channel_id": config["channel_id"],
                "hold_reason": hold_reason, "down_delay_remaining": down_delay_remaining,
                "error": sensor_error, "updated_at": time.time(), **extra,
            }
        except Exception as error:
            return self._profile_error(key, config, str(error))

    def status(self, include_channels: bool = True) -> dict[str, Any]:
        config = load_settings()
        with self._lock:
            runtime = dict(self._runtime)
        helper_built = HELPER_EXE.is_file() if IS_WINDOWS else LINUX_HELPER.is_file()
        result: dict[str, Any] = {
            "platform": "windows" if IS_WINDOWS else "linux",
            "available": helper_built,
            "helper_built": helper_built,
            "conflict": _fan_control_running(),
            "settings": config,
            "runtime": runtime,
            "channels": [],
            "pawnio_installer": pawnio_bootstrap.get_status(),
        }
        if not IS_WINDOWS:
            if not LINUX_HELPER.is_file():
                result["message"] = "Linux hwmon 팬 helper가 설치되지 않았습니다"
            elif include_channels:
                try:
                    helper = self._helper_status(max_age_seconds=1.0)
                    result["channels"] = helper.get("channels", [])
                    result["cpu_temperature"] = helper.get("cpu_temperature")
                    result["cpu_temperature_name"] = helper.get("cpu_temperature_name")
                    result["lease_seconds"] = helper.get("lease_seconds", 15)
                    result["available"] = bool(result["channels"])
                    if not result["channels"]:
                        result["message"] = "제어 가능한 Linux hwmon PWM 채널을 찾지 못했습니다"
                except Exception as error:
                    result["available"] = False
                    result["message"] = str(error)
        elif result["conflict"]:
            result["message"] = "Fan Control을 종료하면 메인보드 팬 채널을 탐색할 수 있습니다"
        elif not HELPER_EXE.is_file():
            result["message"] = "fan_helper/build.ps1로 Windows 팬 헬퍼를 빌드하세요"
        elif not result["pawnio_installer"].get("installed"):
            pawnio_bootstrap.ensure_async()
            install = pawnio_bootstrap.get_status()
            result["available"] = False
            result["pawnio_installer"] = install
            result["message"] = install.get("error") or install.get("message") or "PawnIO 자동 설치 준비 중"
        elif include_channels:
            try:
                helper = self._helper_status(max_age_seconds=1.0)
                result.update({key: helper.get(key) for key in (
                    "pawnio_installed", "pawnio_version", "lease_seconds",
                    "cpu_temperature", "cpu_temperature_name",
                )})
                result["channels"] = helper.get("channels", [])
                result["available"] = bool(helper.get("pawnio_installed") and result["channels"])
                if not helper.get("pawnio_installed"):
                    result["message"] = "PawnIO 커널 드라이버가 설치되지 않았습니다"
                elif not result["channels"]:
                    result["message"] = "제어 가능한 메인보드 팬 채널을 찾지 못했습니다"
            except Exception as error:
                result["available"] = False
                result["message"] = str(error)
        return result

    def reset(self) -> dict[str, Any]:
        with self._lock:
            response = self.helper.request({"command": "reset"})
            self._reset_profile_state()
            self._manual_channel_id = ""
            self._runtime = {
                "active": False,
                "control_mode": "manual" if self._manual_mode else "auto",
                "temperature": None, "target_percent": None, "error": None,
            }
            return response

    def enter_manual(self) -> dict[str, Any]:
        """Pause saved automatic control and enter a non-persistent discovery mode."""
        if _fan_control_running():
            raise RuntimeError("Fan Control을 종료한 뒤 수동 팬 찾기를 사용하세요")
        with self._lock:
            response = self.helper.request({"command": "reset"})
            self._manual_mode = True
            self._manual_channel_id = ""
            self._reset_profile_state()
            self._runtime = {
                "active": False, "control_mode": "manual", "temperature": None,
                "target_percent": None, "error": None, "updated_at": time.time(),
            }
            response["message"] = "수동 팬 찾기 모드입니다. 저장된 자동 설정은 변경되지 않았습니다"
            return response

    def set_manual(self, channel_id: str, percent: int) -> dict[str, Any]:
        if not channel_id:
            raise ValueError("테스트할 팬 채널을 선택하세요")
        if not 20 <= percent <= 100:
            raise ValueError("수동 PWM은 20~100%여야 합니다")
        if _fan_control_running():
            raise RuntimeError("Fan Control을 종료한 뒤 수동 팬 찾기를 사용하세요")
        with self._lock:
            if not self._manual_mode:
                raise ValueError("먼저 수동 팬 찾기 모드로 전환하세요")
            response = self.helper.request({"command": "set", "id": channel_id, "percent": percent})
            applied = int(round(float(response.get("percent", percent))))
            self._manual_channel_id = channel_id
            self._manual_percent = applied
            self._runtime = {
                "active": True, "control_mode": "manual",
                "manual_channel_id": channel_id,
                "temperature": None, "target_percent": applied,
                "error": None, "updated_at": time.time(),
            }
            response["message"] = "선택한 팬을 수동 제어 중입니다. 자동 운전으로 돌아가면 기존 HBM 연동이 즉시 재개됩니다"
            return response

    def exit_manual(self) -> dict[str, Any]:
        """Leave discovery mode. The caller should immediately tick automatic control."""
        with self._lock:
            response = self.helper.request({"command": "reset"})
            self._manual_mode = False
            self._manual_channel_id = ""
            self._reset_profile_state()
            self._runtime = {
                "active": False, "control_mode": "auto", "temperature": None,
                "target_percent": None, "error": None, "updated_at": time.time(),
            }
            response["message"] = "자동 운전으로 복귀했습니다"
            return response

    def reconfigure(self) -> None:
        """Apply an edited curve immediately without carrying the old ramp state."""
        with self._lock:
            self._reset_profile_state()

    def test(self, channel_id: str, percent: int) -> dict[str, Any]:
        config = load_settings()
        if config.get("enabled") or (config.get("cpu") or {}).get("enabled"):
            raise ValueError("자동 팬 제어를 먼저 끈 뒤 채널 테스트를 실행하세요")
        if not 20 <= percent <= 100:
            raise ValueError("테스트 PWM은 20~100%여야 합니다")
        response = self.helper.request({"command": "set", "id": channel_id, "percent": percent})
        response["message"] = "15초 동안 적용되며 이후 메인보드 기본 모드로 자동 복귀합니다"
        return response

    def gpu_tune(self, uuid: str, power: int, clock: int, fan: int) -> dict[str, Any]:
        """Apply privileged NVIDIA settings through the authenticated helper."""
        return self.helper.request({
            "command": "gpu_tune", "uuid": uuid,
            "power": int(power), "clock": int(clock), "fan": int(fan),
        }, timeout=30.0)

    def gpu_temperatures(self) -> list[dict[str, Any]]:
        """Return GPU temperatures exposed by LibreHardwareMonitor/NVAPI."""
        response = self._helper_status(max_age_seconds=2.0)
        values = response.get("gpu_temperatures")
        return values if isinstance(values, list) else []

    def _helper_status(self, max_age_seconds: float = 1.0) -> dict[str, Any]:
        now = time.monotonic()
        cached = self._helper_status_cache
        if cached["value"] and now - cached["time"] < max_age_seconds:
            return dict(cached["value"])
        response = self.helper.request({"command": "status"})
        self._helper_status_cache = {"time": now, "value": dict(response)}
        return response

    def close(self) -> None:
        if not IS_WINDOWS:
            self.helper.stop()
            return
        try:
            if self._process_alive():
                self.helper.request({"command": "reset"}, timeout=2)
        except Exception:
            pass
        self.helper.stop()

    def _process_alive(self) -> bool:
        return IS_WINDOWS and self.helper._pipe is not None


controller = FanController()
