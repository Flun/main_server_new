"""Unified install, path, and vLLM runtime management.

The stable vLLM environment and the experimental DFlash2 environment are kept
separate on purpose: DFlash2 currently needs an unmerged upstream pull request.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import requests
from fastapi import APIRouter, HTTPException

from config import BASE_DIR, DEFAULTS, settings
from process_mgr import Service, tail


router = APIRouter(prefix="/api/infrastructure", tags=["infrastructure"])
vllm_service = Service("vllm")

INFRA_SETTINGS_FILE = Path(BASE_DIR) / "infrastructure_settings.json"
INFRA_LOG_FILE = Path(BASE_DIR) / "logs" / "infrastructure_install.log"
LLAMA_SETTINGS_FILE = Path(BASE_DIR) / "llama_settings.json"

MODEL_ID = "sakamakismile/Qwen3.8-27B-MTP-NVFP4"
DFLASH_MODEL_ID = "incoai/Qwen3.8-27B-DFlash2"
MODEL_REVISION = "a0b936f0bbcb362c38d39840602c8d7b2476a9fc"
VLLM_COMPAT_VERSION = "0.22.0"
DFLASH_VLLM_SPEC = "vllm @ git+https://github.com/vllm-project/vllm.git@refs/pull/52816/head"

INSTALL_STATE: dict[str, Any] = {
    "busy": False,
    "target": "",
    "message": "",
    "started_at": 0.0,
    "finished_at": 0.0,
    "ok": None,
}
INSTALL_LOCK = threading.Lock()


def _read_json(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError, TypeError):
        return default


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def _current_model_root() -> str:
    llama = _read_json(LLAMA_SETTINGS_FILE, {})
    return str(llama.get("model_root") or settings.get("model_root"))


def _default_model_path(model_root: str) -> str:
    alias = Path(model_root) / "vllm" / "Qwen3.8-27B-MTP-NVFP4"
    if alias.exists():
        return str(alias)
    snapshot = (
        Path(model_root)
        / "hub"
        / "models--sakamakismile--Qwen3.8-27B-MTP-NVFP4"
        / "snapshots"
        / MODEL_REVISION
    )
    return str(snapshot) if snapshot.exists() else MODEL_ID


def _venv_python(env_path: Path) -> Path:
    if os.name == "nt":
        return env_path / "Scripts" / "python.exe"
    return env_path / "bin" / "python"


def defaults() -> dict[str, Any]:
    model_root = _current_model_root()
    return {
        "server_root": str(settings.get("server_root") or DEFAULTS["server_root"]),
        "model_root": model_root,
        "comfyui_dir": str(settings.get("comfyui_dir")),
        "llama_install_root": str(settings.get("llama_install_root")),
        "vllm_env": str(settings.get("vllm_env") or DEFAULTS["vllm_env"]),
        "vllm_dflash_env": str(settings.get("vllm_dflash_env") or DEFAULTS["vllm_dflash_env"]),
        "vllm_model": _default_model_path(model_root),
        "dflash_model": DFLASH_MODEL_ID,
        "profile": "mtp",
        "host": "0.0.0.0",
        "port": int(settings.get("vllm_port") or 8000),
        "gpu_devices": "0",
        "gpu_memory_utilization": 0.90,
        "max_model_len": 131072,
        "max_num_seqs": 4,
        "served_model_name": "qwen3.8-27b",
        "api_key": "",
        "extra_args": "",
        "autostart_vllm": bool(settings.get("autostart_vllm")),
    }


def load_settings() -> dict[str, Any]:
    result = defaults()
    saved = _read_json(INFRA_SETTINGS_FILE, {})
    if isinstance(saved, dict):
        result.update({key: value for key, value in saved.items() if key in result})
    return result


def _looks_like_local_path(value: str) -> bool:
    """Hugging Face ID와 구별하기 위한 로컬 경로 판별 (Windows 드라이브 문자 포함)."""
    value = str(value or "").strip()
    if not value:
        return False
    if value.startswith(("/", "~", ".")):
        return True
    if os.name == "nt" and re.match(r"^[A-Za-z]:[\\/]", value):
        return True
    return False


def _absolute_directory(value: Any, field: str) -> str:
    raw = os.path.expandvars(os.path.expanduser(str(value or "").strip()))
    if not raw or not os.path.isabs(raw):
        raise HTTPException(400, f"{field}는 절대 경로여야 합니다")
    return os.path.normpath(raw)


def save_settings(values: dict[str, Any]) -> dict[str, Any]:
    merged = load_settings()
    merged.update({key: value for key, value in values.items() if key in merged})
    for field in (
        "server_root", "model_root", "comfyui_dir", "llama_install_root",
        "vllm_env", "vllm_dflash_env",
    ):
        merged[field] = _absolute_directory(merged[field], field)
    model_value = str(merged.get("vllm_model") or "").strip()
    if not model_value:
        raise HTTPException(400, "vLLM 모델 경로 또는 Hugging Face ID가 필요합니다")
    if _looks_like_local_path(model_value):
        merged["vllm_model"] = _absolute_directory(model_value, "vllm_model")
    try:
        merged["port"] = int(merged["port"])
        merged["max_model_len"] = int(merged["max_model_len"])
        merged["max_num_seqs"] = int(merged["max_num_seqs"])
        merged["gpu_memory_utilization"] = float(merged["gpu_memory_utilization"])
    except (TypeError, ValueError) as error:
        raise HTTPException(400, f"vLLM 숫자 설정이 올바르지 않습니다: {error}") from error
    if not 1 <= merged["port"] <= 65535:
        raise HTTPException(400, "포트 범위는 1~65535입니다")
    if not 0.1 <= merged["gpu_memory_utilization"] <= 1.0:
        raise HTTPException(400, "GPU 메모리 사용률은 0.1~1.0입니다")
    if merged.get("profile") not in {"mtp", "dflash"}:
        raise HTTPException(400, "profile은 mtp 또는 dflash여야 합니다")
    if not re.fullmatch(r"[0-9, ]*", str(merged.get("gpu_devices") or "")):
        raise HTTPException(400, "GPU 목록은 0,1 형식으로 입력하세요")

    _write_json(INFRA_SETTINGS_FILE, merged)
    comfy_dir = Path(merged["comfyui_dir"])
    settings.save({
        "server_root": merged["server_root"],
        "model_root": merged["model_root"],
        "comfyui_dir": merged["comfyui_dir"],
        "comfyui_python": str(_venv_python(comfy_dir)),
        "llama_install_root": merged["llama_install_root"],
        "llama_version_glob": str(Path(merged["llama_install_root"]) / ("llama-*" if os.name != "nt" else "llama*")),
        "vllm_env": merged["vllm_env"],
        "vllm_dflash_env": merged["vllm_dflash_env"],
        "vllm_port": str(merged["port"]),
        "autostart_vllm": bool(merged.get("autostart_vllm")),
    })
    llama = _read_json(LLAMA_SETTINGS_FILE, {})
    llama["model_root"] = merged["model_root"]
    llama.setdefault("vram_cleanup_enabled", True)
    _write_json(LLAMA_SETTINGS_FILE, llama)
    return merged


def _python_version(executable: Path) -> str | None:
    if not executable.is_file():
        return None
    try:
        result = subprocess.run(
            [str(executable), "-c", "from importlib.metadata import version; print(version('vllm'))"],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _gpu_snapshot() -> list[dict[str, Any]]:
    command = [
        "nvidia-smi",
        "--query-gpu=index,name,memory.total,compute_cap,driver_version",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 5:
            continue
        try:
            capability = float(parts[3])
            gpus.append({
                "index": int(parts[0]), "name": parts[1], "memory_mb": int(parts[2]),
                "compute_capability": capability, "driver": parts[4],
                "native_nvfp4": capability >= 12.0,
            })
        except ValueError:
            continue
    return gpus


def _path_state(path: str) -> dict[str, Any]:
    target = Path(path)
    state: dict[str, Any] = {"path": str(target), "exists": target.exists(), "is_dir": target.is_dir()}
    if target.exists():
        try:
            usage = shutil.disk_usage(target)
            state["free_gb"] = round(usage.free / 1024**3, 1)
        except OSError:
            pass
    return state


def runtime_status() -> dict[str, Any]:
    configured = load_settings()
    stable_version = _python_version(_venv_python(Path(configured["vllm_env"])))
    dflash_version = _python_version(_venv_python(Path(configured["vllm_dflash_env"])))
    model = configured["vllm_model"]
    model_local = os.path.isabs(model)
    model_ok = Path(model).is_dir() if model_local else True
    gpus = _gpu_snapshot()
    selected = {
        int(value) for value in str(configured.get("gpu_devices") or "0").split(",")
        if value.strip().isdigit()
    }
    selected_gpus = [gpu for gpu in gpus if gpu["index"] in selected]
    native_nvfp4 = bool(selected_gpus) and all(gpu["native_nvfp4"] for gpu in selected_gpus)
    warnings = []
    if not native_nvfp4:
        warnings.append("선택 GPU는 native NVFP4(SM 12.x)가 아닙니다. CMP 170HX 장착 후 다시 점검하세요.")
    if not model_ok:
        warnings.append(f"로컬 모델 폴더가 없습니다: {model}")
    if configured["profile"] == "dflash" and not dflash_version:
        warnings.append("DFlash2 실험 환경이 아직 설치되지 않았습니다.")
    if configured["profile"] == "mtp" and not stable_version:
        warnings.append("vLLM 안정 환경이 설치되지 않았습니다.")
    return {
        "settings": configured,
        "service": vllm_service.info(),
        "stable": {"installed": bool(stable_version), "version": stable_version, "compatible": stable_version == VLLM_COMPAT_VERSION},
        "dflash": {"installed": bool(dflash_version), "version": dflash_version, "experimental": True},
        "model": {"value": model, "local": model_local, "available": model_ok, "id": MODEL_ID},
        "gpus": gpus,
        "native_nvfp4_ready": native_nvfp4 and model_ok,
        "warnings": warnings,
        "paths": {
            key: _path_state(configured[key]) for key in (
                "server_root", "model_root", "comfyui_dir", "llama_install_root",
                "vllm_env", "vllm_dflash_env",
            )
        },
        "install": dict(INSTALL_STATE),
        "profiles": {
            "mtp": {"label": "NVFP4 + native MTP", "vllm": VLLM_COMPAT_VERSION, "speculative_tokens": 3, "experimental": False},
            "dflash": {"label": "NVFP4 + DFlash2", "vllm": "PR #52816", "speculative_tokens": 7, "experimental": True},
        },
    }


def _install_log(message: str) -> None:
    INFRA_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with INFRA_LOG_FILE.open("a", encoding="utf-8") as stream:
        stream.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    INSTALL_STATE["message"] = message


def _run_logged(command: list[str], *, cwd: str | None = None) -> None:
    safe_command = " ".join(command)
    _install_log(f"실행: {safe_command}")
    with INFRA_LOG_FILE.open("a", encoding="utf-8") as log_stream:
        subprocess.run(command, cwd=cwd, stdout=log_stream, stderr=subprocess.STDOUT, check=True)


def _ensure_venv(path: str) -> Path:
    env_path = Path(path)
    python = _venv_python(env_path)
    if not python.is_file():
        env_path.parent.mkdir(parents=True, exist_ok=True)
        base_python = sys.executable or (shutil.which("python") or shutil.which("python3") or "python3")
        _run_logged([base_python, "-m", "venv", str(env_path)])
    return python


def _install_worker(target: str) -> None:
    INSTALL_STATE.update(busy=True, target=target, message="시작", started_at=time.time(), finished_at=0.0, ok=None)
    configured = load_settings()
    try:
        if target == "vllm-compatible":
            python = _ensure_venv(configured["vllm_env"])
            _run_logged([str(python), "-m", "pip", "install", "--upgrade", f"vllm=={VLLM_COMPAT_VERSION}"])
        elif target == "vllm-latest":
            python = _ensure_venv(configured["vllm_env"])
            _run_logged([str(python), "-m", "pip", "install", "--upgrade", "vllm"])
        elif target == "vllm-dflash":
            python = _ensure_venv(configured["vllm_dflash_env"])
            _run_logged([str(python), "-m", "pip", "install", "--upgrade", DFLASH_VLLM_SPEC])
        elif target == "comfyui":
            directory = Path(configured["comfyui_dir"])
            if (directory / ".git").is_dir():
                _run_logged(["git", "pull", "--ff-only"], cwd=str(directory))
            elif directory.exists() and any(directory.iterdir()):
                raise RuntimeError(f"비어 있지 않은 비-git 폴더입니다: {directory}")
            else:
                directory.parent.mkdir(parents=True, exist_ok=True)
                _run_logged(["git", "clone", "https://github.com/comfyanonymous/ComfyUI.git", str(directory)])
            python = _ensure_venv(str(directory / "venv"))
            _run_logged([str(python), "-m", "pip", "install", "--upgrade", "pip"])
            _run_logged([str(python), "-m", "pip", "install", "-r", str(directory / "requirements.txt")])
        else:
            raise RuntimeError(f"지원하지 않는 설치 대상: {target}")
        _install_log("완료")
        INSTALL_STATE["ok"] = True
    except Exception as error:
        _install_log(f"실패: {error}")
        INSTALL_STATE["ok"] = False
    finally:
        INSTALL_STATE.update(busy=False, finished_at=time.time())


def _extra_args(raw: str) -> list[str]:
    # Deliberately support a conservative whitespace-only format. Values that
    # need JSON are first-class settings above, avoiding shell interpretation.
    values = str(raw or "").split()
    if any("\0" in value for value in values):
        raise HTTPException(400, "추가 인자에 NUL 문자를 사용할 수 없습니다")
    return values


def _venv_entrypoint(env_path: Path, name: str) -> Path:
    """venv 내부 실행 파일 경로 (Windows: Scripts/<name>.exe, POSIX: bin/<name>)."""
    if os.name == "nt":
        return env_path / "Scripts" / f"{name}.exe"
    return env_path / "bin" / name


def build_vllm_command(configured: dict[str, Any] | None = None) -> tuple[list[str], dict[str, str]]:
    configured = configured or load_settings()
    profile = configured["profile"]
    env_root = configured["vllm_dflash_env"] if profile == "dflash" else configured["vllm_env"]
    executable = _venv_entrypoint(Path(env_root), "vllm")
    if not executable.is_file():
        raise HTTPException(400, f"vLLM 실행 파일이 없습니다: {executable}")
    model = str(configured["vllm_model"])
    if _looks_like_local_path(model) and not Path(model).is_dir():
        raise HTTPException(400, f"모델 폴더가 없습니다: {model}")
    speculative = (
        {"method": "dflash", "model": configured["dflash_model"], "num_speculative_tokens": 7}
        if profile == "dflash"
        else {"method": "qwen3_5_mtp", "num_speculative_tokens": 3}
    )
    command = [
        str(executable), "serve", model,
        "--host", str(configured["host"]), "--port", str(configured["port"]),
        "--served-model-name", str(configured["served_model_name"]),
        "--trust-remote-code",
        "--max-model-len", str(configured["max_model_len"]),
        "--max-num-seqs", str(configured["max_num_seqs"]),
        "--gpu-memory-utilization", str(configured["gpu_memory_utilization"]),
        "--kv-cache-dtype", "fp8",
        "--reasoning-parser", "qwen3",
        "--enable-auto-tool-choice", "--tool-call-parser", "qwen3_xml",
        "--limit-mm-per-prompt", json.dumps({"image": 0, "video": 0}, separators=(",", ":")),
        "--speculative-config", json.dumps(speculative, separators=(",", ":")),
    ]
    if configured.get("api_key"):
        command += ["--api-key", str(configured["api_key"])]
    command += _extra_args(configured.get("extra_args", ""))
    runtime_env = {
        "HF_HOME": str(configured["model_root"]),
        "HUGGINGFACE_HUB_CACHE": str(Path(configured["model_root"]) / "hub"),
        "TOKENIZERS_PARALLELISM": "true",
    }
    return command, runtime_env


@router.get("")
def infrastructure_get():
    return runtime_status()


@router.post("/settings")
def infrastructure_settings_save(values: dict[str, Any]):
    saved = save_settings(values)
    return {"ok": True, "settings": saved, "status": runtime_status()}


@router.post("/install/{target}")
def infrastructure_install(target: str):
    allowed = {"vllm-compatible", "vllm-latest", "vllm-dflash", "comfyui"}
    if target not in allowed:
        raise HTTPException(404, f"알 수 없는 설치 대상: {target}")
    with INSTALL_LOCK:
        if INSTALL_STATE["busy"]:
            raise HTTPException(409, f"이미 {INSTALL_STATE['target']} 작업이 진행 중입니다")
        INSTALL_STATE.update(busy=True, target=target, message="대기 중", started_at=time.time(), ok=None)
        threading.Thread(target=_install_worker, args=(target,), daemon=True).start()
    return {"ok": True, "install": dict(INSTALL_STATE)}


@router.get("/install/status")
def infrastructure_install_status():
    return {"install": dict(INSTALL_STATE), "log": tail(str(INFRA_LOG_FILE), 180)}


@router.post("/vllm/start")
def infrastructure_vllm_start(values: dict[str, Any] | None = None):
    configured = save_settings(values) if values else load_settings()
    snapshot = runtime_status()
    if not snapshot["native_nvfp4_ready"]:
        raise HTTPException(409, "native NVFP4 GPU와 로컬 모델 준비 상태를 먼저 확인하세요")
    command, runtime_env = build_vllm_command(configured)
    devices = [item.strip() for item in str(configured["gpu_devices"]).split(",") if item.strip()]
    pid = vllm_service.start(command, env=runtime_env, device=devices or None)
    return {"ok": True, "pid": pid, "cmd": command, "profile": configured["profile"], "port": configured["port"]}


@router.post("/vllm/stop")
def infrastructure_vllm_stop():
    return {"ok": vllm_service.stop()}


@router.post("/vllm/benchmark")
def infrastructure_vllm_benchmark(values: dict[str, Any] | None = None):
    configured = load_settings()
    payload = values or {}
    prompt = str(payload.get("prompt") or "Python으로 LRU cache를 구현하고 시간복잡도를 설명해줘.")
    max_tokens = max(16, min(int(payload.get("max_tokens") or 512), 8192))
    headers = {"Content-Type": "application/json"}
    if configured.get("api_key"):
        headers["Authorization"] = f"Bearer {configured['api_key']}"
    request_body = {
        "model": configured["served_model_name"],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "chat_template_kwargs": {"reasoning_effort": "medium"},
    }
    started = time.perf_counter()
    try:
        response = requests.post(
            f"http://127.0.0.1:{configured['port']}/v1/chat/completions",
            headers=headers, json=request_body, timeout=900,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as error:
        raise HTTPException(503, f"벤치마크 요청 실패: {error}") from error
    elapsed = time.perf_counter() - started
    usage = data.get("usage") or {}
    output_tokens = int(usage.get("completion_tokens") or 0)
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    return {
        "ok": True, "elapsed_seconds": round(elapsed, 3),
        "output_tokens": output_tokens, "prompt_tokens": prompt_tokens,
        "output_tokens_per_second": round(output_tokens / elapsed, 2) if elapsed else None,
        "prefill_tokens_per_second": None,
        "profile": configured["profile"],
    }
