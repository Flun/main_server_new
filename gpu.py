import os
import re
import shutil
import subprocess
import threading
import time

import psutil

IS_WINDOWS = os.name == "nt"

GPU_SERVICE_LABELS = {
    "comfyui": {"label": "ComfyUI", "color": "amber"},
    "llama": {"label": "llama.cpp", "color": "sky"},
    "bot": {"label": "봇", "color": "emerald"},
    "watcher": {"label": "와처", "color": "violet"},
    "vllm": {"label": "vLLM", "color": "rose"},
    "system": {"label": "OS / 디스플레이 / 드라이버", "color": "zinc"},
}

_EXTENDED_SENSOR_CACHE = {"time": 0.0, "values": {}}
_EXTENDED_SENSOR_LOCK = threading.Lock()


def _run(cmd, timeout=6):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout or ""
    except Exception:
        return ""


def _nvml_backend():
    gpus = []
    try:
        import pynvml

        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        for i in range(count):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(h)
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            util = pynvml.nvmlDeviceGetUtilizationRates(h)
            temp = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
            power = None
            power_max = None
            power_min = None
            power_default = None
            clock_graphics = None
            clock_max_graphics = None
            fan_speed = None
            try:
                power = pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0
            except Exception:
                pass
            try:
                power_max = pynvml.nvmlDeviceGetEnforcedPowerLimit(h) / 1000.0
            except Exception:
                pass
            try:
                power_min, _ = pynvml.nvmlDeviceGetPowerManagementLimitConstraints(h)
                power_min /= 1000.0
            except Exception:
                pass
            try:
                power_default = pynvml.nvmlDeviceGetPowerManagementDefaultLimit(h) / 1000.0
            except Exception:
                pass
            try:
                clock_graphics = pynvml.nvmlDeviceGetClockInfo(h, pynvml.NVML_CLOCK_GRAPHICS)
                clock_max_graphics = pynvml.nvmlDeviceGetMaxClockInfo(h, pynvml.NVML_CLOCK_GRAPHICS)
            except Exception:
                pass
            try:
                fan_speed = pynvml.nvmlDeviceGetFanSpeed(h)
            except Exception:
                pass
            try:
                uuid = pynvml.nvmlDeviceGetUUID(h)
                if isinstance(uuid, bytes):
                    uuid = uuid.decode("utf-8", errors="replace")
            except Exception:
                uuid = str(i)
            try:
                pci = pynvml.nvmlDeviceGetPciInfo(h).busId
                if isinstance(pci, bytes):
                    pci = pci.decode("utf-8", errors="replace")
            except Exception:
                pci = ""
            gpus.append(
                {
                    "index": i,
                    "uuid": str(uuid),
                    "pci_bus_id": str(pci),
                    "name": name,
                    "vendor": "nvidia",
                    "vram_total": mem.total // 1048576,
                    "vram_used": mem.used // 1048576,
                    "vram_free": mem.free // 1048576,
                    "util": util.gpu,
                    "mem_util": round(mem.used * 100 / mem.total, 1) if mem.total else 0,
                    "temp": temp,
                    "power": power,
                    "power_max": power_max,
                    "power_min_limit": power_min,
                    "power_default_limit": power_default,
                    "clock_graphics": clock_graphics,
                    "clock_max_graphics": clock_max_graphics,
                    "fan_speed": fan_speed,
                }
            )
    except Exception:
        return None
    return gpus


def _nvidia_smi_backend():
    out = _run(
        [
            "nvidia-smi",
            "--query-gpu=index,uuid,pci.bus_id,name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu,power.draw,power.limit",
            "--format=csv,noheader,nounits",
        ]
    )
    gpus = []
    for line in out.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 11:
            continue
        try:
            total, used = int(p[4]), int(p[5])
            gpus.append(
                {
                    "index": int(p[0]),
                    "uuid": p[1],
                    "pci_bus_id": p[2],
                    "name": p[3],
                    "vendor": "nvidia",
                    "vram_total": total,
                    "vram_used": used,
                    "vram_free": int(p[6]),
                    "util": int(p[7]),
                    "mem_util": round(used * 100 / total, 1) if total else 0,
                    "temp": int(p[8]),
                    "power": float(p[9]) if p[9] not in {"", "N/A", "[N/A]"} else None,
                    "power_max": float(p[10]) if p[10] not in {"", "N/A", "[N/A]"} else None,
                }
            )
        except ValueError:
            continue
    return gpus or None


def _extended_nvidia_sensors():
    """Read sensors missing from pynvml, with a short shared subprocess cache."""
    now = time.monotonic()
    with _EXTENDED_SENSOR_LOCK:
        if now - _EXTENDED_SENSOR_CACHE["time"] < 1.0:
            return _EXTENDED_SENSOR_CACHE["values"]
        out = _run(
            [
                "nvidia-smi",
                "--query-gpu=uuid,temperature.memory,utilization.memory,clocks.current.memory",
                "--format=csv,noheader,nounits",
            ]
        )
        values = {}
        for line in out.strip().splitlines():
            parts = [item.strip() for item in line.split(",")]
            if len(parts) != 4:
                continue

            def integer(value):
                try:
                    return int(float(value))
                except (TypeError, ValueError):
                    return None

            values[parts[0]] = {
                "temp_memory": integer(parts[1]),
                "memory_controller_util": integer(parts[2]),
                "clock_memory": integer(parts[3]),
            }
        _EXTENDED_SENSOR_CACHE.update({"time": now, "values": values})
        return values


def get_gpus():
    for backend in (_nvml_backend, _nvidia_smi_backend):
        gpus = backend()
        if gpus:
            extended = _extended_nvidia_sensors()
            for gpu in gpus:
                gpu.update(extended.get(gpu.get("uuid"), {
                    "temp_memory": None, "memory_controller_util": None,
                    "clock_memory": None,
                }))
            return gpus
    return []


def get_gpu_topology():
    gpus = get_gpus()
    processes = scan_vram_processes()
    by_uuid = {gpu.get("uuid"): gpu for gpu in gpus}
    for gpu in gpus:
        gpu["processes"] = []
    for process in processes:
        gpu = by_uuid.get(process.get("gpu_uuid"))
        if gpu is not None:
            gpu["processes"].append(process)
    # NVML의 총 사용량에는 Xorg/Wayland, 프레임버퍼, 드라이버 예약분처럼
    # compute-apps 목록에 나타나지 않는 메모리가 포함됩니다. 그 차이를 별도
    # 영역으로 보여 주면 서비스별 사용량의 합과 실제 총량이 일치합니다.
    for gpu in gpus:
        accounted = sum(p.get("used_mb") or 0 for p in gpu["processes"])
        system_mb = max(0, int(gpu.get("vram_used") or 0) - accounted)
        gpu["system_vram_used_mb"] = system_mb
        gpu["system_vram_used_gb"] = round(system_mb / 1024, 2)
        if system_mb:
            gpu["processes"].append({
                "gpu_uuid": gpu.get("uuid"), "pid": None, "used_mb": system_mb,
                "vram_used_gb": round(system_mb / 1024, 2), "name": "unreported-vram",
                "service": "system", "service_label": GPU_SERVICE_LABELS["system"]["label"],
            })
    return {"available": bool(gpus), "gpus": gpus, "processes": processes}


def parse_llama_offload(log_path):
    if not log_path or not os.path.exists(log_path):
        return {}
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return {}

    # 로그는 실행 간 누적되므로 마지막 시작 구간만 현재 프로세스 정보로 취급합니다.
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].startswith("=====") and "시작:" in lines[index]:
            lines = lines[index:]
            break

    result = {}
    for line in lines:
        m = re.search(r"offloaded\s+(\d+)/(\d+)\s+layers", line)
        if m:
            result["layers_offloaded"] = int(m.group(1))
            result["layers_total"] = int(m.group(2))
        m = re.search(r"VRAM used:\s*([\d.]+)\s*GiB", line)
        if m:
            result["vram_used_gb"] = float(m.group(1))
        m = re.search(r"RAM used:\s*([\d.]+)\s*GiB", line)
        if m:
            result["ram_used_gb"] = float(m.group(1))
        m = re.search(r"offload\s+(\d+)\s+repeat\s+layers", line)
        if m:
            result["repeat_layers"] = int(m.group(1))
        m = re.search(r"CUDA\d+ model buffer size\s*=\s*([\d.]+)\s*MiB", line)
        if m:
            result["gpu_model_buffer_gb"] = round(result.get("gpu_model_buffer_gb", 0) + float(m.group(1)) / 1024, 2)
        m = re.search(r"CPU(?:_Mapped)? model buffer size\s*=\s*([\d.]+)\s*MiB", line)
        if m:
            result["cpu_model_buffer_gb"] = round(result.get("cpu_model_buffer_gb", 0) + float(m.group(1)) / 1024, 2)
    return result


def _classify_process(pid, process_name=""):
    try:
        proc = psutil.Process(int(pid))
        for _ in range(6):
            name = (proc.name() or process_name or "").lower()
            try:
                command = " ".join(proc.cmdline()).lower()
            except (psutil.AccessDenied, psutil.ZombieProcess):
                command = ""
            if "llama-server" in name or "llama-server" in command:
                return "llama"
            if "main.py" in command and "comfyui" in command:
                return "comfyui"
            if "telegram" in command or "comfy_bridge" in command:
                return "bot"
            if "watcher" in command:
                return "watcher"
            if "vllm" in command and ("serve" in command or "openai" in command):
                return "vllm"
            parent = proc.parent()
            if parent is None:
                break
            proc = parent
    except (ValueError, psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        pass
    return "other"


def scan_vram_processes():
    """GPU별 프로세스 (nvidia-smi) — 어떤 서비스가 VRAM을 쓰는지 직관적으로 보여주기 위함."""
    if IS_WINDOWS:
        return []
    out = _run(
        [
            "nvidia-smi",
            "--query-compute-apps=gpu_uuid,pid,used_gpu_memory,process_name",
            "--format=csv,noheader,nounits",
        ],
        timeout=4,
    )
    procs = []
    for line in out.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 4:
            continue
        service = _classify_process(p[1], p[3])
        label = GPU_SERVICE_LABELS.get(service, {"label": "Other"})["label"]
        try:
            used_mb = int(p[2])
        except ValueError:
            used_mb = None
        procs.append({
            "gpu_uuid": p[0],
            "pid": int(p[1]),
            "used_mb": used_mb,
            "vram_used_gb": round(used_mb / 1024, 2) if used_mb is not None else None,
            "name": p[3],
            "service": service,
            "service_label": label,
        })
    return procs
