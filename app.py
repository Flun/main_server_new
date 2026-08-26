import glob
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import psutil
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

import requests

from config import BASE_DIR, IS_WINDOWS, settings
from gpu import (
    build_gpu_topology,
    get_gpus,
    parse_llama_offload,
    scan_vram_processes,
)
from model_hub import router as model_hub_router
from media import router as media_router
from dataset_api import router as dataset_router
from vast_api import router as vast_router
from process_mgr import Service, find_process, tail
from infrastructure import router as infrastructure_router, vllm_service, _ensure_linux_nvcc
from os_boot import router as os_boot_router
from motherboard_fan import (
    controller as motherboard_fan_controller,
    save_settings as save_motherboard_fan_settings,
)
import pawnio_bootstrap
from comfy_model_paths import ModelPathError, ensure_model_config
import cmp170_service

HOST = "0.0.0.0"
PORT = 8999
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0

PRESETS_FILE = os.path.join(BASE_DIR, "presets.json")
MEMO_FILE = os.path.join(BASE_DIR, "memo.txt")
COMYFUI_SETTINGS_FILE = os.path.join(BASE_DIR, "comfyui_settings.json")
HW_HISTORY_FILE = os.path.join(BASE_DIR, "hw_history.json")
GPU_THERMAL_EVENTS_FILE = os.path.join(BASE_DIR, "gpu_thermal_events.json")
LAST_RUN_FILE = os.path.join(BASE_DIR, "last_run.json")
LLAMA_SETTINGS_FILE = os.path.join(
    BASE_DIR, "llama_windows_settings.json" if IS_WINDOWS else "llama_settings.json"
)
LEGACY_LLAMA_SETTINGS_FILE = os.path.join(BASE_DIR, "llama_settings.json")
LLAMA_PORT = int(settings.get("llama_port") or 8080)

services = {
    "comfyui": Service("comfyui"),
    "llama": Service("llama"),
    "bot": Service("bot"),
    "watcher": Service("watcher"),
    "vllm": vllm_service,
    "unsloth": Service("unsloth"),
}

STARTED_AT = time.time()
_catalog_scan_lock = threading.Lock()
_catalog_scan_last = 0.0
_catalog_scan_inflight = False
_sampler_started = threading.Event()
_startup_state_lock = threading.Lock()
_manager_update_lock = threading.Lock()
_startup_state = {
    "ready": False,
    "phase": "starting",
    "message": "프론트엔드 시작 중",
    "started_at": STARTED_AT,
    "completed_at": None,
    "errors": [],
}

STATE = {
    "hw": {
        "gpus": [],
        "cpu_percent": 0.0,
        "cpu_model": "",
        "cpu_physical_cores": None,
        "cpu_logical_cores": None,
        "cpu_frequency_mhz": None,
        "cpu_frequency_max_mhz": None,
        "cpu_load_average": [None, None, None],
        "cpu_package_temp": None,
        "process_count": 0,
        "ram_total": 0,
        "ram_used": 0,
        "ram_available": 0,
        "ram_cached": 0,
        "ram_buffers": 0,
        "ram_percent": 0.0,
        "swap_total": 0,
        "swap_used": 0,
        "swap_percent": 0.0,
        "vram_procs": [],
        "time": 0.0,
    },
    "history": [],
    "thermal_events": [],
    "llama_versions": [],
    "gguf_models": [],
}

app = FastAPI(title="AI Server Manager", docs_url=None, redoc_url=None)
app.include_router(model_hub_router)
app.include_router(media_router)
app.include_router(dataset_router)
app.include_router(vast_router)
app.include_router(infrastructure_router)
app.include_router(os_boot_router)


# ---------- 유틸 ----------

def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


GPU_HBM_WARNING_C = 85
GPU_THERMAL_EVENT_LIMIT = 500
_thermal_event_lock = threading.Lock()
_active_thermal_events = {}
_CPU_MODEL = None


def _load_gpu_thermal_events():
    events = _read_json(GPU_THERMAL_EVENTS_FILE, [])
    if not isinstance(events, list):
        events = []
    changed = False
    for event in events:
        if isinstance(event, dict) and event.get("ended_at") is None:
            event["ended_at"] = event.get("last_seen_at") or event.get("started_at")
            event["ended_reason"] = "manager_restart"
            changed = True
    STATE["thermal_events"] = [event for event in events if isinstance(event, dict)][-GPU_THERMAL_EVENT_LIMIT:]
    if changed:
        _write_json(GPU_THERMAL_EVENTS_FILE, STATE["thermal_events"])


def _thermal_workloads(gpu_uuid, processes):
    workloads = []
    seen = set()
    for process in processes:
        if process.get("gpu_uuid") != gpu_uuid:
            continue
        key = (process.get("pid"), process.get("service"), process.get("name"))
        if key in seen:
            continue
        seen.add(key)
        workloads.append({
            "pid": process.get("pid"),
            "service": process.get("service") or "other",
            "label": process.get("service_label") or "Other",
            "name": process.get("name") or "unknown",
            "vram_used_gb": process.get("vram_used_gb"),
        })
    if not workloads:
        workloads.append({
            "pid": None, "service": "unknown", "label": "미식별 / 시스템",
            "name": "no-compute-process", "vram_used_gb": None,
        })
    return workloads


def _update_gpu_thermal_events(gpus, processes, sampled_at):
    dirty = False
    with _thermal_event_lock:
        for gpu in gpus:
            temperature = gpu.get("temp_memory")
            if temperature is None:
                continue
            uuid = gpu.get("uuid")
            active = _active_thermal_events.get(uuid)
            if temperature > GPU_HBM_WARNING_C:
                workloads = _thermal_workloads(uuid, processes)
                if active is None:
                    active = {
                        "id": f"{uuid}:{int(sampled_at * 1000)}",
                        "gpu_uuid": uuid, "gpu_index": gpu.get("index"),
                        "gpu_name": gpu.get("name"), "threshold_c": GPU_HBM_WARNING_C,
                        "started_at": sampled_at, "last_seen_at": sampled_at,
                        "ended_at": None, "peak_c": temperature,
                        "workloads": workloads,
                    }
                    STATE["thermal_events"].append(active)
                    _active_thermal_events[uuid] = active
                    dirty = True
                else:
                    active["last_seen_at"] = sampled_at
                    if temperature > active.get("peak_c", temperature):
                        active["peak_c"] = temperature
                        dirty = True
                    known = {
                        (item.get("pid"), item.get("service"), item.get("name"))
                        for item in active.get("workloads", [])
                    }
                    for workload in workloads:
                        key = (workload.get("pid"), workload.get("service"), workload.get("name"))
                        if key not in known:
                            active.setdefault("workloads", []).append(workload)
                            known.add(key)
                            dirty = True
            elif active is not None:
                active["last_seen_at"] = sampled_at
                active["ended_at"] = sampled_at
                active["duration_seconds"] = round(sampled_at - active["started_at"], 1)
                _active_thermal_events.pop(uuid, None)
                dirty = True
        if len(STATE["thermal_events"]) > GPU_THERMAL_EVENT_LIMIT:
            STATE["thermal_events"] = STATE["thermal_events"][-GPU_THERMAL_EVENT_LIMIT:]
            dirty = True
        if dirty:
            _write_json(GPU_THERMAL_EVENTS_FILE, STATE["thermal_events"])


_load_gpu_thermal_events()


def load_presets():
    presets = _read_json(PRESETS_FILE, {})
    return presets if isinstance(presets, dict) else {}


def save_presets(presets):
    _write_json(PRESETS_FILE, presets)


def load_comfy_settings():
    return _read_json(COMYFUI_SETTINGS_FILE, {})


def save_comfy_settings(data):
    merged = {
        "listen": True,
        "use_sage_attention": True,
        "disable_cuda_malloc": True,
        "preview_method_none": True,
        "cache_none": False,
        "reserve_vram_enabled": True,
        "reserve_vram": 1.0,
        "disable_async_offload": False,
        "disable_dynamic_vram": True,
        "fast_disk": False,
        "fast_fp16_accumulation": False,
        "gpu_device": "",
    }
    existing = load_comfy_settings()
    if "reserve_vram_enabled" not in existing and "reserve_vram_1" in existing:
        existing["reserve_vram_enabled"] = bool(existing.get("reserve_vram_1"))
        existing["reserve_vram"] = 1.0
    merged.update({k: existing[k] for k in merged if k in existing})
    for key in (
        "listen", "use_sage_attention", "disable_cuda_malloc", "preview_method_none", "cache_none", "reserve_vram_enabled",
        "disable_async_offload", "disable_dynamic_vram", "fast_disk", "fast_fp16_accumulation",
    ):
        if key in data:
            merged[key] = bool(data[key])
    if "reserve_vram" in data:
        try:
            merged["reserve_vram"] = max(0.0, float(data["reserve_vram"]))
        except (TypeError, ValueError):
            raise HTTPException(400, "reserve_vram은 0 이상의 숫자여야 합니다")
    if "gpu_device" in data:
        merged["gpu_device"] = str(data.get("gpu_device") or "").strip()
    _write_json(COMYFUI_SETTINGS_FILE, merged)
    return merged


def normalize_gpu_devices(value):
    if value in (None, ""):
        return []
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        raise HTTPException(400, "GPU 선택값 형식이 잘못되었습니다")
    result = []
    for item in values:
        device = str(item).strip()
        if device and device not in result:
            result.append(device)
    gpus = get_gpus()
    valid = {str(gpu.get("index")) for gpu in gpus} | {str(gpu.get("uuid")) for gpu in gpus}
    invalid = [device for device in result if device not in valid]
    if invalid:
        raise HTTPException(400, f"존재하지 않는 GPU입니다: {', '.join(invalid)}")
    return result


# ---------- llama.cpp 설정 / 모델 스캔 ----------

def normalize_model_root(value):
    value = os.path.expandvars(os.path.expanduser(str(value or "").strip().strip('"')))
    return os.path.normpath(os.path.abspath(value or settings.get("model_root")))


def load_llama_settings():
    data = _read_json(LLAMA_SETTINGS_FILE, {})
    if IS_WINDOWS and not data:
        data = _read_json(LEGACY_LLAMA_SETTINGS_FILE, {})
    return {
        "model_root": normalize_model_root(data.get("model_root", settings.get("model_root"))),
    }


def save_llama_settings(values):
    model_root = normalize_model_root(values.get("model_root"))
    if not os.path.isdir(model_root):
        raise HTTPException(400, f"Model root 폴더가 없습니다: {model_root}")
    saved = {
        "model_root": model_root,
    }
    _write_json(LLAMA_SETTINGS_FILE, saved)
    return saved


def scan_llama_models(root=None):
    root = root or load_llama_settings()["model_root"]
    models, mmproj_models, templates = [], [], []
    if root and os.path.isdir(root):
        for cur, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(cur, d))]
            for fn in files:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in {".gguf", ".jinja", ".jinja2"}:
                    continue
                full = os.path.normpath(os.path.join(cur, fn))
                item = {
                    "name": fn,
                    "label": os.path.relpath(full, root),
                    "path": full,
                    "modified_time": int(os.path.getmtime(full)),
                }
                if ext == ".gguf":
                    parent_parts = Path(item["label"]).parts[:-1]
                    if any(p.lower() == "mmproj" for p in parent_parts) or "mmproj" in fn.lower():
                        mmproj_models.append(item)
                    else:
                        models.append(item)
                else:
                    templates.append(item)
    models.sort(key=lambda i: (-i["modified_time"], i["label"].lower()))
    mmproj_models.sort(key=lambda i: (-i["modified_time"], i["label"].lower()))
    templates.sort(key=lambda i: i["label"].lower())
    return models, mmproj_models, templates


# ---------- llama.cpp 바이너리 / 프리셋 ----------

def _llama_server_exe(version_dir):
    if not version_dir:
        return None
    candidates = [
        os.path.join(version_dir, "build", "bin", "llama-server"),
        os.path.join(version_dir, "bin", "llama-server"),
        os.path.join(version_dir, "build", "bin", "Release", "llama-server"),
        os.path.join(version_dir, "llama-server"),
    ]
    if IS_WINDOWS:
        candidates = [c + ".exe" for c in candidates]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def scan_llama_versions():
    pat = settings.get("llama_version_glob")
    dirs = sorted(
        glob.glob(pat),
        key=lambda path: (os.path.getmtime(path) if os.path.exists(path) else 0, os.path.basename(path).lower()),
        reverse=True,
    ) if pat else []
    versions = []
    for d in dirs:
        if os.path.isdir(d):
            exe = _llama_server_exe(d)
            versions.append({"path": d, "name": os.path.basename(d), "exe": exe or ""})
    STATE["llama_versions"] = versions
    return versions


def scan_gguf_models(root=None):
    root = root or settings.get("model_root")
    models = []
    if root and os.path.isdir(root):
        for f in sorted(glob.glob(os.path.join(root, "*.gguf"))):
            try:
                models.append(
                    {
                        "path": f,
                        "name": os.path.basename(f),
                        "size": os.path.getsize(f),
                        "mtime": int(os.path.getmtime(f)),
                    }
                )
            except OSError:
                continue
    STATE["gguf_models"] = models
    return models


def _refresh_catalogs_if_stale(max_age_seconds=60):
    """Refresh catalogs in the background; dashboard requests never wait for disk scans."""
    global _catalog_scan_inflight
    now = time.monotonic()
    if now - _catalog_scan_last < max_age_seconds:
        return
    with _catalog_scan_lock:
        if now - _catalog_scan_last < max_age_seconds or _catalog_scan_inflight:
            return
        _catalog_scan_inflight = True
    threading.Thread(target=_refresh_catalogs_worker, name="catalog-refresh", daemon=True).start()


def _refresh_catalogs_worker():
    global _catalog_scan_last, _catalog_scan_inflight
    try:
        scan_llama_versions()
        scan_gguf_models()
    except Exception as error:
        print(f"[catalog] 백그라운드 검색 실패: {error}")
    finally:
        with _catalog_scan_lock:
            _catalog_scan_last = time.monotonic()
            _catalog_scan_inflight = False


def _resolve_llama_binary(preset):
    version = (preset.get("version") or "").strip()
    if version and os.path.isdir(version):
        exe = _llama_server_exe(version)
        if exe:
            return exe, version
    versions = scan_llama_versions()
    if versions:
        latest = versions[0]
        return latest["exe"], latest["path"]
    return None, None


def _llama_port_value(preset):
    port = str(preset.get("port", "")).strip() or "8080"
    try:
        value = int(port)
    except ValueError as error:
        raise HTTPException(400, "port는 정수여야 합니다") from error
    if not 1 <= value <= 65535:
        raise HTTPException(400, "port는 1~65535 사이여야 합니다")
    return value


def _build_llama_cmd(preset):
    exe, version_dir = _resolve_llama_binary(preset)
    if not exe:
        raise HTTPException(400, "llama-server 실행 파일을 찾지 못했습니다 (경로 설정 또는 버전 스캔 필요)")
    model = preset.get("model", "")
    if not model or not os.path.isfile(model):
        raise HTTPException(400, f"모델 파일이 없습니다: {model}")
    legacy = preset.get("optionalArgs") if isinstance(preset.get("optionalArgs"), dict) else {}

    def value(key, default=""):
        current = preset.get(key, legacy.get(key, default))
        return str(current if current is not None else "").strip()

    def enabled(key, default=False):
        current = preset.get(key, legacy.get(key, default))
        if isinstance(current, str):
            return current.strip().lower() in {"1", "true", "yes", "on"}
        return bool(current)

    cmd = [exe, "-m", model]
    mmproj = preset.get("mmproj", "")
    if mmproj and os.path.isfile(mmproj):
        cmd += ["--mmproj", mmproj]
    else:
        # 이름이 비슷한 projector를 자동으로 집어 드는 동작을 막아 text/MTP
        # 실행과 vision 실행을 명확히 구분합니다.
        cmd += ["--no-mmproj-auto"]
    alias = value("alias")
    if alias:
        cmd += ["--alias", alias]
    for flag, key in (
        ("--ctx-size", "ctx"),
        ("--host", "host"),
        ("-ngl", "ngl"),
        ("-n", "nPredict"),
        ("-np", "parallel"),
        ("-t", "threads"),
        ("-ctk", "cacheK"),
        ("-ctv", "cacheV"),
    ):
        v = value(key)
        if v:
            cmd += [flag, v]
    cmd += ["--port", str(_llama_port_value(preset))]
    reasoning_mode = str(preset.get("reasoningMode", "")).strip()
    if reasoning_mode in {"on", "off"}:
        cmd += ["--reasoning", reasoning_mode]
    reasoning_budget = str(preset.get("reasoningBudget", "")).strip()
    if reasoning_mode != "off" and reasoning_budget:
        cmd += ["--reasoning-budget", reasoning_budget]
    fit_target = value("fitTarget")
    if enabled("fit"):
        cmd += ["--fit", "on"]
        if fit_target:
            cmd += ["--fit-target", fit_target]
    else:
        # Current llama.cpp defaults --fit to on, so omission is not enough.
        cmd += ["--fit", "off"]
    batch_size = value("batchSize")
    ubatch_size = value("ubatchSize")
    if batch_size and ubatch_size:
        try:
            if int(ubatch_size) > int(batch_size):
                raise HTTPException(400, "ubatch size는 batch size보다 클 수 없습니다")
        except ValueError:
            raise HTTPException(400, "batch size와 ubatch size는 정수여야 합니다")
    for flag, key in (
        ("--batch-size", "batchSize"),
        ("--ubatch-size", "ubatchSize"),
        ("--threads-batch", "threadsBatch"),
        ("--ctx-checkpoints", "ctxCheckpoints"),
        ("--cache-ram", "cacheRam"),
        ("--cache-reuse", "cacheReuse"),
    ):
        current = value(key)
        if current:
            cmd += [flag, current]

    split_mode = value("splitMode")
    if split_mode:
        cmd += ["--split-mode", split_mode]
    tensor_split = value("tensorSplit")
    if tensor_split:
        cmd += ["--tensor-split", tensor_split]
    main_gpu = value("mainGpu")
    if main_gpu:
        cmd += ["--main-gpu", main_gpu]

    cpu_moe = enabled("cpuMoe")
    cpu_moe_layers = value("cpuMoeLayers")
    if cpu_moe and cpu_moe_layers:
        raise HTTPException(400, "--cpu-moe와 --n-cpu-moe는 동시에 사용할 수 없습니다")
    if cpu_moe:
        cmd.append("--cpu-moe")
    elif cpu_moe_layers:
        cmd += ["--n-cpu-moe", cpu_moe_layers]

    spec_type = value("specType")
    allowed_spec_types = {"draft-mtp", "draft-dflash", "draft-dspark", "draft-eagle3", "draft-simple"}
    if spec_type and spec_type not in allowed_spec_types:
        raise HTTPException(400, f"지원하지 않는 speculative type입니다: {spec_type}")
    if spec_type:
        cmd += ["--spec-type", spec_type]
        draft_n_max = value("specDraftNMax", "4")
        if draft_n_max:
            try:
                if not 1 <= int(draft_n_max) <= 64:
                    raise ValueError
            except ValueError:
                raise HTTPException(400, "spec draft N max는 1~64 정수여야 합니다")
            cmd += ["--spec-draft-n-max", draft_n_max]
        draft_model = value("specDraftModel")
        if draft_model:
            if not os.path.isfile(draft_model):
                raise HTTPException(400, f"Draft 모델 파일이 없습니다: {draft_model}")
            cmd += ["--spec-draft-model", draft_model]
        draft_ngl = value("specDraftNgl")
        if draft_ngl:
            cmd += ["--spec-draft-ngl", draft_ngl]
    if enabled("flash"):
        cmd += ["--flash-attn", "on"]
    return cmd


# ---------- ComfyUI / 봇 / 와처 ----------

def _comfy_python():
    py = settings.get("comfyui_python")
    if py and os.path.isfile(py):
        return py
    comfy_dir = settings.get("comfyui_dir")
    if comfy_dir:
        for rel in (
            os.path.join("venv", "Scripts", "python.exe"),
            os.path.join(".venv", "Scripts", "python.exe"),
            os.path.join("venv", "bin", "python"),
            os.path.join(".venv", "bin", "python"),
        ):
            candidate = os.path.join(comfy_dir, rel)
            if os.path.isfile(candidate):
                return candidate
        # ComfyUI_windows_portable: python_embeded는 ComfyUI 소스 폴더의 상위 폴더에 있습니다.
        portable_python = os.path.join(
            os.path.dirname(os.path.abspath(comfy_dir)), "python_embeded", "python.exe"
        )
        if os.path.isfile(portable_python):
            return portable_python
    return "python" if IS_WINDOWS else "python3"


def _comfy_env():
    return {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}


def _comfy_args():
    args = []
    s = load_comfy_settings()
    if s.get("preview_method_none"):
        args += ["--preview-method", "none"]
    if s.get("cache_none"):
        args += ["--cache-none"]
    if s.get("use_sage_attention"):
        args += ["--use-sage-attention"]
    if s.get("disable_cuda_malloc"):
        args += ["--disable-cuda-malloc"]
    if s.get("reserve_vram_enabled"):
        args += ["--reserve-vram", str(s.get("reserve_vram", 1.0))]
    if s.get("disable_async_offload"):
        args += ["--disable-async-offload"]
    if s.get("disable_dynamic_vram"):
        args += ["--disable-dynamic-vram"]
    if s.get("fast_disk"):
        args += ["--fast-disk"]
    if s.get("fast_fp16_accumulation"):
        args += ["--fast", "fp16_accumulation"]
    return args


def _run_dir_python(dir_path, script="main.py"):
    for py in (
        os.path.join(dir_path, "venv", "Scripts", "python.exe"),
        os.path.join(dir_path, ".venv", "Scripts", "python.exe"),
        os.path.join(dir_path, "venv", "bin", "python"),
        os.path.join(dir_path, ".venv", "bin", "python"),
    ):
        if os.path.isfile(py):
            return py
    if not os.path.isfile(os.path.join(dir_path, script)):
        raise HTTPException(400, f"{script} 파일이 없습니다: {dir_path}")
    return "python" if IS_WINDOWS else "python3"


def _unsloth_executable():
    configured = os.path.expandvars(os.path.expanduser(str(settings.get("unsloth_executable") or "")))
    candidates = [configured, shutil.which("unsloth")]
    if not IS_WINDOWS:
        candidates += [os.path.expanduser("~/.local/bin/unsloth")]
    else:
        candidates += [
            os.path.join(os.path.expanduser("~"), ".local", "bin", "unsloth.exe"),
            shutil.which("unsloth.exe"),
        ]
    return next((path for path in candidates if path and os.path.isfile(path)), None)


def _unsloth_port():
    try:
        port = int(settings.get("unsloth_port") or 8890)
    except (TypeError, ValueError) as error:
        raise HTTPException(400, "Unsloth 포트가 올바르지 않습니다") from error
    if not 1 <= port <= 65535:
        raise HTTPException(400, "Unsloth 포트 범위는 1~65535입니다")
    return port


# ---------- 하드웨어 샘플러 ----------

def _cpu_model_name():
    global _CPU_MODEL
    if _CPU_MODEL is not None:
        return _CPU_MODEL
    model = ""
    if not IS_WINDOWS:
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.lower().startswith("model name"):
                        model = line.split(":", 1)[1].strip()
                        break
        except OSError:
            pass
    if not model:
        model = platform.processor().strip()
    _CPU_MODEL = model or "Unknown CPU"
    return _CPU_MODEL


def _cpu_package_temperature():
    """Read the most representative package sensor from psutil/sysfs."""
    if IS_WINDOWS or not hasattr(psutil, "sensors_temperatures"):
        return None
    try:
        sensors = psutil.sensors_temperatures()
    except Exception:
        return None
    priorities = (
        ("k10temp", ("tctl", "tdie")),
        ("coretemp", ("package id 0", "package")),
        ("nct6798", ("cputin",)),
        ("asusec", ("cpu",)),
    )
    normalized = {str(chip).casefold(): entries for chip, entries in sensors.items()}
    for chip, labels in priorities:
        for entry in normalized.get(chip, []):
            if str(entry.label or "").casefold() in labels and 0 < float(entry.current) < 125:
                return round(float(entry.current), 1)
    return None

def _merge_lhm_gpu_temperatures(gpus):
    """Fill consumer GPU memory-junction temperatures missing from NVML."""
    if not IS_WINDOWS or not gpus:
        return gpus
    try:
        sensors = motherboard_fan_controller.gpu_temperatures()
    except Exception:
        return gpus
    unmatched = list(gpus)
    for sensor in sensors:
        name = str(sensor.get("name") or "").casefold()
        gpu = next((item for item in unmatched if str(item.get("name") or "").casefold() == name), None)
        if gpu is None:
            continue
        unmatched.remove(gpu)
        if gpu.get("temp_memory") is None and sensor.get("memory_temperature") is not None:
            gpu["temp_memory"] = round(float(sensor["memory_temperature"]))
            gpu["temp_memory_source"] = "librehardwaremonitor"
        if sensor.get("hotspot_temperature") is not None:
            gpu["temp_hotspot"] = round(float(sensor["hotspot_temperature"]), 1)
    return gpus

def _sample_hw():
    gpus = _merge_lhm_gpu_temperatures(get_gpus())
    processes = scan_vram_processes()
    sampled_at = time.time()
    cpu = psutil.cpu_percent(interval=None)
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    try:
        frequency = psutil.cpu_freq()
    except Exception:
        frequency = None
    try:
        load_average = psutil.getloadavg() if hasattr(psutil, "getloadavg") else (None, None, None)
    except Exception:
        load_average = (None, None, None)
    STATE["hw"] = {
        "gpus": gpus,
        "cpu_percent": cpu,
        "cpu_model": _cpu_model_name(),
        "cpu_physical_cores": psutil.cpu_count(logical=False),
        "cpu_logical_cores": psutil.cpu_count(logical=True),
        "cpu_frequency_mhz": round(frequency.current) if frequency else None,
        "cpu_frequency_max_mhz": round(frequency.max) if frequency and frequency.max else None,
        "cpu_load_average": [round(value, 2) if value is not None else None for value in load_average],
        "cpu_package_temp": _cpu_package_temperature(),
        "process_count": len(psutil.pids()),
        "ram_total": vm.total,
        "ram_used": vm.used,
        "ram_available": vm.available,
        "ram_cached": getattr(vm, "cached", 0),
        "ram_buffers": getattr(vm, "buffers", 0),
        "ram_percent": vm.percent,
        "swap_total": swap.total,
        "swap_used": swap.used,
        "swap_percent": swap.percent,
        "vram_procs": processes,
        "time": sampled_at,
    }
    # The Windows fan helper uses the same already-sampled GPU values. This
    # avoids a second NVML/nvidia-smi poll and refreshes its 15-second PWM lease.
    motherboard_fan_controller.tick(gpus)
    _update_gpu_thermal_events(gpus, processes, sampled_at)
    STATE["history"].append(
        {
            "t": sampled_at,
            "gpu_util": [g["util"] for g in gpus],
            "gpu_mem": [g["mem_util"] for g in gpus],
            "cpu": cpu,
            "ram": vm.percent,
        }
    )
    if len(STATE["history"]) > 1440:
        STATE["history"] = STATE["history"][-1440:]
    if len(STATE["history"]) % 10 == 0:
        _write_json(HW_HISTORY_FILE, STATE["history"][-720:])


def _sampler():
    while True:
        try:
            _sample_hw()
        except Exception:
            pass
        time.sleep(5)


def _start_sampler_once():
    if _sampler_started.is_set():
        return
    _sampler_started.set()
    threading.Thread(target=_sampler, name="hardware-sampler", daemon=True).start()


# ---------- 페이지 ----------

@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))


@app.get("/models")
def models_page():
    # A distinct canonical URL prevents browsers from restoring the pre-recovery
    # Model Hub document from back/forward cache under the old /models URL.
    return RedirectResponse("/model-hub", status_code=307, headers={"Cache-Control": "no-store"})


@app.get("/model-hub", response_class=HTMLResponse)
def model_hub_page():
    return FileResponse(
        os.path.join(BASE_DIR, "models.html"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/media", response_class=HTMLResponse)
def media_page():
    return FileResponse(
        os.path.join(BASE_DIR, "media.html"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/vast", response_class=HTMLResponse)
def vast_page():
    return FileResponse(
        os.path.join(BASE_DIR, "vast.html"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/infrastructure", response_class=HTMLResponse)
def infrastructure_page():
    return FileResponse(
        os.path.join(BASE_DIR, "infrastructure.html"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )



# ---------- 상태 ----------

def _pid_in_dir(pid, dir_path):
    """프로세스가 지정 폴더의 것이냐 — 절대 경로/상대 경로(portable)/cwd 모두 매칭.

    ComfyUI_windows_portable은 `ComfyUI\\main.py` 같은 상대 경로로 실행되어
    전체 경로 문자열 매칭만으로는 감지가 안 됩니다.
    """
    if not dir_path:
        return True
    try:
        proc = psutil.Process(pid)
        cmdline = " ".join(proc.cmdline() or [])
        if dir_path in cmdline:
            return True
        try:
            cwd = (proc.cwd() or "").replace("/", os.sep)
            if cwd.lower() == os.path.normpath(dir_path).lower():
                return True
        except (psutil.AccessDenied, OSError):
            pass
        leaf = os.path.basename(os.path.normpath(dir_path))
        joined = cmdline.replace("/", os.sep)
        return f"{leaf}{os.sep}main.py" in joined
    except Exception:
        return False


def _service_state(name):
    svc = services[name]
    st = svc.info()
    if name == "llama" and not st["running"]:
        pids = find_process(r"llama-server")
        if pids:
            st["running"] = True
            st["pid"] = pids[0]
            st["external"] = True
    if name == "comfyui" and not st["running"]:
        pids = find_process(r"main\.py")
        comfy_dir = settings.get("comfyui_dir")
        if comfy_dir:
            pids = [p for p in pids if _pid_in_dir(p, comfy_dir)]
        if pids:
            st["running"] = True
            st["pid"] = pids[0]
            st["external"] = True
    if name == "vllm" and not st["running"]:
        pids = find_process(r"vllm\s+serve|vllm\.entrypoints\.openai")
        if pids:
            st["running"] = True
            st["pid"] = pids[0]
            st["external"] = True
    if name == "unsloth":
        executable = _unsloth_executable()
        st["available"] = bool(executable)
        if not st["running"]:
            pids = find_process(r"unsloth(?:\.exe)?\s+studio(?:\s|$)")
            if pids:
                st["running"] = True
                st["pid"] = pids[0]
                st["external"] = True
    return st


@app.get("/api/status")
def status():
    _refresh_catalogs_if_stale()
    with _startup_state_lock:
        initialization = dict(_startup_state)
        initialization["errors"] = list(_startup_state["errors"])
    return {
        "platform": "linux" if not IS_WINDOWS else "windows",
        "is_windows": IS_WINDOWS,
        "hostname": os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME", ""),
        "server_uptime": round(time.time() - STARTED_AT),
        "services": {k: _service_state(k) for k in services},
        "gpus": STATE["hw"]["gpus"],
        "llama_versions": STATE["llama_versions"],
        "gguf_models": STATE["gguf_models"],
        "settings": settings.all() if not IS_WINDOWS else {},
        "llama_port": LLAMA_PORT,
        "last_run": _read_json(LAST_RUN_FILE, {}),
        "initialization": initialization,
    }


@app.get("/api/gpus")
def gpus():
    # The 5-second safety sampler already paid for NVML, nvidia-smi and process
    # classification. Reuse that snapshot instead of polling the hardware again
    # for every browser refresh.
    topo = build_gpu_topology(STATE["hw"].get("gpus", []), STATE["hw"].get("vram_procs", []))
    processes = topo.get("processes", [])
    index_by_uuid = {gpu.get("uuid"): gpu.get("index") for gpu in topo.get("gpus", [])}
    def configured_devices(value):
        if value in (None, ""):
            return []
        values = value.split(",") if isinstance(value, str) else value
        return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))

    comfy_saved = load_comfy_settings()
    last_run = _read_json(LAST_RUN_FILE, {})
    configured = {
        "comfyui": configured_devices([comfy_saved.get("gpu_device")]),
        "llama": configured_devices(last_run.get("gpuDevices") or last_run.get("device") or []),
        "bot": configured_devices(services["bot"].device or []),
        "watcher": configured_devices(services["watcher"].device or []),
        "vllm": configured_devices(services["vllm"].device or []),
        "unsloth": configured_devices(services["unsloth"].device or []),
    }
    service_defs = {
            "comfyui": {"label": "ComfyUI", "color": "amber"},
            "llama": {"label": "llama.cpp", "color": "sky"},
            "bot": {"label": "봇", "color": "emerald"},
            "watcher": {"label": "와처", "color": "violet"},
            "vllm": {"label": "vLLM", "color": "rose"},
            "unsloth": {"label": "Unsloth", "color": "lime"},
    }
    topo["services"] = {}
    for name, definition in service_defs.items():
        owned = [process for process in processes if process.get("service") == name]
        active = sorted({index_by_uuid.get(process.get("gpu_uuid")) for process in owned} - {None})
        topo["services"][name] = {
            "name": name,
            **definition,
            "configured_devices": configured[name],
            "active_gpu_indexes": active,
            "vram_used_gb": round(sum(process.get("vram_used_gb") or 0 for process in owned), 2),
            "running": _service_state(name)["running"],
        }
    topo["services"]["llama"]["offload"] = parse_llama_offload(services["llama"].log_file)
    system_mb = sum(gpu.get("system_vram_used_mb") or 0 for gpu in topo.get("gpus", []))
    topo["services"]["system"] = {
        "name": "system", "label": "OS / 디스플레이 / 드라이버", "color": "zinc",
        "configured_devices": [], "active_gpu_indexes": [],
        "vram_used_gb": round(system_mb / 1024, 2), "running": True,
    }
    return topo


@app.get("/api/hardware")
def hardware():
    return STATE["hw"]


@app.get("/api/cmp170/unlock")
def cmp170_unlock_status():
    return cmp170_service.status()


@app.post("/api/cmp170/unlock")
async def cmp170_unlock_action(request: Request):
    data = await request.json()
    try:
        return cmp170_service.start(str(data.get("action") or ""))
    except (RuntimeError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/hardware/history")
def hardware_history():
    return {"history": STATE["history"]}


@app.get("/api/motherboard-fans")
def motherboard_fans_get():
    return motherboard_fan_controller.status(include_channels=True)


@app.post("/api/motherboard-fans/config")
def motherboard_fans_config(data: dict):
    try:
        normalized = save_motherboard_fan_settings(data)
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(400, f"메인보드 팬 설정이 올바르지 않습니다: {error}") from error

    if normalized.get("enabled") and normalized.get("fan_role") == "cmp170hx_hbm":
        try:
            snapshot = _gpu_tuning_snapshot()
        except Exception as error:
            normalized["enabled"] = False
            save_motherboard_fan_settings(normalized)
            raise HTTPException(400, f"CMP 170HX 식별에 실패해 팬 제어를 활성화하지 않았습니다: {error}") from error
        selected = next((gpu for gpu in snapshot.get("gpus", []) if gpu.get("uuid") == normalized.get("gpu_uuid")), None)
        if not selected or selected.get("profile") != "cmp_170hx":
            normalized["enabled"] = False
            save_motherboard_fan_settings(normalized)
            raise HTTPException(400, "CMP 170HX 팬 역할에는 CMP 170HX로 식별된 GPU를 선택해야 합니다")

    if not normalized.get("enabled") and not (normalized.get("cpu") or {}).get("enabled"):
        try:
            motherboard_fan_controller.reset()
        except Exception:
            pass
    else:
        motherboard_fan_controller.reconfigure()
        motherboard_fan_controller.tick(STATE["hw"].get("gpus", []))
    return motherboard_fan_controller.status(include_channels=True)


@app.post("/api/motherboard-fans/action")
def motherboard_fans_action(data: dict):
    action = str(data.get("action") or "")
    try:
        if action == "reset":
            result = motherboard_fan_controller.reset()
        elif action == "manual_enter":
            result = motherboard_fan_controller.enter_manual()
        elif action == "manual_set":
            result = motherboard_fan_controller.set_manual(
                str(data.get("channel_id") or ""), int(data.get("percent", 60))
            )
        elif action == "manual_stop":
            result = motherboard_fan_controller.exit_manual()
            motherboard_fan_controller.tick(STATE["hw"].get("gpus", []))
        elif action == "test":
            result = motherboard_fan_controller.test(str(data.get("channel_id") or ""), int(data.get("percent", 70)))
        else:
            raise ValueError("지원하지 않는 팬 작업입니다")
    except (TypeError, ValueError, RuntimeError) as error:
        raise HTTPException(400, str(error)) from error
    result["status"] = motherboard_fan_controller.status(include_channels=True)
    return result


@app.get("/api/gpu/thermal-events")
def gpu_thermal_events():
    with _thermal_event_lock:
        events = list(reversed(STATE["thermal_events"]))
    counts = {}
    for event in events:
        uuid = event.get("gpu_uuid")
        counts[uuid] = counts.get(uuid, 0) + 1
    return {
        "threshold_c": GPU_HBM_WARNING_C,
        "counts": counts,
        "events": events,
    }


@app.post("/api/gpu/thermal-events/clear")
def gpu_thermal_events_clear():
    with _thermal_event_lock:
        STATE["thermal_events"] = []
        _active_thermal_events.clear()
        _write_json(GPU_THERMAL_EVENTS_FILE, [])
    return {"ok": True}


# ---------- llama.cpp ----------

@app.get("/api/llama/presets")
def llama_presets():
    return {"presets": load_presets()}


@app.post("/api/llama/presets")
def llama_presets_save(presets: dict):
    save_presets(presets)
    return {"ok": True, "presets": load_presets()}


@app.post("/api/llama/start")
def llama_start(preset: dict):
    effective = dict(preset)
    cmd = _build_llama_cmd(effective)
    devices = normalize_gpu_devices(effective.get("gpuDevices") or effective.get("device") or [])
    pid = services["llama"].start(cmd, device=devices or None)
    _write_json(
        LAST_RUN_FILE,
        {"version": _resolve_llama_binary(effective)[1] or "", **effective, "gpuDevices": devices, "device": ""},
    )
    return {
        "ok": True,
        "pid": pid,
        "cmd": cmd,
        "url": f"http://127.0.0.1:{_llama_port_value(effective)}",
        "gpu_devices": devices,
        "warnings": [],
    }


@app.post("/api/llama/stop")
def llama_stop():
    return {"ok": services["llama"].stop()}


@app.get("/api/llama/offload")
def llama_offload():
    return parse_llama_offload(services["llama"].log_file)


@app.get("/api/llama/settings")
def llama_settings_get():
    return load_llama_settings()


@app.post("/api/llama/settings")
def llama_settings_save(values: dict):
    return save_llama_settings(values)


@app.get("/api/llama/scan")
def llama_scan():
    ls = load_llama_settings()
    models, mmproj, templates = scan_llama_models(ls["model_root"])
    return {
        "versions": scan_llama_versions(),
        "model_root": ls["model_root"],
        "llama_install_root": settings.get("llama_install_root"),
        "models": models,
        "mmproj_models": mmproj,
        "templates": templates,
    }


@app.post("/api/llama/cleanup")
def llama_cleanup():
    versions = scan_llama_versions()
    if not versions:
        return {"status": "no_versions", "message": "버전 폴더가 없습니다."}
    latest = versions[0]
    to_delete = [v for v in versions[1:] if os.path.isdir(v["path"])]
    if not to_delete:
        return {"status": "no_old_versions", "message": f"최신 버전만 존재합니다: {latest['name']}"}
    deleted, failed = [], []
    for v in to_delete:
        try:
            shutil.rmtree(v["path"], ignore_errors=True)
            (deleted if not os.path.exists(v["path"]) else failed).append(v["name"])
        except Exception:
            failed.append(v["name"])
    scan_llama_versions()
    return {"status": "completed", "kept": latest["name"], "deleted": deleted, "failed": failed}


LLAMA_GIT_REPO = "https://github.com/ggml-org/llama.cpp"
GITHUB_API = "https://api.github.com/repos/ggml-org/llama.cpp"
NIGHTLY_TAG_RE = re.compile(r"^b\d+$")

LLAMA_BUILD_STATE = {
    "busy": False,
    "tag": "",
    "message": "",
    "started_at": 0.0,
}


def _win_cuda_zip_assets(release):
    """릴리스에서 현재 아키텍처용 Windows CUDA 프리빌트 zip만 추린다 (url 포함).

    cudart 번들(런타임 포함 대형)과 다른 아키텍처는 제외하고,
    표준 정적 llama-* 빌드만 노출한다 → CUDA 버전별 깔끔한 선택지.
    """
    import platform
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    out = []
    for a in release.get("assets", []) or []:
        name = a.get("name", "")
        n = name.lower()
        if "win" not in n or "cuda" not in n or not n.endswith(".zip"):
            continue
        if arch not in n:
            continue
        if "cudart" in n:
            continue
        url = a.get("browser_download_url") or a.get("download_url") or ""
        if url:
            out.append({"name": name, "size": a.get("size", 0), "url": url})
    return out


def _driver_max_cuda():
    """드라이버가 지원하는 최대 CUDA 버전 (nvidia-smi 헤더). 알 수 없으면 None."""
    try:
        out = subprocess.run(
            ["nvidia-smi"], capture_output=True, text=True, timeout=10,
            creationflags=(subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0),
        ).stdout
        m = re.search(r"CUDA Version:\s*(\d+)\.(\d+)", out)
        if m:
            return int(m.group(1)) * 100 + int(m.group(2))
    except Exception:
        pass
    return None


@app.get("/api/llama/releases")
def llama_releases():
    try:
        # stable(vX.Y.Z)과 nightly(bXXXX)를 API 호출 한 번에 모두 확인한다.
        # 공식 레포의 stable 릴리스에는 바이너리가 없고, nightly에만
        # Windows CUDA 프리빌트 zip이 딸려 온다 (stable의 nightly-tag.txt도
        # 이 nightly 태그를 가리킨다). 따라서 "최신 CUDA 기반 빌드"는
        # 최신 nightly가 곧 그것이다.
        res = requests.get(f"{GITHUB_API}/releases?per_page=50", timeout=15)
        res.raise_for_status()
        rels = res.json()
        stable = next(
            (r for r in rels if not NIGHTLY_TAG_RE.fullmatch(r.get("tag_name", ""))), None
        ) or {}
        windows_nightly = next(
            (
                r
                for r in rels
                if NIGHTLY_TAG_RE.fullmatch(r.get("tag_name", "")) and _win_cuda_zip_assets(r)
            ),
            None,
        )

        latest_nightly = next(
            (r for r in rels if NIGHTLY_TAG_RE.fullmatch(r.get("tag_name", ""))), None
        )
        if IS_WINDOWS and windows_nightly:
            data = windows_nightly  # Windows: CUDA 프리빌트가 있는 최신 빌드
        else:
            # Linux는 오래된 stable(v0.3.0)이 아니라 최신 bNNNN 소스를
            # CUDA로 빌드한다. nightly 태그는 git 설치가 가능하다.
            data = latest_nightly or stable or (rels[0] if rels else {})
        tag = data.get("tag_name", "")
        # 설치 여부: <root>/llama-<tag> 또는 <root>/llama-<tag>-cuda* 중 하나라도 있으면 True
        root = settings.get("llama_install_root")
        installed = os.path.isdir(os.path.join(root, f"llama-{tag}")) or bool(
            glob.glob(os.path.join(root, f"llama-{tag}-cuda*"))
        )
        return {
            "tag_name": tag,
            "stable_tag": stable.get("tag_name", ""),
            "nightly_tag": latest_nightly.get("tag_name", "") if latest_nightly else "",
            "published_at": data.get("published_at"),
            "html_url": data.get("html_url"),
            "platform": "linux" if not IS_WINDOWS else "windows",
            "git_url": LLAMA_GIT_REPO,
            "source_url": f"{LLAMA_GIT_REPO}/archive/refs/tags/{tag}.tar.gz",
            "installed": installed,
            "install_root": root,
            "assets": _win_cuda_zip_assets(data) if IS_WINDOWS else [],
            "releases": [
                {
                    "tag_name": release.get("tag_name", ""),
                    "published_at": release.get("published_at"),
                    "prerelease": bool(release.get("prerelease")),
                    "nightly": bool(NIGHTLY_TAG_RE.fullmatch(release.get("tag_name", ""))),
                    "assets": _win_cuda_zip_assets(release) if IS_WINDOWS else [],
                    "installable": (not IS_WINDOWS) or bool(_win_cuda_zip_assets(release)),
                }
                for release in rels
                if _safe_release_tag(release.get("tag_name", ""))
            ],
            "driver_max_cuda": _driver_max_cuda(),
            "build_state": LLAMA_BUILD_STATE,
        }
    except Exception as e:
        raise HTTPException(500, detail=str(e))


def _llama_install_log(msg):
    log = os.path.join(BASE_DIR, "logs", "llama_install.log")
    with open(log, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def _safe_tag(tag):
    tag = str(tag or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", tag or ""):
        raise HTTPException(400, f"잘못된 버전 태그입니다: {tag}")
    return tag


def _safe_release_tag(tag):
    return bool(re.fullmatch(r"[A-Za-z0-9._-]+", str(tag or "").strip()))


def _run_llama_logged(command, *, cwd=None, env=None):
    _llama_install_log("실행: " + " ".join(str(value) for value in command))
    log_path = os.path.join(BASE_DIR, "logs", "llama_install.log")
    with open(log_path, "a", encoding="utf-8", errors="replace") as stream:
        subprocess.run(
            command, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT,
            check=True, creationflags=NO_WINDOW,
        )


def _ensure_llama_linux_environment():
    """Install Linux build prerequisites and prepare only the managed root."""
    if IS_WINDOWS:
        return {}
    LLAMA_BUILD_STATE["message"] = "Linux CUDA 빌드 환경 확인"
    missing = []
    if not shutil.which("cmake"):
        missing.append("cmake")
    if not shutil.which("ninja"):
        missing.append("ninja-build")
    package = subprocess.run(
        ["dpkg-query", "-W", "-f=${Status}", "libcurl4-openssl-dev"],
        capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW,
    )
    if "ok installed" not in package.stdout:
        missing.append("libcurl4-openssl-dev")
    if missing:
        prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
        _run_llama_logged([*prefix, "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "update"])
        _run_llama_logged([
            *prefix, "env", "DEBIAN_FRONTEND=noninteractive",
            "apt-get", "install", "-y", *missing,
        ])

    nvcc = _ensure_linux_nvcc("13.0")
    root = Path(settings.get("llama_install_root")).resolve()
    if root == Path("/opt/llama") and not root.exists():
        prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
        _run_llama_logged([
            *prefix, "install", "-d", "-o", str(os.getuid()), "-g", str(os.getgid()),
            "-m", "0755", str(root),
        ])
    else:
        root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir() or not os.access(root, os.W_OK | os.X_OK):
        raise RuntimeError(f"llama.cpp 설치 경로에 쓸 수 없습니다: {root}")

    architecture_result = subprocess.run(
        ["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW,
    )
    architectures = sorted({
        value.strip().replace(".", "") for value in architecture_result.stdout.splitlines()
        if re.fullmatch(r"\d+\.\d+", value.strip())
    })
    build_env = dict(os.environ)
    cuda_root = str(Path(nvcc).resolve().parent.parent)
    build_env["CUDACXX"] = nvcc
    build_env["CUDA_HOME"] = cuda_root
    return {
        "root": str(root), "nvcc": nvcc, "cuda_root": cuda_root,
        "architectures": architectures, "env": build_env,
    }


def _build_llama_git(tag):
    """최신 llama.cpp를 git에서 클론해 CUDA로 빌드하고 /opt/llama-<tag> 에 설치한다."""
    LLAMA_BUILD_STATE.update(busy=True, tag=tag, message="시작", started_at=time.time())
    stage_dir = None
    try:
        _llama_install_log(f"===== llama.cpp {tag} git 빌드 시작 =====")
        environment = _ensure_llama_linux_environment()
        install_root = environment["root"]

        # 소스 클론 (이미 있으면 최신 태그로 갱신)
        # Keep build sources under the configured llama.cpp root so changing
        # the installation directory really moves the whole managed tree.
        src_dir = os.path.join(install_root, ".src", tag)
        os.makedirs(os.path.dirname(src_dir), exist_ok=True)
        if not os.path.exists(os.path.join(src_dir, ".git")):
            _run_llama_logged(
                ["git", "clone", "--depth", "1", "--branch", tag, LLAMA_GIT_REPO, src_dir],
                env=environment["env"],
            )
        else:
            _run_llama_logged(["git", "fetch", "--depth", "1", "origin", "tag", tag], cwd=src_dir, env=environment["env"])
            _run_llama_logged(["git", "checkout", "--force", tag], cwd=src_dir, env=environment["env"])

        build_dir = os.path.join(src_dir, "build")
        os.makedirs(build_dir, exist_ok=True)

        LLAMA_BUILD_STATE["message"] = "CMake 구성 (GGML_CUDA=ON)"
        configure = [
            "cmake", "-S", ".", "-B", "build", "-G", "Ninja",
            "-DGGML_CUDA=ON", "-DCMAKE_BUILD_TYPE=Release",
            "-DGGML_CUDA_F16=ON", "-DBUILD_SHARED_LIBS=OFF", "-DLLAMA_CURL=OFF",
            f"-DCMAKE_CUDA_COMPILER={environment['nvcc']}",
        ]
        if environment["architectures"]:
            configure.append("-DCMAKE_CUDA_ARCHITECTURES=" + ";".join(environment["architectures"]))
        _run_llama_logged(configure, cwd=src_dir, env=environment["env"])

        nproc = min(os.cpu_count() or 4, 16)
        LLAMA_BUILD_STATE["message"] = "CUDA 빌드 중"
        _run_llama_logged(
            ["cmake", "--build", "build", "--target", "llama-server", "llama-cli", "-j", str(nproc)],
            cwd=src_dir, env=environment["env"],
        )

        install_dir = os.path.join(install_root, f"llama-{tag}")
        unique = f"{os.getpid()}-{threading.get_ident()}-{int(time.time() * 1000)}"
        stage_dir = f"{install_dir}.installing-{unique}"
        backup_dir = f"{install_dir}.backup-{unique}"
        os.makedirs(os.path.join(stage_dir, "bin"))
        bin_src = os.path.join(build_dir, "bin")
        for fn in os.listdir(bin_src):
            source = os.path.join(bin_src, fn)
            if os.path.isfile(source):
                shutil.copy2(source, os.path.join(stage_dir, "bin"))
        staged_exe = _llama_server_exe(stage_dir)
        if not staged_exe:
            raise RuntimeError("빌드 결과에 llama-server가 없습니다")
        verify = subprocess.run(
            [staged_exe, "--version"], capture_output=True, text=True,
            timeout=30, creationflags=NO_WINDOW,
        )
        if verify.returncode:
            raise RuntimeError("빌드된 llama-server 실행 검증 실패: " + (verify.stderr.strip() or verify.stdout.strip()))
        if os.path.exists(install_dir):
            os.replace(install_dir, backup_dir)
        try:
            os.replace(stage_dir, install_dir)
            stage_dir = None
        except Exception:
            if os.path.exists(backup_dir) and not os.path.exists(install_dir):
                os.replace(backup_dir, install_dir)
            raise
        if os.path.exists(backup_dir):
            shutil.rmtree(backup_dir, ignore_errors=True)
        _llama_install_log(f"설치 완료: {install_dir}")
        LLAMA_BUILD_STATE["message"] = f"설치 완료: {install_dir}"
    except Exception as e:
        _llama_install_log(f"빌드 실패: {e}")
        LLAMA_BUILD_STATE["message"] = f"실패: {e}"
    finally:
        if stage_dir and os.path.isdir(stage_dir):
            shutil.rmtree(stage_dir, ignore_errors=True)
        LLAMA_BUILD_STATE["busy"] = False
        scan_llama_versions()


def _find_vcvars():
    """VS 2022/2019(Build Tools/Community 등) vcvars64.bat 탐색.

    반환: (vcvars64.bat 경로, CMake generator) 또는 (None, None)
    """
    roots = [
        r"C:\Program Files\Microsoft Visual Studio",
        r"C:\Program Files (x86)\Microsoft Visual Studio",
    ]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for year, gen in (("2022", "Visual Studio 17 2022"), ("2019", "Visual Studio 16 2019")):
            pat = os.path.join(root, year, "*", "VC", "Auxiliary", "Build", "vcvars64.bat")
            for p in sorted(glob.glob(pat)):
                if os.path.isfile(p):
                    return p, gen
    return None, None


def _find_cuda_toolkit():
    """nvcc가 있는 CUDA Toolkit 루트 반환 (PATH 우선, 없으면 Program Files 스캔).

    반환: (toolkit 루트, '12.4' 같은 버전 문자열) 또는 (None, None)
    """
    nvcc = shutil.which("nvcc")
    if nvcc:
        root = os.path.dirname(os.path.dirname(os.path.abspath(nvcc)))
        m = re.search(r"v(\d+)\.(\d+)", root)
        ver = f"{m.group(1)}.{m.group(2)}" if m else ""
        return root, ver
    hits = sorted(
        glob.glob(r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v*\bin\nvcc.exe"),
        reverse=True,
    )
    if hits:
        root = os.path.dirname(os.path.dirname(hits[0]))
        m = re.search(r"v(\d+)\.(\d+)", root)
        ver = f"{m.group(1)}.{m.group(2)}" if m else ""
        return root, ver
    return None, None


def _build_llama_git_windows(tag):
    """git에서 tag를 클론해 MSVC + CUDA(GGML_CUDA=ON)로 빌드한다.

    설치 위치: <root>/llama-<tag>-cuda<toolkit버전>  (C:\\llama\\llama-<tag>-cuda12.4 등)
    빌드 도구가 없으면 설치 안내 메시지로 안전하게 실패한다.
    """
    LLAMA_BUILD_STATE.update(busy=True, tag=tag, message="시작", started_at=time.time())
    stage_dir = None
    try:
        _llama_install_log(f"===== llama.cpp {tag} Windows git 빌드 시작 =====")

        cmake = shutil.which("cmake") or (
            r"C:\Program Files\CMake\bin\cmake.exe"
            if os.path.isfile(r"C:\Program Files\CMake\bin\cmake.exe") else None
        )
        vcvars, generator = _find_vcvars()
        cuda_root, cuda_ver = _find_cuda_toolkit()

        missing = []
        if not cmake:
            missing.append("CMake")
        if not vcvars:
            missing.append("Visual Studio Build Tools (C++ 클라이언트 도구)")
        if not cuda_root:
            missing.append("CUDA Toolkit (nvcc)")
        if missing:
            msg = "실패: 빌드 도구가 없습니다 — 설치 후 다시 시도하세요: " + ", ".join(missing)
            _llama_install_log(msg)
            LLAMA_BUILD_STATE["message"] = msg
            return

        # 소스 클론: <root>/.src/<tag> (Linux와 동일한 관리 트리)
        src_dir = os.path.join(settings.get("llama_install_root"), ".src", tag)
        os.makedirs(src_dir, exist_ok=True)
        if not os.path.exists(os.path.join(src_dir, ".git")):
            _llama_install_log(f"git clone --depth 1 --branch {tag} {LLAMA_GIT_REPO}")
            subprocess.run(
                ["git", "clone", "--depth", "1", "--branch", tag, LLAMA_GIT_REPO, src_dir],
                check=True, creationflags=NO_WINDOW,
            )
        else:
            _llama_install_log("소스 이미 존재, git fetch")
            subprocess.run(["git", "fetch", "--depth", "1", "origin", "tag", tag], cwd=src_dir, check=True, creationflags=NO_WINDOW)
            subprocess.run(["git", "checkout", tag], cwd=src_dir, check=True, creationflags=NO_WINDOW)

        build_dir = os.path.join(src_dir, "build")
        os.makedirs(build_dir, exist_ok=True)

        def _q(s):
            return f'"{s}"' if " " in s else s

        # MSVC 환경은 vcvars 셸 안에서만 존재 → cmd /c 로 실행
        cfg_args = [
            "-B", "build", "-G", generator, "-A", "x64",
            "-DGGML_CUDA=ON",
            f"-DCUDAToolkit_ROOT={cuda_root}",
            "-DGGML_CUDA_F16=ON",
            "-DBUILD_SHARED_LIBS=OFF",
            "-DCMAKE_BUILD_TYPE=Release",
        ]
        cfg_line = " ".join(_q(a) for a in cfg_args)
        _llama_install_log(f'call "{vcvars}" && {cmake} {cfg_line}')
        LLAMA_BUILD_STATE["message"] = "CMake 구성 (GGML_CUDA=ON)"
        subprocess.run(
            ["cmd", "/c", f'call "{vcvars}" && {_q(cmake)} {cfg_line}'],
            cwd=src_dir, check=True, capture_output=True, creationflags=NO_WINDOW,
        )

        nproc = os.cpu_count() or 4
        build_line = f"{_q(cmake)} --build build --config Release -j {nproc}"
        _llama_install_log(f'call "{vcvars}" && {build_line}')
        LLAMA_BUILD_STATE["message"] = "CUDA 빌드 중"
        subprocess.run(
            ["cmd", "/c", f'call "{vcvars}" && {build_line}'],
            cwd=src_dir, check=True, capture_output=True, creationflags=NO_WINDOW,
        )

        # 설치: <root>/llama-<tag>-cuda<toolkit버전>/bin/...
        suffix = f"-cuda{cuda_ver}" if cuda_ver else ""
        install_dir = os.path.join(settings.get("llama_install_root"), f"llama-{tag}{suffix}")
        bin_src = os.path.join(build_dir, "bin", "Release")
        if not os.path.isdir(bin_src) or not any(
            f.startswith("llama-server") for f in os.listdir(bin_src)
        ):
            raise RuntimeError("빌드 결과가 없습니다: build\\bin\\Release (logs/llama_install.log 확인)")
        unique = f"{os.getpid()}-{threading.get_ident()}-{int(time.time() * 1000)}"
        stage_dir = f"{install_dir}.installing-{unique}"
        os.makedirs(os.path.join(stage_dir, "bin"))
        for fn in os.listdir(bin_src):
            shutil.copy2(os.path.join(bin_src, fn), os.path.join(stage_dir, "bin"))
        staged_exe = _llama_server_exe(stage_dir)
        verify = subprocess.run(
            [staged_exe, "--version"], capture_output=True, text=True,
            timeout=30, creationflags=NO_WINDOW,
        )
        if verify.returncode:
            raise RuntimeError(
                "빌드된 llama-server 실행 검증 실패: "
                + (verify.stderr.strip() or verify.stdout.strip())
            )
        backup_dir = f"{install_dir}.backup-{unique}"
        if os.path.exists(install_dir):
            os.replace(install_dir, backup_dir)
        try:
            os.replace(stage_dir, install_dir)
            stage_dir = None
        except Exception:
            if os.path.exists(backup_dir) and not os.path.exists(install_dir):
                os.replace(backup_dir, install_dir)
            raise
        if os.path.exists(backup_dir):
            shutil.rmtree(backup_dir, ignore_errors=True)
        _llama_install_log(f"설치 완료: {install_dir}")
        LLAMA_BUILD_STATE["message"] = f"설치 완료: {install_dir}"
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors="ignore") if isinstance(e.stderr, bytes) else (e.stderr or "")
        tail_lines = (err or "").strip().splitlines()[-5:]
        _llama_install_log(f"빌드 실패: {e} | " + "\n".join(tail_lines))
        LLAMA_BUILD_STATE["message"] = "실패: 빌드 오류 — logs/llama_install.log 확인"
    except Exception as e:
        _llama_install_log(f"빌드 실패: {e}")
        LLAMA_BUILD_STATE["message"] = f"실패: {e}"
    finally:
        if stage_dir and os.path.isdir(stage_dir):
            shutil.rmtree(stage_dir, ignore_errors=True)
        LLAMA_BUILD_STATE["busy"] = False
        scan_llama_versions()


def _fetch_release_assets(tag):
    res = requests.get(f"{GITHUB_API}/releases/tags/{tag}", timeout=20)
    res.raise_for_status()
    return _win_cuda_zip_assets(res.json())


def _asset_cuda_ver(name):
    m = re.search(r"cuda[-_]?(\d+)(?:\.(\d+))?", str(name).lower())
    return int(m.group(1)) * 100 + int(m.group(2) or 0) if m else 0


def _asset_cuda_str(name):
    """자산 이름에서 CUDA 버전 문자열 추출 (예: '...cuda-12.4...' -> '12.4')."""
    m = re.search(r"cuda[-_]?(\d+)(?:\.(\d+))?", str(name).lower())
    if not m:
        return ""
    return m.group(1) + ("." + m.group(2) if m.group(2) else "")


def _pick_windows_asset(assets, preferred="", driver_max=None):
    """Windows CUDA zip을 고른다.

    지정한 자산이 있으면 그것, 없으면 x64 후보 안에서 드라이버가 지원하는
    (nvidia-smi 최대 CUDA <= driver_max) 버전 중 가장 높은 것을 선택한다.
    호환 버전이 없으면 설치를 중단해 드라이버와 맞지 않는 빌드를 막는다.
    """
    if preferred:
        for asset in assets:
            if asset.get("name") == preferred:
                cuda = _asset_cuda_ver(asset.get("name", ""))
                if driver_max is not None and cuda and cuda > driver_max:
                    raise HTTPException(
                        400,
                        f"선택한 CUDA {_asset_cuda_str(asset['name'])} 빌드는 현재 드라이버 최대 CUDA "
                        f"{driver_max // 100}.{driver_max % 100}보다 높습니다",
                    )
                return asset
        raise HTTPException(400, f"지정한 릴리스 자산이 없습니다: {preferred}")
    cands = [a for a in assets if "x64" in a.get("name", "").lower()] or list(assets)
    if not cands:
        raise HTTPException(400, "Windows용 llama.cpp CUDA 릴리스 자산을 찾지 못했습니다")

    def score(a):
        cuda = _asset_cuda_ver(a["name"])
        compat = 1 if (driver_max is None or cuda <= driver_max) else 0
        return (compat, cuda)

    compatible = [a for a in cands if driver_max is None or _asset_cuda_ver(a["name"]) <= driver_max]
    if not compatible:
        maximum = f"{driver_max // 100}.{driver_max % 100}" if driver_max is not None else "알 수 없음"
        raise HTTPException(400, f"현재 드라이버 최대 CUDA {maximum}와 호환되는 Windows 빌드가 없습니다")
    compatible.sort(key=lambda a: (score(a), a["name"]), reverse=True)
    return compatible[0]


def _download_github_file(url, dest_path):
    """GitHub 릴리스 자산을 스트리밍 다운로드한다 (진행률 로그 포함)."""
    import requests

    _llama_install_log(f"다운로드: {url}")
    LLAMA_BUILD_STATE["message"] = f"다운로드 중: {os.path.basename(dest_path)}"
    with requests.get(url, stream=True, timeout=(20, 600)) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        last_pct = -1
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                pct = int(done * 100 / total) if total else 0
                if pct >= last_pct + 5:
                    last_pct = pct
                    _llama_install_log(f"다운로드 {pct}% ({done // (1024 * 1024)} MB)")


def _install_cudart_bundle(tag, main_fname, install_dir, tmp_dir):
    """Windows CUDA 빌드와 짝인 CUDA 런타임 DLL 번들(cudart-*.zip)을 함께 설치한다.

    공식 릴리스는 cudart64/cublas64 등 런타임 DLL을 메인 zip과 별도 zip으로 배포한다.
    이것 없이는 ggml-cuda.dll이 로딩되지 않아 에러 로그 없이 CPU 전용으로 동작하므로,
    CUDA 빌드라면 항상 함께 받아 같은 폴더에 조합 설치해야 한다.
    번들 설치 또는 검증 실패 시 staging 설치 전체를 폐기한다.
    """
    cuda_str = _asset_cuda_str(main_fname)
    if not cuda_str:
        return  # CPU 빌드 — cudart 번들 없음
    try:
        import platform

        arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
        LLAMA_BUILD_STATE["message"] = f"CUDA 런타임 DLL 다운로드 중 (cuda {cuda_str})"
        res = requests.get(f"{GITHUB_API}/releases/tags/{tag}", timeout=30)
        res.raise_for_status()
        pat = re.compile(r"^cudart-.*win-cuda-" + re.escape(cuda_str) + r".*\.zip$", re.I)
        asset = next(
            (a for a in res.json().get("assets", [])
             if pat.fullmatch(a.get("name", "")) and arch in a.get("name", "").lower()),
            None,
        )
        if not asset:
            msg = (f"CUDA {cuda_str}에 해당하는 cudart 번들을 릴리스에서 찾지 못했습니다 — "
                   f"CUDA 런타임 DLL이 없어 GPU 실행이 불가합니다")
            raise RuntimeError(msg)
        zip_path = os.path.join(tmp_dir, asset["name"])
        _download_github_file(asset.get("browser_download_url") or asset.get("download_url"), zip_path)

        LLAMA_BUILD_STATE["message"] = "CUDA 런타임 DLL 압축 해제 중"
        stage = os.path.join(tmp_dir, f"cudart-{tag}")
        if os.path.exists(stage):
            shutil.rmtree(stage, ignore_errors=True)
        os.makedirs(stage, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(stage)
        # 단일 최상위 폴더로 묶여 있으면 한 단계 끌어올린다.
        entries = [e for e in os.listdir(stage) if not e.startswith(".")]
        if len(entries) == 1 and os.path.isdir(os.path.join(stage, entries[0])):
            inner = os.path.join(stage, entries[0])
            for item in os.listdir(inner):
                shutil.move(os.path.join(inner, item), os.path.join(stage, item))
            os.rmdir(inner)
        n_files = 0
        for item in os.listdir(stage):
            dst = os.path.join(install_dir, item)
            if os.path.exists(dst):
                (shutil.rmtree if os.path.isdir(dst) else os.remove)(dst)
            shutil.move(os.path.join(stage, item), dst)
            n_files += 1
        _llama_install_log(f"CUDA 런타임 DLL 설치 완료: {n_files}개 파일 ({asset['name']})")
    except Exception as e:
        msg = f"CUDA 런타임 DLL 설치 실패: {e} — GPU 실행이 불가합니다 (logs/llama_install.log 확인)"
        _llama_install_log("오류: " + msg)
        raise RuntimeError(msg) from e


def _install_llama_windows(tag, asset_name=""):
    """GitHub 릴리스의 Windows CUDA 프리빌트 zip을 내려받아 <root>/llama-<tag>에 설치한다."""
    LLAMA_BUILD_STATE.update(busy=True, tag=tag, message="시작", started_at=time.time())
    tmp_dir = os.path.join(BASE_DIR, "logs", "llama_install_tmp")
    install_dir = None
    stage_dir = None
    backup_dir = None
    try:
        import requests

        _llama_install_log(f"===== llama.cpp {tag} Windows 설치 시작 =====")
        LLAMA_BUILD_STATE["message"] = "릴리스 정보 조회"
        assets = _fetch_release_assets(tag)
        if not asset_name and not assets:
            # 프리빌트 바이너리가 없는 태그(예: stable 릴리스) → git 클론 + CUDA 빌드
            _llama_install_log(f"{tag} 릴리스에 Windows 프리빌트가 없어 git 빌드로 전환")
            LLAMA_BUILD_STATE["message"] = "프리빌트 없음 — git 빌드로 전환"
            _build_llama_git_windows(tag)
            return
        asset = _pick_windows_asset(assets, asset_name, driver_max=_driver_max_cuda())
        url = asset.get("url", "")
        fname = asset.get("name", f"llama-{tag}.zip")
        if not url or not fname.lower().endswith(".zip"):
            raise RuntimeError(f"다운로드 가능한 zip 자산을 찾지 못했습니다: {fname}")

        os.makedirs(tmp_dir, exist_ok=True)
        zip_path = os.path.join(tmp_dir, fname)
        _llama_install_log(f"다운로드: {url}")
        LLAMA_BUILD_STATE["message"] = f"다운로드 중: {fname}"
        with requests.get(url, stream=True, timeout=(20, 300)) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            last_pct = -1
            with open(zip_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    pct = int(done * 100 / total) if total else 0
                    if pct >= last_pct + 5:
                        last_pct = pct
                        _llama_install_log(f"다운로드 {pct}% ({done // (1024 * 1024)} MB)")

        # CUDA 버전별로 별도 폴더에 설치한다. staging 폴더는 최종 폴더와
        # 같은 볼륨에 만들어 검증 후 디렉터리 rename으로 안전하게 교체한다.
        cuda_str = _asset_cuda_str(fname)
        suffix = f"-cuda{cuda_str}" if cuda_str else ""
        install_dir = os.path.join(settings.get("llama_install_root"), f"llama-{tag}{suffix}")
        os.makedirs(os.path.dirname(install_dir), exist_ok=True)
        unique = f"{os.getpid()}-{threading.get_ident()}-{int(time.time() * 1000)}"
        stage_dir = f"{install_dir}.installing-{unique}"
        backup_dir = f"{install_dir}.backup-{unique}"
        os.makedirs(stage_dir)

        LLAMA_BUILD_STATE["message"] = "압축 해제 중"
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(stage_dir)

        # zip이 단일 최상위 폴더로 묶여 있으면 그 내용을 한 단계 끌어올린다.
        entries = [e for e in os.listdir(stage_dir) if not e.startswith(".")]
        if len(entries) == 1 and os.path.isdir(os.path.join(stage_dir, entries[0])):
            inner = os.path.join(stage_dir, entries[0])
            for item in os.listdir(inner):
                shutil.move(os.path.join(inner, item), os.path.join(stage_dir, item))
            os.rmdir(inner)

        if not _llama_server_exe(stage_dir):
            raise RuntimeError("압축 파일 안에 llama-server 실행 파일을 찾지 못했습니다")

        # CUDA 런타임 DLL(cudart64/cublas64 등)은 공식 릴리스에서 별도 zip으로 배포된다.
        # 이것을 함께 설치하지 않으면 ggml-cuda.dll이 로딩돼지 않아 에러 로그 없이
        # CPU 전용으로만 동작하므로, CUDA 빌드라면 항상 조합 설치한다.
        _install_cudart_bundle(tag, fname, stage_dir, tmp_dir)

        installed_exe = _llama_server_exe(stage_dir)
        verify = subprocess.run(
            [installed_exe, "--version"], capture_output=True, text=True,
            timeout=30, creationflags=NO_WINDOW,
        )
        if verify.returncode:
            raise RuntimeError("설치된 llama-server와 CUDA DLL 로딩 검증 실패: " + (verify.stderr.strip() or verify.stdout.strip()))

        LLAMA_BUILD_STATE["message"] = "검증 완료 — 기존 설치 교체 중"
        if os.path.exists(install_dir):
            os.replace(install_dir, backup_dir)
        try:
            os.replace(stage_dir, install_dir)
            stage_dir = None
        except Exception:
            if backup_dir and os.path.exists(backup_dir) and not os.path.exists(install_dir):
                os.replace(backup_dir, install_dir)
                backup_dir = None
            raise
        if backup_dir and os.path.exists(backup_dir):
            shutil.rmtree(backup_dir, ignore_errors=True)
            backup_dir = None

        _llama_install_log(f"설치 완료: {install_dir}")
        LLAMA_BUILD_STATE["message"] = f"설치 완료: {install_dir}"
    except Exception as e:
        if stage_dir and os.path.isdir(stage_dir):
            shutil.rmtree(stage_dir, ignore_errors=True)
        if backup_dir and os.path.exists(backup_dir) and install_dir and not os.path.exists(install_dir):
            try:
                os.replace(backup_dir, install_dir)
                backup_dir = None
            except OSError as restore_error:
                _llama_install_log(f"기존 설치 자동 복구 실패: {restore_error} (백업: {backup_dir})")
        _llama_install_log(f"설치 실패: {e}")
        LLAMA_BUILD_STATE["message"] = f"실패: {e}"
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except OSError:
            pass
        LLAMA_BUILD_STATE["busy"] = False
        scan_llama_versions()


@app.post("/api/llama/install")
def llama_install(tag: str = "", asset: str = ""):
    tag = _safe_tag(tag)
    if LLAMA_BUILD_STATE["busy"]:
        raise HTTPException(409, "이미 빌드가 진행 중입니다.")
    # Mark the request as busy before starting the worker.  Otherwise the UI's
    # first status poll can race the thread and incorrectly conclude that the
    # installation already finished.
    LLAMA_BUILD_STATE.update(busy=True, tag=tag, message="설치 대기 중", started_at=time.time())
    if IS_WINDOWS:
        threading.Thread(target=_install_llama_windows, args=(tag, asset), daemon=True).start()
    else:
        threading.Thread(target=_build_llama_git, args=(tag,), daemon=True).start()
    return {"ok": True, "tag": tag, "state": LLAMA_BUILD_STATE}


@app.get("/api/llama/install/status")
def llama_install_status():
    log = os.path.join(BASE_DIR, "logs", "llama_install.log")
    lines = tail(log, 100) if os.path.exists(log) else []
    return {"state": LLAMA_BUILD_STATE, "log": lines}


@app.post("/api/llama/models/download")
def llama_model_download(url: str = "", name: str = ""):
    root = load_llama_settings()["model_root"]
    if not url:
        raise HTTPException(400, "URL이 없습니다")
    filename = (name or os.path.basename(url.split("?")[0])).strip()
    if not filename or filename in {".", ".."} or os.path.sep in filename:
        raise HTTPException(400, "잘못된 파일 이름입니다")
    dest = os.path.join(root, filename)
    if os.path.exists(dest):
        raise HTTPException(400, f"이미 존재합니다: {filename}")
    os.makedirs(root, exist_ok=True)

    def _run():
        try:
            import requests

            with requests.get(url, stream=True, timeout=(15, None)) as resp:
                resp.raise_for_status()
                tmp = dest + ".part"
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                os.replace(tmp, dest)
        except Exception:
            try:
                os.remove(dest + ".part")
            except OSError:
                pass
            raise

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "path": dest, "url": url}


# ---------- ComfyUI ----------

@app.post("/api/comfy/start")
def comfy_start(data: dict | None = None):
    if data:
        saved = save_comfy_settings(data)
    else:
        saved = save_comfy_settings({})
    comfy_dir = settings.get("comfyui_dir")
    if not comfy_dir or not os.path.isdir(comfy_dir):
        raise HTTPException(400, f"ComfyUI 폴더가 없습니다: {comfy_dir}")
    try:
        model_config, resolved_model_root = ensure_model_config(
            comfy_dir, settings.get("comfyui_model_root"), try_mount=True,
        )
    except ModelPathError as error:
        raise HTTPException(400, str(error)) from error
    settings.save({"comfyui_model_root": str(resolved_model_root)})
    cmd = [
        _comfy_python(),
        "main.py",
        "--port",
        str(settings.get("comfyui_port")),
        "--extra-model-paths-config",
        str(model_config),
    ]
    if saved.get("listen"):
        cmd += ["--listen", "0.0.0.0"]
    cmd += _comfy_args()
    devices = normalize_gpu_devices([saved.get("gpu_device")]) if saved.get("gpu_device") else []
    pid = services["comfyui"].start(cmd, cwd=comfy_dir, env=_comfy_env(), device=devices or None)
    return {"ok": True, "pid": pid, "cmd": cmd, "gpu_devices": devices}


@app.post("/api/comfy/stop")
def comfy_stop(request: Request):
    _require_comfy_confirmation(request)
    return {"ok": services["comfyui"].stop()}


@app.get("/api/comfy/settings")
def comfy_settings_get():
    return save_comfy_settings({})


@app.post("/api/comfy/settings")
def comfy_settings_save(data: dict):
    return save_comfy_settings(data)


# ---------- 봇 / 와처 ----------

@app.post("/api/bot/start")
def bot_start(device: str = ""):
    d = settings.get("bot_dir")
    if not d or not os.path.isdir(d):
        raise HTTPException(400, f"봇 폴더가 없습니다: {d}")
    py = _run_dir_python(d)
    pid = services["bot"].start([py, "main.py"], cwd=d, device=device or None)
    return {"ok": True, "pid": pid}


@app.post("/api/bot/stop")
def bot_stop():
    return {"ok": services["bot"].stop()}


@app.post("/api/watcher/start")
def watcher_start(device: str = ""):
    d = settings.get("watcher_dir")
    if not d or not os.path.isdir(d):
        raise HTTPException(400, f"와처 폴더가 없습니다: {d}")
    py = _run_dir_python(d)
    pid = services["watcher"].start([py, "main.py"], cwd=d, device=device or None)
    return {"ok": True, "pid": pid}


@app.post("/api/watcher/stop")
def watcher_stop():
    return {"ok": services["watcher"].stop()}


# ---------- Unsloth Studio (headless web server) ----------

@app.post("/api/unsloth/start")
def unsloth_start():
    executable = _unsloth_executable()
    if not executable:
        raise HTTPException(400, f"Unsloth CLI 실행 파일이 없습니다: {settings.get('unsloth_executable')}")
    port = _unsloth_port()
    cmd = [
        executable, "studio", "--host", "0.0.0.0", "--port", str(port),
        "--no-cloudflare",
    ]
    pid = services["unsloth"].start(
        cmd,
        cwd=os.path.expanduser("~"),
        env={"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not services["unsloth"].running():
            detail = "\n".join(tail(services["unsloth"].log_file, 30))
            raise HTTPException(500, detail or "Unsloth Studio가 시작 직후 종료되었습니다")
        try:
            response = requests.get(f"http://127.0.0.1:{port}/api/health", timeout=0.5)
            if response.status_code < 500:
                return {"ok": True, "pid": pid, "port": port, "headless": True}
        except requests.RequestException:
            pass
        time.sleep(0.25)
    return {"ok": True, "pid": pid, "port": port, "headless": True, "starting": True}


@app.post("/api/unsloth/stop")
def unsloth_stop(request: Request):
    if request.headers.get("X-Unsloth-Stop-Confirm") != "confirmed":
        raise HTTPException(409, "Unsloth 작업 종료 확인이 필요합니다")
    return {"ok": services["unsloth"].stop()}


# ---------- 메모 / 로그 / 프리셋 ----------

@app.get("/api/memo")
def memo_get():
    try:
        with open(MEMO_FILE, "r", encoding="utf-8") as f:
            return {"memo": f.read()}
    except FileNotFoundError:
        return {"memo": ""}


@app.post("/api/memo")
def memo_save(memo: str = ""):
    with open(MEMO_FILE, "w", encoding="utf-8") as f:
        f.write(memo)
    return {"ok": True}


@app.get("/api/logs")
def logs_list():
    logs = []
    for f in sorted(glob.glob(os.path.join(BASE_DIR, "logs", "*.log"))):
        logs.append(
            {
                "name": os.path.basename(f)[:-4],
                "path": f,
                "size": os.path.getsize(f),
                "mtime": int(os.path.getmtime(f)),
            }
        )
    return {"logs": logs}


@app.get("/api/logs/{name}")
def logs_read(name: str, lines: int = 300):
    safe = os.path.basename(name)
    path = os.path.join(BASE_DIR, "logs", f"{safe}.log")
    if not os.path.exists(path):
        return {"lines": []}
    return {"lines": tail(path, min(max(lines, 10), 2000))}


@app.post("/api/clear_log/{name}")
def clear_log(name: str):
    safe = os.path.basename(name)
    path = os.path.join(BASE_DIR, "logs", f"{safe}.log")
    if os.path.exists(path):
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("")
        except OSError:
            return {"ok": False}
    return {"ok": True}


@app.get("/api/last_run")
def last_run_get():
    return _read_json(LAST_RUN_FILE, {})


@app.post("/api/clear_hw_history")
def clear_hw_history():
    STATE["history"] = []
    try:
        _write_json(HW_HISTORY_FILE, [])
    except OSError:
        pass
    return {"ok": True}


@app.get("/api/presets")
def presets_get():
    return {"presets": load_presets()}


@app.post("/api/presets")
def presets_save(presets: dict):
    save_presets(presets)
    return {"ok": True}


# ---------- 설정 / 관리 ----------

MANAGER_TASK_NAME = "MainServer"


def _windows_manager_service():
    """Windows manager 자동 시작 상태.

    HKCU Run의 숨김 Python supervisor를 사용합니다.
    Linux의 systemd user service(linger)와 동일한 역할입니다.
    """
    try:
        import autostart_service

        state = autostart_service.status()
    except (OSError, subprocess.SubprocessError):
        state = {"exists": False, "registry": False, "task_scheduler": False}
    return {
        "enabled": bool(state.get("exists")),
        "unit": f"HKCU Run:\\{MANAGER_TASK_NAME}",
        "label": "Windows 자동 시작",
        "registry": bool(state.get("registry")),
        "task_scheduler": bool(state.get("task_scheduler")),
    }


def _manager_service_status():
    if IS_WINDOWS:
        return _windows_manager_service()
    enabled = subprocess.run(
        ["systemctl", "--user", "is-enabled", "main_server.service"],
        capture_output=True, text=True,
    ).returncode == 0
    return {"enabled": enabled, "unit": "main_server.service", "label": "systemd"}


@app.get("/api/linux-settings")
def linux_settings_get():
    return {
        "platform": "windows" if IS_WINDOWS else "linux",
        "settings": settings.all(),
        "effective": settings.all(),
        "manager_service": _manager_service_status(),
    }


@app.post("/api/linux-settings")
def linux_settings_save(data: dict):
    # autostart 토글 시 Task Scheduler 등록/해제를 자동 수행 (별도 버튼 불필요)
    if IS_WINDOWS:
        was_autostart = settings.get("autostart", False)
        now_autostart = data.get("autostart", was_autostart)
        if now_autostart and not was_autostart:
            import autostart_service
            try:
                autostart_service.register()
            except Exception as e:
                print(f"[autostart] 자동 등록 실패: {e}")
        elif not now_autostart and was_autostart:
            import autostart_service
            try:
                autostart_service.unregister()
            except Exception as e:
                print(f"[autostart] 자동 해제 실패: {e}")

    effective = settings.save(data)
    scan_llama_versions()
    scan_gguf_models()
    return {"ok": True, "effective": effective, "manager_service": _manager_service_status()}


GPU_CONTROL_HELPER = "/usr/local/sbin/main-server-gpu-control"
GPU_TUNE_CONFIG_DIR = "/etc/main-server/gpu-tune.d"
# Windows는 PawnIO 관리자 helper를 통해 nvidia-smi를 실행하고, 영속 설정을
# main_server 루트의 JSON 파일에 둡니다. manager 시작 시 다시 적용해
# Linux gpu-tune.service와 같은 부팅 유지 동작을 제공합니다.
GPU_TUNE_SETTINGS_FILE = os.path.join(BASE_DIR, "gpu_tune_settings.json")
_gpu_tuning_apply_lock = threading.Lock()
_gpu_tuning_persistence = {
    "status": "idle", "applied": 0, "errors": [], "updated_at": None,
}


def _load_configured_gpu_tuning():
    """UUID별 저장된 GPU 튜닝 설정 (Linux: /etc/main-server/gpu-tune.d, Windows: gpu_tune_settings.json)."""
    configured_by_uuid = {}
    if IS_WINDOWS:
        data = _read_json(GPU_TUNE_SETTINGS_FILE, {})
        if isinstance(data, dict):
            for uuid_key, values in data.items():
                if isinstance(values, dict) and str(uuid_key).startswith("GPU-"):
                    configured_by_uuid[str(uuid_key)] = {
                        "TUNING_ENABLED": int(bool(values.get("enabled", True))),
                        "POWER_LIMIT": values.get("power_limit"),
                        "CLOCK_MAX": values.get("clock_max"),
                        "FAN_PERCENT": values.get("fan_percent"),
                        "FAN_AUTO": int(bool(values.get("fan_auto", False))),
                        "CLOCK_AUTO": int(bool(values.get("clock_auto", False))),
                    }
        return configured_by_uuid
    for path in Path(GPU_TUNE_CONFIG_DIR).glob("GPU-*.conf"):
        values = _read_gpu_tuning_config(path)
        if values.get("GPU_UUID"):
            configured_by_uuid[values["GPU_UUID"]] = values
    return configured_by_uuid


def _save_gpu_tuning_windows(uuid_key, enabled, power_limit=None, clock_max=None, fan_percent=None):
    data = _read_json(GPU_TUNE_SETTINGS_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data[uuid_key] = {
        "enabled": bool(enabled),
        "power_limit": power_limit,
        "clock_max": clock_max,
        "fan_percent": fan_percent or 0,
        "fan_auto": not fan_percent,
        "clock_auto": False,
    }
    _write_json(GPU_TUNE_SETTINGS_FILE, data)


def _run_nvidia_smi(args):
    try:
        result = subprocess.run(
            ["nvidia-smi"] + list(args),
            capture_output=True, text=True, timeout=20,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise HTTPException(503, f"nvidia-smi 실행 실패: {error}") from error
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        # 일부 드라이버/세션에서는 관리자 권한이 필요한 튜닝 명령을 거부합니다.
        raise HTTPException(400, detail or "GPU 설정 적용 실패")
    return result.stdout.strip()


def _gpu_index_by_uuid(uuid_key):
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"],
        capture_output=True, text=True, timeout=8,
        creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
    ).stdout
    for line in out.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) == 2 and parts[1] == uuid_key:
            return parts[0]
    raise HTTPException(400, f"GPU UUID를 찾지 못했습니다: {uuid_key}")


def _gpu_tuning_apply_values(uuid_key, power, clock, fan):
    """nvidia-smi로 전력/클럭/팬 값을 실제 GPU에 적용한다. 팬은 미지원 시 경고만 남긴다."""
    if IS_WINDOWS:
        try:
            response = motherboard_fan_controller.gpu_tune(
                uuid_key, int(power), int(clock), int(fan or 0)
            )
        except Exception as error:
            raise HTTPException(400, str(error)) from error
        return list(response.get("warnings") or [])
    warnings = []
    index = _gpu_index_by_uuid(uuid_key)
    i_args = ["-i", index]
    if power:
        _run_nvidia_smi(i_args + ["-pl", str(int(power))])
    if clock:
        _run_nvidia_smi(i_args + ["-lgc", f"{int(clock)},{int(clock)}"])
    if fan and int(fan) > 0:
        try:
            _run_nvidia_smi(i_args + ["--fan", str(int(fan))])
        except HTTPException as exc:
            warnings.append(f"팬 설정은 이 GPU/드라이버에서 적용되지 않았습니다: {exc.detail}")
    return warnings


def _gpu_tuning_set_windows(uuid_key, action, power=None, clock=None, fan=None):
    if action == "set":
        if not power or not clock:
            raise HTTPException(400, "power_limit과 clock_max가 필요합니다")
        warnings = _gpu_tuning_apply_values(uuid_key, power, clock, fan)
        _save_gpu_tuning_windows(uuid_key, enabled=True, power_limit=power, clock_max=clock, fan_percent=fan or 0)
    elif action == "reset":
        snapshot = _gpu_tuning_snapshot()
        target = next((g for g in snapshot["gpus"] if g["uuid"] == uuid_key), None)
        if not target:
            raise HTTPException(400, f"GPU UUID를 찾지 못했습니다: {uuid_key}")
        recommended = target.get("recommended") or {}
        power = recommended.get("power_limit") or target.get("power_default")
        clock = recommended.get("clock_max") or target.get("clock_hardware_max")
        fan = 0
        warnings = _gpu_tuning_apply_values(uuid_key, power, clock, fan)
        _save_gpu_tuning_windows(uuid_key, enabled=True, power_limit=power, clock_max=clock, fan_percent=0)
    elif action == "disable":
        data = _read_json(GPU_TUNE_SETTINGS_FILE, {})
        entry = data.get(uuid_key) if isinstance(data, dict) else None
        if not isinstance(entry, dict):
            entry = {"power_limit": power, "clock_max": clock, "fan_percent": fan or 0}
        entry["enabled"] = False
        _save_gpu_tuning_windows(uuid_key, enabled=False,
                                 power_limit=entry.get("power_limit"),
                                 clock_max=entry.get("clock_max"),
                                 fan_percent=entry.get("fan_percent"))
        warnings = []
    else:
        raise HTTPException(400, "지원하지 않는 작업입니다")
    time.sleep(0.3)
    snapshot = _gpu_tuning_snapshot()
    message = "; ".join(warnings) if warnings else "GPU 설정이 적용되었습니다"
    snapshot.update({"ok": True, "message": message})
    return snapshot


def _apply_saved_gpu_tuning():
    """manager 시작 시 저장된 Windows GPU 튜닝을 다시 적용 (부팅 유지 상당)."""
    if not IS_WINDOWS:
        return True
    if not settings.get("gpu_tuning_enabled", True):
        print("[gpu-tune] 'GPU 튜닝 사용'이 꺼져 있어 저장 설정 재적용을 건너뜁니다")
        _gpu_tuning_persistence.update({
            "status": "disabled", "applied": 0, "errors": [], "updated_at": time.time(),
        })
        return True
    data = _read_json(GPU_TUNE_SETTINGS_FILE, {})
    if not isinstance(data, dict):
        return True
    errors = []
    applied = 0
    with _gpu_tuning_apply_lock:
        for uuid_key, entry in data.items():
            if not isinstance(entry, dict) or not entry.get("enabled"):
                continue
            try:
                _gpu_tuning_apply_values(
                    uuid_key, entry.get("power_limit"), entry.get("clock_max"), entry.get("fan_percent")
                )
                applied += 1
            except Exception as error:
                message = f"{uuid_key}: {error}"
                errors.append(message)
                print(f"[gpu-tune] 저장 설정 적용 실패: {message}")
    _gpu_tuning_persistence.update({
        "status": "ready" if not errors else "retrying",
        "applied": applied, "errors": errors, "updated_at": time.time(),
    })
    if not errors and applied:
        print(f"[gpu-tune] 저장 설정 {applied}개 부팅 재적용 완료")
    return not errors


def _retry_saved_gpu_tuning():
    """Retry while the NVIDIA driver/elevated helper finishes booting."""
    for delay in (5, 10, 20, 40, 60):
        time.sleep(delay)
        if _apply_saved_gpu_tuning():
            return True
    _gpu_tuning_persistence["status"] = "error"
    return False


def _read_gpu_tuning_config(path):
    values = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip("'\"")
            if key.strip() == "GPU_UUID":
                values[key.strip()] = value
            else:
                values[key.strip()] = int(value)
    except (OSError, ValueError):
        return {}
    return values


def _gpu_tuning_snapshot():
    fields = (
        "index,uuid,name,pci.device_id,power.draw,power.limit,power.default_limit,power.min_limit,"
        "power.max_limit,clocks.current.graphics,clocks.max.graphics,clocks.current.memory,"
        "clocks.max.memory,fan.speed"
    )
    result = subprocess.run(
        ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=8,
        creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
    )
    if result.returncode:
        raise HTTPException(503, result.stderr.strip() or "NVIDIA GPU 정보를 읽지 못했습니다")
    configured_by_uuid = _load_configured_gpu_tuning()
    def number(value, integer=False):
        value = value.strip()
        if value in {"", "N/A", "[N/A]"}:
            return None
        return int(float(value)) if integer else round(float(value), 1)

    gpus = []
    for line in result.stdout.strip().splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 14:
            continue
        configured = configured_by_uuid.get(parts[1])
        # Never reuse the old index-based config: inserting or reordering a card
        # could otherwise apply another GPU's power limit to this UUID.
        pci_device_id = parts[3].lower()
        is_cmp_170hx = pci_device_id.startswith(("0x20c2", "0x2082"))
        recommended_power = 250 if is_cmp_170hx else number(parts[6], True)
        recommended_clock = 1410 if is_cmp_170hx else number(parts[10], True)
        gpu = {
            "index": int(parts[0]), "uuid": parts[1], "name": parts[2],
            "pci_device_id": pci_device_id, "profile": "cmp_170hx" if is_cmp_170hx else "generic",
            "power_draw": number(parts[4]), "power_limit": number(parts[5]),
            "power_default": number(parts[6]), "power_min": number(parts[7]),
            "power_max": number(parts[8]), "clock_current": number(parts[9], True),
            "clock_hardware_max": number(parts[10], True),
            "memory_clock_current": number(parts[11], True),
            "memory_clock_max": number(parts[12], True), "fan_speed": number(parts[13], True),
            "fan_supported": number(parts[13], True) is not None, "configured": None,
            "recommended": {
                "power_limit": recommended_power, "clock_max": recommended_clock,
                "fan_percent": 0,
            },
        }
        if configured:
            gpu["configured"] = {
                "enabled": bool(configured.get("TUNING_ENABLED", 1)),
                "power_limit": configured.get("POWER_LIMIT"),
                "clock_max": configured.get("CLOCK_MAX"),
                "fan_percent": configured.get("FAN_PERCENT"),
                "fan_auto": bool(configured.get("FAN_AUTO", 0)),
                "clock_auto": bool(configured.get("CLOCK_AUTO", 0)),
            }
        gpus.append(gpu)
    return {
        "available": bool(gpus), "gpus": gpus,
        "service": "manager-autostart" if IS_WINDOWS else "gpu-tune.service",
        "tuning_mode": "power_and_clock_cap", "voltage_target_supported": False,
        "persistence": dict(_gpu_tuning_persistence) if IS_WINDOWS else None,
    }


@app.get("/api/gpu/tuning")
def gpu_tuning_get():
    return _gpu_tuning_snapshot()


@app.post("/api/gpu/tuning")
def gpu_tuning_set(data: dict):
    if not IS_WINDOWS and not os.path.isfile(GPU_CONTROL_HELPER):
        raise HTTPException(503, "GPU 제어 helper가 설치되지 않았습니다")
    action = str(data.get("action") or "set")
    # 마스터 토글이 꺼져 있으면 값 쓰기(set/reset)를 차단 (disable은 허용)
    if action in ("set", "reset") and not settings.get("gpu_tuning_enabled", True):
        raise HTTPException(400, "GPU 튜닝이 비활성화되어 있습니다. 'GPU 전력 / 코어 클럭 / 팬' 섹션의 'GPU 튜닝 사용' 토글을 켜세요")
    try:
        gpu_selector = str(data.get("gpu_uuid") or data.get("gpu_index", 0))
        if not (gpu_selector.isdigit() or re.fullmatch(r"GPU-[A-Za-z0-9-]+", gpu_selector)):
            raise ValueError("GPU 식별자가 올바르지 않습니다")
        power = int(data["power_limit"]) if action == "set" else None
        clock = int(data["clock_max"]) if action == "set" else None
        fan = int(data["fan_percent"]) if action == "set" else None
        if IS_WINDOWS and fan is not None and fan != 0 and not 20 <= fan <= 100:
            raise ValueError("GPU 팬은 자동(0) 또는 20~100%여야 합니다")
        if not IS_WINDOWS and fan not in (None, 0):
            raise ValueError("Linux에서는 GPU 자체 팬을 제어하지 않습니다. 메인보드 PWM 팬 제어를 사용하세요")
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(400, f"GPU 설정값이 올바르지 않습니다: {error}") from error

    if IS_WINDOWS:
        uuid_key = gpu_selector
        if uuid_key.isdigit():
            # 인덱스로 왔으면 UUID로 바꾼다 (UUID 기반 저장 정책 유지)
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=8,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            ).stdout
            uuid_key = next(
                (line.split(",")[1].strip() for line in out.splitlines()
                 if line.strip().split(",")[0].strip() == uuid_key),
                None,
            )
            if not uuid_key:
                raise HTTPException(400, f"GPU 인덱스를 찾지 못했습니다: {gpu_selector}")
        return _gpu_tuning_set_windows(uuid_key, action, power, clock, fan)

    try:
        if action == "reset":
            command = ["sudo", "-n", GPU_CONTROL_HELPER, "reset", gpu_selector]
        elif action == "disable":
            command = ["sudo", "-n", GPU_CONTROL_HELPER, "disable", gpu_selector]
        elif action == "set":
            command = ["sudo", "-n", GPU_CONTROL_HELPER, "set", gpu_selector, str(power), str(clock), str(fan)]
        else:
            raise ValueError("지원하지 않는 작업입니다")
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise HTTPException(400, result.stderr.strip() or result.stdout.strip() or "GPU 설정 적용 실패")
    time.sleep(0.3)
    snapshot = _gpu_tuning_snapshot()
    snapshot.update({"ok": True, "message": result.stdout.strip()})
    return snapshot


COMFY_CONFIRM_HEADER = "x-comfyui-stop-confirm"


def _require_comfy_confirmation(request: Request):
    if request.headers.get(COMFY_CONFIRM_HEADER, "").lower() != "confirmed":
        raise HTTPException(409, "ComfyUI 종료 확인이 필요합니다")


@app.post("/api/panic")
def panic(request: Request):
    if _service_state("comfyui")["running"]:
        _require_comfy_confirmation(request)
    results = {}
    for name, svc in services.items():
        results[name] = svc.stop()
    return {"ok": True, "results": results}


@app.post("/api/force_kill/{server}")
def force_kill(server: str, request: Request):
    if server not in services:
        raise HTTPException(404, f"알 수 없는 서비스: {server}")
    if server == "comfyui":
        _require_comfy_confirmation(request)
    return {"ok": services[server].force_kill()}


def _folder_for(target):
    folders = {
        "model_root": load_llama_settings()["model_root"],
        "comfyui": settings.get("comfyui_dir"),
        "comfy_output": os.path.join(settings.get("comfyui_dir"), "output"),
        "bot": settings.get("bot_dir"),
        "watcher": settings.get("watcher_dir"),
        "unsloth": os.path.expanduser("~/.unsloth"),
        "logs": os.path.join(BASE_DIR, "logs"),
        "vllm": settings.get("vllm_env"),
        "base": BASE_DIR,
    }
    if target not in folders:
        raise HTTPException(404, f"알 수 없는 대상: {target}")
    path = folders[target]
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
    return path


@app.post("/api/open_folder/{target}")
def open_folder(target: str):
    path = _folder_for(target)
    if os.name == "nt":
        os.startfile(path)
    else:
        subprocess.Popen(
            ["xdg-open", path], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    return {"ok": True, "path": path}


@app.post("/api/open_terminal/{target}")
def open_terminal(target: str):
    path = _folder_for(target)
    if os.name == "nt":
        subprocess.Popen(f'start cmd /K "cd /d {path}"', shell=True)
        return {"ok": True, "path": path}
    for term in ("gnome-terminal", "konsole", "xfce4-terminal", "x-terminal-emulator", "xterm"):
        if not shutil.which(term):
            continue
        try:
            if term in ("gnome-terminal",):
                subprocess.Popen([term, "--", "bash", "-lc", f"cd {path} && exec bash"], start_new_session=True)
            elif term == "konsole":
                subprocess.Popen([term, "--workdir", path], start_new_session=True)
            else:
                subprocess.Popen([term, "-e", "bash", "-lc", f"cd {path} && exec bash"], start_new_session=True)
            return {"ok": True, "path": path}
        except Exception:
            continue
    return {"ok": False, "detail": "사용 가능한 터미널 에뮬레이터가 없습니다"}


@app.post("/api/restart")
def restart():
    threading.Thread(target=_restart_now, daemon=True).start()
    return {"ok": True}


def _update_manager_git():
    if not os.path.isdir(os.path.join(BASE_DIR, ".git")):
        raise HTTPException(400, "현재 main_server 폴더는 Git 저장소가 아닙니다")

    def git(*args, timeout=180):
        return subprocess.run(
            ["git", *args], cwd=BASE_DIR, capture_output=True, text=True,
            errors="replace", timeout=timeout, creationflags=NO_WINDOW,
        )

    dirty = git("status", "--porcelain", timeout=20)
    if dirty.returncode:
        raise HTTPException(500, dirty.stderr.strip() or "Git 상태 확인 실패")
    if dirty.stdout.strip():
        raise HTTPException(409, "커밋하지 않은 로컬 변경이 있어 업데이트를 중단했습니다")
    branch = git("branch", "--show-current", timeout=20).stdout.strip()
    if not branch:
        raise HTTPException(409, "detached HEAD에서는 자동 업데이트할 수 없습니다")
    before = git("rev-parse", "--short", "HEAD", timeout=20).stdout.strip()
    fetch = git("fetch", "--prune", "origin", branch)
    if fetch.returncode:
        raise HTTPException(500, fetch.stderr.strip() or fetch.stdout.strip() or "Git fetch 실패")
    merge = git("merge", "--ff-only", f"origin/{branch}")
    if merge.returncode:
        raise HTTPException(409, merge.stderr.strip() or merge.stdout.strip() or "fast-forward 업데이트 실패")
    after = git("rev-parse", "--short", "HEAD", timeout=20).stdout.strip()
    return {
        "ok": True, "updated": before != after, "before": before, "after": after,
        "message": (merge.stdout or "").strip() or "이미 최신 버전입니다",
    }


@app.post("/api/manager/update")
def update_manager(request: Request):
    if request.headers.get("X-Manager-Update-Confirm") != "confirmed":
        raise HTTPException(409, "main_server 업데이트 확인 헤더가 필요합니다")
    if not _manager_update_lock.acquire(blocking=False):
        raise HTTPException(409, "main_server 업데이트가 이미 진행 중입니다")
    try:
        return _update_manager_git()
    finally:
        _manager_update_lock.release()


@app.post("/api/manager/stop")
def stop_manager(request: Request):
    if request.headers.get("X-Manager-Stop-Confirm") != "confirmed":
        raise HTTPException(409, "main_server 종료 확인 헤더가 필요합니다")
    threading.Thread(target=_stop_manager_now, daemon=True).start()
    return {"ok": True, "stopping": True}


def _stop_manager_now():
    time.sleep(1.0)
    if IS_WINDOWS:
        try:
            Path(BASE_DIR, ".manager-stop").write_text("stopped from web UI\n", encoding="utf-8")
        finally:
            os._exit(0)
    result = subprocess.run(
        [
            "systemd-run", "--user", "--quiet", "--collect",
            "--unit=main-server-manager-stop", "--on-active=1s",
            "systemctl", "--user", "stop", "main_server.service",
        ],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode:
        print(f"[manager stop] {result.stderr.strip() or result.stdout.strip()}")


def _restart_now():
    if IS_WINDOWS:
        # systemd가 없으므로 분리 프로세스(manager_restarter.py)가 포트 8999가
        # 비어질 때까지 대기한 뒤 manager를 다시 띄웁니다. start_manager.bat
        # 감독 루프가 동시에 재시작해도 포트 선점 경쟁에서 지는 쪽은 즉시 종료되어
        # 중복/orphan 프로세스가 남지 않습니다.
        helper = os.path.join(BASE_DIR, "manager_restarter.py")
        try:
            subprocess.Popen(
                [sys.executable, helper],
                cwd=BASE_DIR,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=(
                    getattr(subprocess, "DETACHED_PROCESS", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                ),
            )
        except Exception as error:
            print(f"[restart] restarter spawn 실패: {error}")
    time.sleep(0.5)
    if not IS_WINDOWS:
        # systemd의 Restart=always가 새 manager를 하나만 기동합니다. 여기서 직접
        # app.py를 spawn하면 systemd 재기동과 경합해 8999를 점유하는 orphan이 생깁니다.
        pass
    os._exit(0)


# ---------- 데스크톱 GUI/CLI 모드 전환 ----------

DESKTOP_TOGGLE_HELPER = "/usr/local/sbin/main-server-desktop-toggle"


def _desktop_toggle(args, timeout=120):
    if IS_WINDOWS:
        raise HTTPException(400, "데스크톱 모드 전환은 Linux에서만 지원합니다")
    if not os.path.exists(DESKTOP_TOGGLE_HELPER):
        raise HTTPException(500, f"헬퍼가 없습니다: {DESKTOP_TOGGLE_HELPER}")
    try:
        proc = subprocess.run(
            ["sudo", "-n", DESKTOP_TOGGLE_HELPER] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "모드 전환 시간이 초과되었습니다")
    except Exception as exc:
        raise HTTPException(500, f"모드 전환 실행 실패: {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or "모드 전환 실패"
        raise HTTPException(500, detail)
    return proc.stdout.strip()


@app.post("/api/desktop/mode")
def desktop_mode_set(payload: dict = None):
    mode = (payload or {}).get("mode")
    if mode not in ("gui", "cli"):
        raise HTTPException(400, "mode는 'gui' 또는 'cli'여야 합니다")
    new_mode = _desktop_toggle(["set", mode])
    return {"ok": True, "mode": new_mode}


# ---------- 시작 ----------

def _ensure_files():
    if not os.path.exists(PRESETS_FILE):
        root = settings.get("model_root")
        save_presets(
            {
                "기본 (auto)": {
                    "version": "",
                    "model": os.path.join(root, "MODEL.gguf") if root else "",
                    "mmproj": "",
                    "ctx": "32768",
                    "host": "0.0.0.0",
                    "port": settings.get("llama_port"),
                    "fit": True,
                    "flash": True,
                    "cacheK": "q8_0",
                    "cacheV": "q8_0",
                    "reasoningMode": "auto",
                    "reasoningBudget": "",
                    "ngl": "auto",
                    "fitTarget": "1024",
                    "nPredict": "-1",
                    "parallel": "1",
                    "threads": "",
                    "batchSize": "",
                    "ubatchSize": "",
                    "threadsBatch": "",
                    "ctxCheckpoints": "",
                    "cacheRam": "",
                    "cacheReuse": "",
                    "splitMode": "",
                    "tensorSplit": "",
                    "mainGpu": "",
                    "cpuMoe": False,
                    "cpuMoeLayers": "",
                    "specType": "",
                    "specDraftNMax": "4",
                    "specDraftModel": "",
                    "specDraftNgl": "",
                    "gpuDevices": [],
                    "device": "",
                }
            }
        )
    if not os.path.exists(COMYFUI_SETTINGS_FILE):
        save_comfy_settings({})
    if not os.path.exists(MEMO_FILE):
        with open(MEMO_FILE, "w", encoding="utf-8") as f:
            f.write("")


@app.on_event("startup")
def startup():
    _ensure_files()
    # Return from FastAPI startup immediately. Hardware, NAS and driver work is
    # detached so the frontend is reachable while Windows is still bringing
    # devices and network shares online.
    _start_sampler_once()
    _refresh_catalogs_if_stale(max_age_seconds=0)
    threading.Thread(
        target=_background_startup,
        name="background-startup", daemon=True,
    ).start()


def _set_startup_state(phase, message, *, ready=None, error=None):
    with _startup_state_lock:
        _startup_state.update({"phase": phase, "message": message})
        if ready is not None:
            _startup_state["ready"] = bool(ready)
            if ready:
                _startup_state["completed_at"] = time.time()
        if error:
            _startup_state["errors"].append(str(error))


def _background_startup():
    # NAS gets its first attempt before managed workloads, but can no longer
    # delay the web frontend. Failed mounts keep retrying in their own worker.
    _set_startup_state("nas", "NAS 자동 연결 확인 중")
    try:
        import nas_mount
        nas_mount.auto_mount()
    except Exception as error:
        print(f"[nas] 최초 자동 마운트 실패, 백그라운드 재시도 시작: {error}")
        nas_mount.auto_mount_async()

    _set_startup_state("hardware", "GPU 및 팬 제어 장치 준비 중")
    if IS_WINDOWS:
        pawnio_bootstrap.ensure_async()

    tuning_ready = True
    _set_startup_state("gpu_tuning", "저장된 GPU 전력·클럭 설정 적용 중")
    try:
        if not _apply_saved_gpu_tuning():
            tuning_ready = _retry_saved_gpu_tuning()
    except Exception as error:
        tuning_ready = False
        print(f"[gpu-tune] 저장 설정 적용 실패: {error}")
        _set_startup_state("gpu_tuning", "GPU 설정 재적용 실패", error=error)

    if settings.get("autostart"):
        _set_startup_state("services", "저장된 서비스 자동 시작 준비 중")
        if IS_WINDOWS:
            try:
                import autostart_service
                st = autostart_service.status()
                if not st.get("exists"):
                    autostart_service.register()
                    print("[autostart] 부팅 시 태스크(MainServer) 미감지 → 자동 재등록")
            except Exception as error:
                print(f"[autostart] 자동 재등록 실패: {error}")
                _set_startup_state("services", "자동 시작 등록 확인 실패", error=error)
        try:
            if IS_WINDOWS and cmp170_service.autostart_enabled() and not cmp170_service.status(include_log=False).get("unlocked"):
                _set_startup_state("cmp170", "CMP 170HX 64GB 언락 대기 중")
                print("[cmp170] 로그인 자동 언락 완료 전까지 GPU 워크로드 시작을 대기합니다")
                _auto_start_services_after_cmp()
            else:
                _auto_start_services()
        except Exception as error:
            _set_startup_state("services", "서비스 자동 시작 중 오류", error=error)

    message = "백그라운드 초기화 완료" if tuning_ready else "초기화 완료 (GPU 설정 오류 확인 필요)"
    _set_startup_state("ready", message, ready=True)


@app.on_event("shutdown")
def shutdown():
    motherboard_fan_controller.close()


def _auto_start_services():
    keys = [
        ("autostart_llama", "llama"),
        ("autostart_comfyui", "comfyui"),
        ("autostart_bot", "bot"),
        ("autostart_watcher", "watcher"),
        ("autostart_vllm", "vllm"),
        ("autostart_unsloth", "unsloth"),
    ]
    any_set = any(settings.get(k) for k, _ in keys)
    for key, name in keys:
        if not settings.get(key):
            continue
        try:
            if name == "llama":
                launch = _read_json(LAST_RUN_FILE, {})
                if not launch:
                    presets = load_presets()
                    launch = presets[sorted(presets.keys())[0]] if presets else {}
                if launch:
                    devices = normalize_gpu_devices(launch.get("gpuDevices") or launch.get("device") or [])
                    services["llama"].start(_build_llama_cmd(launch), device=devices or None)
            elif name == "comfyui":
                comfy_start()
            elif name == "bot":
                bot_start()
            elif name == "watcher":
                watcher_start()
            elif name == "vllm":
                from infrastructure import build_vllm_command, load_settings
                configured = load_settings()
                cmd, runtime_env = build_vllm_command(configured)
                devices = [item.strip() for item in str(configured.get("gpu_devices") or "").split(",") if item.strip()]
                services["vllm"].start(cmd, env=runtime_env, device=devices or None)
            elif name == "unsloth":
                unsloth_start()
        except Exception:
            pass
    if not any_set:
        return


def _auto_start_services_after_cmp():
    if cmp170_service.wait_until_unlocked(timeout=180):
        print("[cmp170] 64GB 확인 완료, 저장된 워크로드 자동 시작을 진행합니다")
        _auto_start_services()
    else:
        print("[cmp170] 180초 내 64GB 확인 실패, GPU 보호를 위해 워크로드 자동 시작을 건너뜁니다")


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
