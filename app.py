import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

import requests

from config import BASE_DIR, IS_WINDOWS, settings
from gpu import (
    get_gpu_topology,
    get_gpus,
    parse_llama_offload,
    scan_vram_processes,
)
from model_hub import router as model_hub_router
from media import router as media_router
from dataset_api import router as dataset_router
from vast_api import router as vast_router
from process_mgr import Service, find_process, tail
from llama_vram_guard import run_guard

HOST = "0.0.0.0"
PORT = 8999

PRESETS_FILE = os.path.join(BASE_DIR, "presets.json")
MEMO_FILE = os.path.join(BASE_DIR, "memo.txt")
COMYFUI_SETTINGS_FILE = os.path.join(BASE_DIR, "comfyui_settings.json")
HW_HISTORY_FILE = os.path.join(BASE_DIR, "hw_history.json")
LAST_RUN_FILE = os.path.join(BASE_DIR, "last_run.json")
LLAMA_SETTINGS_FILE = os.path.join(BASE_DIR, "llama_settings.json")
LLAMA_PUBLIC_PORT = int(settings.get("llama_port") or 8080)
LLAMA_BACKEND_PORT = 8082

services = {
    "comfyui": Service("comfyui"),
    "llama": Service("llama"),
    "bot": Service("bot"),
    "watcher": Service("watcher"),
}

STARTED_AT = time.time()

STATE = {
    "hw": {
        "gpus": [],
        "cpu_percent": 0.0,
        "ram_total": 0,
        "ram_used": 0,
        "ram_percent": 0.0,
        "vram_procs": [],
        "time": 0.0,
    },
    "history": [],
    "llama_versions": [],
    "gguf_models": [],
}

app = FastAPI(title="AI Server Manager", docs_url=None, redoc_url=None)
app.include_router(model_hub_router)
app.include_router(media_router)
app.include_router(dataset_router)
app.include_router(vast_router)


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
        "use_sage_attention": False,
        "preview_method_none": True,
        "cache_none": False,
        "reserve_vram_enabled": True,
        "reserve_vram": 1.0,
        "disable_async_offload": False,
        "fast_disk": False,
        "fast_fp16_accumulation": True,
        "gpu_device": "",
    }
    existing = load_comfy_settings()
    if "reserve_vram_enabled" not in existing and "reserve_vram_1" in existing:
        existing["reserve_vram_enabled"] = bool(existing.get("reserve_vram_1"))
        existing["reserve_vram"] = 1.0
    merged.update({k: existing[k] for k in merged if k in existing})
    for key in (
        "listen", "use_sage_attention", "preview_method_none", "cache_none", "reserve_vram_enabled",
        "disable_async_offload", "fast_disk", "fast_fp16_accumulation",
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
    return {
        "model_root": normalize_model_root(data.get("model_root", settings.get("model_root"))),
        "vram_cleanup_enabled": bool(data.get("vram_cleanup_enabled", True)),
    }


def save_llama_settings(values):
    model_root = normalize_model_root(values.get("model_root"))
    if not os.path.isdir(model_root):
        raise HTTPException(400, f"Model root 폴더가 없습니다: {model_root}")
    saved = {
        "model_root": model_root,
        "vram_cleanup_enabled": bool(values.get("vram_cleanup_enabled", True)),
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


def _build_llama_cmd(preset):
    exe, version_dir = _resolve_llama_binary(preset)
    if not exe:
        raise HTTPException(400, "llama-server 실행 파일을 찾지 못했습니다 (경로 설정 또는 버전 스캔 필요)")
    model = preset.get("model", "")
    if not model or not os.path.isfile(model):
        raise HTTPException(400, f"모델 파일이 없습니다: {model}")
    cmd = [exe, "-m", model]
    mmproj = preset.get("mmproj", "")
    if mmproj and os.path.isfile(mmproj):
        cmd += ["--mmproj", mmproj]
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
        v = str(preset.get(key, "")).strip()
        if v:
            cmd += [flag, v]
    sleep_idle = str(preset.get("sleepIdleSeconds", "")).strip()
    if sleep_idle:
        try:
            sleep_idle_value = int(sleep_idle)
        except ValueError as error:
            raise HTTPException(400, "sleep-idle-seconds는 -1 이상의 정수여야 합니다") from error
        if sleep_idle_value < -1:
            raise HTTPException(400, "sleep-idle-seconds는 -1 이상의 정수여야 합니다")
        cmd += ["--sleep-idle-seconds", str(sleep_idle_value)]
    cmd += ["--port", str(LLAMA_BACKEND_PORT)]
    reasoning_mode = str(preset.get("reasoningMode", "")).strip()
    if reasoning_mode in {"on", "off"}:
        cmd += ["--reasoning", reasoning_mode]
    reasoning_budget = str(preset.get("reasoningBudget", "")).strip()
    if reasoning_mode != "off" and reasoning_budget:
        cmd += ["--reasoning-budget", reasoning_budget]
    fit_target = str(preset.get("fitTarget", "")).strip()
    # llama.cpp에는 --fit과 idle sleep을 함께 사용할 때 재기동 과정에서
    # 이미 계산된 tensor override를 다시 적용하며 실패하는 알려진 문제가 있습니다.
    # sleep을 선택한 경우 고정된 ctx/ngl 설정을 우선해 fit을 끕니다.
    sleep_enabled = bool(sleep_idle) and int(sleep_idle) >= 0
    if preset.get("fit") and not sleep_enabled:
        cmd += ["--fit", "on"]
        if fit_target:
            cmd += ["--fit-target", fit_target]
    else:
        # Current llama.cpp defaults --fit to on, so omission is not enough.
        cmd += ["--fit", "off"]
    spec_type = str(preset.get("specType", "")).strip()
    if spec_type:
        cmd += ["--spec-type", spec_type]
        for flag, key in (
            ("--spec-draft-n-max", "specDraftNMax"),
            ("--spec-draft-n-min", "specDraftNMin"),
            ("--spec-draft-p-split", "specDraftPSplit"),
            ("--spec-draft-p-min", "specDraftPMin"),
            ("--spec-draft-ngl", "specDraftNgl"),
            ("--spec-draft-device", "specDraftDevice"),
        ):
            v = str(preset.get(key, "")).strip()
            if v:
                cmd += [flag, v]
    if preset.get("tempMode") == "general":
        cmd += [
            "--temp", "1.0", "--top-p", "0.95", "--top-k", "20",
            "--min-p", "0.0", "--presence-penalty", "1.5", "--repeat-penalty", "1.0",
        ]
    elif preset.get("tempMode") == "coding":
        cmd += [
            "--temp", "0.6", "--top-p", "0.95", "--top-k", "20",
            "--min-p", "0.0", "--presence-penalty", "0.0", "--repeat-penalty", "1.0",
        ]
    optional_definitions = {
        "batchSize": ("--batch-size", False),
        "ubatchSize": ("--ubatch-size", False),
        "threadsBatch": ("--threads-batch", False),
        "loadMode": ("--load-mode", False),
        "splitMode": ("--split-mode", False),
        "tensorSplit": ("--tensor-split", False),
        "mainGpu": ("--main-gpu", False),
        "cpuMoe": ("--cpu-moe", True),
        "cpuMoeLayers": ("--n-cpu-moe", False),
        "cacheSwa": ("--swa-full", True),
        "cacheReuse": ("--cache-reuse", False),
        "serverTimeout": ("--timeout", False),
        "httpThreads": ("--threads-http", False),
        "metrics": ("--metrics", True),
        "noSlots": ("--no-slots", True),
        "noWarmup": ("--no-warmup", True),
    }
    optional = preset.get("optionalArgs") or {}
    if isinstance(optional, dict):
        for key, value in optional.items():
            definition = optional_definitions.get(key)
            if not definition:
                continue
            flag, boolean_flag = definition
            if boolean_flag:
                if value:
                    cmd.append(flag)
            elif str(value).strip():
                cmd += [flag, str(value).strip()]
    if preset.get("flash"):
        cmd += ["--flash-attn", "on"]
    if preset.get("useTemplate"):
        cmd += ["--jinja"]
    return cmd


# ---------- ComfyUI / 봇 / 와처 ----------

def _comfy_python():
    py = settings.get("comfyui_python")
    if py and os.path.isfile(py):
        return py
    return "python3"


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
    if s.get("reserve_vram_enabled"):
        args += ["--reserve-vram", str(s.get("reserve_vram", 1.0))]
    if s.get("disable_async_offload"):
        args += ["--disable-async-offload"]
    if s.get("fast_disk"):
        args += ["--fast-disk"]
    if s.get("fast_fp16_accumulation"):
        args += ["--fast", "fp16_accumulation"]
    return args


def _run_dir_python(dir_path, script="main.py"):
    for py in (
        os.path.join(dir_path, "venv", "bin", "python"),
        os.path.join(dir_path, ".venv", "bin", "python"),
    ):
        if os.path.isfile(py):
            return py
    if not os.path.isfile(os.path.join(dir_path, script)):
        raise HTTPException(400, f"{script} 파일이 없습니다: {dir_path}")
    return "python3"


# ---------- 하드웨어 샘플러 ----------

def _sample_hw():
    gpus = get_gpus()
    cpu = psutil.cpu_percent(interval=None)
    vm = psutil.virtual_memory()
    STATE["hw"] = {
        "gpus": gpus,
        "cpu_percent": cpu,
        "ram_total": vm.total,
        "ram_used": vm.used,
        "ram_percent": vm.percent,
        "vram_procs": scan_vram_processes(),
        "time": time.time(),
    }
    STATE["history"].append(
        {
            "t": time.time(),
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


threading.Thread(target=_sampler, daemon=True).start()


# ---------- 페이지 ----------

@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))


@app.get("/models", response_class=HTMLResponse)
def models_page():
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



# ---------- 상태 ----------

def _pid_cmdline_has(pid, needle):
    if not needle:
        return True
    try:
        return needle in " ".join(psutil.Process(pid).cmdline())
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
            pids = [p for p in pids if _pid_cmdline_has(p, comfy_dir)]
        if pids:
            st["running"] = True
            st["pid"] = pids[0]
            st["external"] = True
    return st


@app.get("/api/status")
def status():
    scan_llama_versions()
    scan_gguf_models()
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
        "llama_ports": {"public": LLAMA_PUBLIC_PORT, "backend": LLAMA_BACKEND_PORT},
        "last_run": _read_json(LAST_RUN_FILE, {}),
    }


@app.get("/api/gpus")
def gpus():
    topo = get_gpu_topology()
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
    }
    service_defs = {
            "comfyui": {"label": "ComfyUI", "color": "amber"},
            "llama": {"label": "llama.cpp", "color": "sky"},
            "bot": {"label": "봇", "color": "emerald"},
            "watcher": {"label": "와처", "color": "violet"},
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


@app.get("/api/hardware/history")
def hardware_history():
    return {"history": STATE["history"]}


# ---------- llama.cpp ----------

@app.get("/api/llama/presets")
def llama_presets():
    return {"presets": load_presets()}


@app.post("/api/llama/presets")
def llama_presets_save(presets: dict):
    save_presets(presets)
    return {"ok": True, "presets": load_presets()}


def _release_comfy_vram_before_llama_start():
    """Apply the same cleanup policy before the initial llama model load.

    The public guard protects wake-up inference, but llama-server also loads a
    model once during process startup. That first load needs the same protection.
    """
    if not load_llama_settings().get("vram_cleanup_enabled"):
        return {"comfy_online": None, "released": False}
    comfy_url = f"http://127.0.0.1:{int(settings.get('comfyui_port') or 8188)}"
    try:
        response = requests.get(f"{comfy_url}/queue", timeout=3)
        response.raise_for_status()
    except requests.RequestException:
        return {"comfy_online": False, "released": False}
    while True:
        try:
            queue = response.json()
        except ValueError as error:
            raise HTTPException(503, "ComfyUI 큐 상태를 해석하지 못해 llama 시작을 중단했습니다") from error
        if not (queue.get("queue_running") or []) and not (queue.get("queue_pending") or []):
            break
        time.sleep(2)
        try:
            response = requests.get(f"{comfy_url}/queue", timeout=5)
            response.raise_for_status()
        except requests.RequestException as error:
            raise HTTPException(503, "ComfyUI 작업 대기 중 연결이 끊겨 llama 시작을 중단했습니다") from error
    try:
        released = requests.post(
            f"{comfy_url}/free",
            json={"unload_models": True, "free_memory": True},
            timeout=30,
        )
        released.raise_for_status()
    except requests.RequestException as error:
        raise HTTPException(503, "ComfyUI VRAM 정리에 실패해 llama 시작을 중단했습니다") from error
    return {"comfy_online": True, "released": True}


@app.post("/api/llama/start")
def llama_start(preset: dict):
    effective = dict(preset)
    warnings = []
    try:
        sleep_enabled = int(str(effective.get("sleepIdleSeconds", "-1") or "-1")) >= 0
    except ValueError:
        sleep_enabled = False
    if sleep_enabled and effective.get("fit"):
        effective["fit"] = False
        warnings.append("VRAM Auto-Unload와 --fit의 llama.cpp 재로딩 충돌을 피하기 위해 --fit을 자동으로 껐습니다.")
    cleanup = _release_comfy_vram_before_llama_start()
    if cleanup.get("released"):
        warnings.append("llama 시작 전에 ComfyUI 모델과 캐시를 정리했습니다.")
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
        "public_url": f"http://127.0.0.1:{LLAMA_PUBLIC_PORT}",
        "backend_url": f"http://127.0.0.1:{LLAMA_BACKEND_PORT}",
        "gpu_devices": devices,
        "warnings": warnings,
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


# ---------- llama.cpp VRAM 가드 ----------
# 사람/웹 UI는 공개 포트 8080을 사용합니다. 가드는 ComfyUI 작업이 끝날
# 때까지 기다린 뒤 VRAM을 비우고 내부 llama-server(8082)로 전달합니다.
# ComfyUI 커스텀 노드는 8082를 직접 호출해 자기 자신을 기다리는 교착을 피합니다.


def _run_guard_server():
    try:
        run_guard(
            host="0.0.0.0",
            port=LLAMA_PUBLIC_PORT,
            backend_port=LLAMA_BACKEND_PORT,
            comfy_port=int(settings.get("comfyui_port") or 8188),
            settings_file=LLAMA_SETTINGS_FILE,
        )
    except Exception as error:
        print(f"[llama vram guard] {LLAMA_PUBLIC_PORT} 서버 시작 실패: {error}")


_guard_thread = None


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
LLAMA_SRC_ROOT = "/opt/llama-src"

LLAMA_BUILD_STATE = {
    "busy": False,
    "tag": "",
    "message": "",
    "started_at": 0.0,
}


@app.get("/api/llama/releases")
def llama_releases():
    try:
        import requests

        res = requests.get("https://api.github.com/repos/ggml-org/llama.cpp/releases/latest", timeout=15)
        res.raise_for_status()
        data = res.json()
        tag = data.get("tag_name", "")
        return {
            "tag_name": tag,
            "published_at": data.get("published_at"),
            "html_url": data.get("html_url"),
            "platform": "linux" if not IS_WINDOWS else "windows",
            "git_url": LLAMA_GIT_REPO,
            "source_url": f"{LLAMA_GIT_REPO}/archive/refs/tags/{tag}.tar.gz",
            "installed": os.path.isdir(os.path.join(settings.get("llama_install_root"), f"llama-{tag}")),
            "assets": data.get("assets", []) if IS_WINDOWS else [],
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


def _build_llama_git(tag):
    """최신 llama.cpp를 git에서 클론해 CUDA로 빌드하고 /opt/llama-<tag> 에 설치한다."""
    LLAMA_BUILD_STATE.update(busy=True, tag=tag, message="시작", started_at=time.time())
    try:
        _llama_install_log(f"===== llama.cpp {tag} git 빌드 시작 =====")

        # 소스 클론 (이미 있으면 최신 태그로 갱신)
        src_dir = os.path.join(LLAMA_SRC_ROOT, tag)
        os.makedirs(src_dir, exist_ok=True)
        if not os.path.exists(os.path.join(src_dir, ".git")):
            _llama_install_log(f"git clone --depth 1 --branch {tag} {LLAMA_GIT_REPO}")
            subprocess.run(
                ["git", "clone", "--depth", "1", "--branch", tag, LLAMA_GIT_REPO, src_dir],
                check=True,
            )
        else:
            _llama_install_log("소스 이미 존재, git fetch")
            subprocess.run(["git", "fetch", "--depth", "1", "origin", "tag", tag], cwd=src_dir, check=True)
            subprocess.run(["git", "checkout", tag], cwd=src_dir, check=True)

        build_dir = os.path.join(src_dir, "build")
        os.makedirs(build_dir, exist_ok=True)

        _llama_install_log("cmake -B build -DGGML_CUDA=ON -DCMAKE_BUILD_TYPE=Release")
        LLAMA_BUILD_STATE["message"] = "CMake 구성 (GGML_CUDA=ON)"
        subprocess.run(
            [
                "cmake", "-B", "build",
                "-DGGML_CUDA=ON",
                "-DCMAKE_BUILD_TYPE=Release",
                "-DGGML_CUDA_F16=ON",
                "-DBUILD_SHARED_LIBS=OFF",
            ],
            cwd=src_dir, check=True,
        )

        nproc = os.cpu_count() or 4
        _llama_install_log(f"cmake --build build -j{nproc}")
        LLAMA_BUILD_STATE["message"] = "CUDA 빌드 중"
        subprocess.run(["cmake", "--build", "build", "-j", str(nproc)], cwd=src_dir, check=True)

        # 설치 위치: /opt/llama-<tag>/bin/llama-server 등
        install_dir = os.path.join(settings.get("llama_install_root"), f"llama-{tag}")
        os.makedirs(os.path.join(install_dir, "bin"), exist_ok=True)
        bin_src = os.path.join(build_dir, "bin")
        if os.path.isdir(bin_src):
            for fn in os.listdir(bin_src):
                shutil.copy2(os.path.join(bin_src, fn), os.path.join(install_dir, "bin"))
        else:
            for exe in ("llama-server", "llama-cli", "llama-quantize"):
                src = os.path.join(build_dir, exe)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(install_dir, "bin"))
        _llama_install_log(f"설치 완료: {install_dir}")
        LLAMA_BUILD_STATE["message"] = f"설치 완료: {install_dir}"
    except Exception as e:
        _llama_install_log(f"빌드 실패: {e}")
        LLAMA_BUILD_STATE["message"] = f"실패: {e}"
    finally:
        LLAMA_BUILD_STATE["busy"] = False
        scan_llama_versions()


@app.post("/api/llama/install")
def llama_install(tag: str = ""):
    tag = _safe_tag(tag)
    if LLAMA_BUILD_STATE["busy"]:
        raise HTTPException(409, "이미 빌드가 진행 중입니다.")
    if IS_WINDOWS:
        raise HTTPException(400, "git 빌드는 Linux에서만 사용합니다")
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
    cmd = [
        _comfy_python(),
        "main.py",
        "--port",
        str(settings.get("comfyui_port")),
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

@app.get("/api/linux-settings")
def linux_settings_get():
    if IS_WINDOWS:
        return {"platform": "windows", "settings": {}, "effective": {}}
    enabled = subprocess.run(
        ["systemctl", "--user", "is-enabled", "main_server.service"],
        capture_output=True, text=True,
    ).returncode == 0
    return {
        "platform": "linux", "settings": settings.all(), "effective": settings.all(),
        "manager_service": {"enabled": enabled, "unit": "main_server.service", "linger": True},
    }


@app.post("/api/linux-settings")
def linux_settings_save(data: dict):
    if IS_WINDOWS:
        raise HTTPException(400, "linux_settings는 Linux에서만 사용합니다")
    effective = settings.save(data)
    scan_llama_versions()
    scan_gguf_models()
    return {"ok": True, "effective": effective}


GPU_CONTROL_HELPER = "/usr/local/sbin/main-server-gpu-control"
GPU_TUNE_CONFIG = "/etc/main-server/gpu-tune.conf"


def _gpu_tuning_snapshot():
    fields = (
        "index,name,power.draw,power.limit,power.default_limit,power.min_limit,"
        "power.max_limit,clocks.current.graphics,clocks.max.graphics,fan.speed"
    )
    result = subprocess.run(
        ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=8,
    )
    if result.returncode:
        raise HTTPException(503, result.stderr.strip() or "NVIDIA GPU 정보를 읽지 못했습니다")
    configured = {}
    try:
        for line in Path(GPU_TUNE_CONFIG).read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                configured[key.strip()] = int(value.strip())
    except (OSError, ValueError):
        configured = {"GPU_INDEX": 0, "POWER_LIMIT": 280, "CLOCK_MAX": 1800, "FAN_PERCENT": 65}

    def number(value, integer=False):
        value = value.strip()
        if value in {"", "N/A", "[N/A]"}:
            return None
        return int(float(value)) if integer else round(float(value), 1)

    gpus = []
    for line in result.stdout.strip().splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 10:
            continue
        gpu = {
            "index": int(parts[0]), "name": parts[1], "power_draw": number(parts[2]),
            "power_limit": number(parts[3]), "power_default": number(parts[4]),
            "power_min": number(parts[5]), "power_max": number(parts[6]),
            "clock_current": number(parts[7], True), "clock_hardware_max": number(parts[8], True),
            "fan_speed": number(parts[9], True), "configured": None,
        }
        if configured.get("GPU_INDEX") == gpu["index"]:
            gpu["configured"] = {
                "power_limit": configured.get("POWER_LIMIT"),
                "clock_max": configured.get("CLOCK_MAX"),
                "fan_percent": configured.get("FAN_PERCENT"),
            }
        gpus.append(gpu)
    return {"available": bool(gpus), "gpus": gpus, "service": "gpu-tune.service"}


@app.get("/api/gpu/tuning")
def gpu_tuning_get():
    if IS_WINDOWS:
        raise HTTPException(400, "Linux NVIDIA 환경에서만 지원합니다")
    return _gpu_tuning_snapshot()


@app.post("/api/gpu/tuning")
def gpu_tuning_set(data: dict):
    if IS_WINDOWS:
        raise HTTPException(400, "Linux NVIDIA 환경에서만 지원합니다")
    if not os.path.isfile(GPU_CONTROL_HELPER):
        raise HTTPException(503, "GPU 제어 helper가 설치되지 않았습니다")
    action = str(data.get("action") or "set")
    try:
        gpu_index = int(data.get("gpu_index", 0))
        if action == "reset":
            command = ["sudo", "-n", GPU_CONTROL_HELPER, "reset", str(gpu_index)]
        elif action == "set":
            power = int(data["power_limit"])
            clock = int(data["clock_max"])
            fan = int(data["fan_percent"])
            command = ["sudo", "-n", GPU_CONTROL_HELPER, "set", str(gpu_index), str(power), str(clock), str(fan)]
        else:
            raise ValueError("지원하지 않는 작업입니다")
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(400, f"GPU 설정값이 올바르지 않습니다: {error}") from error
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
        "logs": os.path.join(BASE_DIR, "logs"),
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
    if IS_WINDOWS:
        raise HTTPException(400, "재시작은 Linux에서만 지원합니다")
    threading.Thread(target=_restart_now, daemon=True).start()
    return {"ok": True}


def _restart_now():
    time.sleep(0.5)
    try:
        subprocess.Popen(
            [sys.executable] + sys.argv,
            start_new_session=True,
            stdout=open(os.path.join(BASE_DIR, "logs", "manager.log"), "a"),
            stderr=subprocess.STDOUT,
        )
    finally:
        os._exit(0)


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
                    "ctx": "262144",
                    "host": "0.0.0.0",
                    "port": settings.get("llama_port"),
                    "fit": True,
                    "flash": True,
                    "cacheK": "q4_0",
                    "cacheV": "q4_0",
                    "tempMode": "general",
                    "useTemplate": False,
                    "reasoningMode": "auto",
                    "reasoningBudget": "",
                    "ngl": "999",
                    "fitTarget": "1024",
                    "nPredict": "-1",
                    "parallel": "1",
                    "threads": "",
                    "sleepIdleSeconds": "-1",
                    "optionalArgs": {},
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
    global _guard_thread
    _ensure_files()
    scan_llama_versions()
    scan_gguf_models()
    _sample_hw()
    if _guard_thread is None or not _guard_thread.is_alive():
        _guard_thread = threading.Thread(
            target=_run_guard_server,
            name="llama-vram-guard",
            daemon=True,
        )
        _guard_thread.start()
    if settings.get("autostart"):
        try:
            _auto_start_services()
        except Exception:
            pass


def _auto_start_services():
    keys = [
        ("autostart_llama", "llama"),
        ("autostart_comfyui", "comfyui"),
        ("autostart_bot", "bot"),
        ("autostart_watcher", "watcher"),
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
        except Exception:
            pass
    if not any_set:
        return


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
