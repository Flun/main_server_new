import os
import re
import shutil
import subprocess
import threading
import time

IS_WINDOWS = os.name == "nt"

GPU_SERVICE_LABELS = {
    "comfyui": {"label": "ComfyUI", "color": "amber"},
    "llama": {"label": "llama.cpp", "color": "sky"},
    "bot": {"label": "봇", "color": "emerald"},
    "watcher": {"label": "와처", "color": "violet"},
}


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
            try:
                power = pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0
            except Exception:
                pass
            gpus.append(
                {
                    "index": i,
                    "name": name,
                    "vendor": "nvidia",
                    "vram_total": mem.total // 1048576,
                    "vram_used": mem.used // 1048576,
                    "vram_free": mem.free // 1048576,
                    "util": util.gpu,
                    "mem_util": round(mem.used * 100 / mem.total, 1) if mem.total else 0,
                    "temp": temp,
                    "power": power,
                    "power_max": None,
                }
            )
    except Exception:
        return None
    return gpus


def _nvidia_smi_backend():
    out = _run(
        [
            "nvidia-smi",
            "--query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu,power.draw,power.limit",
            "--format=csv,noheader,nounits",
        ]
    )
    gpus = []
    for line in out.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 8:
            continue
        try:
            total, used = int(p[2]), int(p[3])
            gpus.append(
                {
                    "index": int(p[0]),
                    "name": p[1],
                    "vendor": "nvidia",
                    "vram_total": total,
                    "vram_used": used,
                    "vram_free": int(p[4]),
                    "util": int(p[5]),
                    "mem_util": round(used * 100 / total, 1) if total else 0,
                    "temp": int(p[6]),
                    "power": float(p[7]) if p[7] else None,
                    "power_max": float(p[8]) if p[8] else None,
                }
            )
        except ValueError:
            continue
    return gpus or None


def get_gpus():
    for backend in (_nvml_backend, _nvidia_smi_backend):
        gpus = backend()
        if gpus:
            return gpus
    return []


def get_gpu_topology():
    gpus = get_gpus()
    services = [
        {
            "name": k,
            "label": v["label"],
            "color": v["color"],
            "device": None,
            "running": False,
        }
        for k, v in GPU_SERVICE_LABELS.items()
    ]
    return {"gpus": gpus, "services": services}


def parse_llama_offload(log_path):
    if not log_path or not os.path.exists(log_path):
        return {}
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return {}

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
    return result


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
        procs.append({"gpu_uuid": p[0], "pid": p[1], "used_mb": p[2], "name": p[3]})
    return procs