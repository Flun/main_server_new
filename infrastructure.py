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
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import requests
import psutil
from fastapi import APIRouter, HTTPException

from config import BASE_DIR, DEFAULTS, settings
from comfy_model_paths import ModelPathError, default_model_root, ensure_model_config, resolve_model_root
import nas_mount
import linux_setup
from process_mgr import Service, tail


router = APIRouter(prefix="/api/infrastructure", tags=["infrastructure"])
vllm_service = Service("vllm")

# Keep Windows paths separate from the Linux deployment settings. A shared
# checkout otherwise turns /home/... into unusable Windows paths (and vice versa).
INFRA_SETTINGS_FILE = Path(BASE_DIR) / (
    "infrastructure_windows_settings.json" if os.name == "nt" else "infrastructure_settings.json"
)
INFRA_LOG_FILE = Path(BASE_DIR) / "logs" / "infrastructure_install.log"
LLAMA_SETTINGS_FILE = Path(BASE_DIR) / (
    "llama_windows_settings.json" if os.name == "nt" else "llama_settings.json"
)
LEGACY_LLAMA_SETTINGS_FILE = Path(BASE_DIR) / "llama_settings.json"

MODEL_ID = "sakamakismile/Qwen3.8-27B-MTP-NVFP4"
DFLASH_MODEL_ID = "incoai/Qwen3.8-27B-DFlash2"
MODEL_REVISION = "a0b936f0bbcb362c38d39840602c8d7b2476a9fc"
VLLM_COMPAT_VERSION = "0.22.0"
DFLASH_VLLM_SPEC = "vllm @ git+https://github.com/vllm-project/vllm.git@refs/pull/52816/head"
COMFYUI_REPO = "https://github.com/comfyanonymous/ComfyUI.git"
COMFYUI_MANAGER_REPO = "https://github.com/Comfy-Org/ComfyUI-Manager.git"
COMFYUI_JH_NODES_REPO = "https://github.com/Flun/ComfyUI_JH_Nodes.git"
SAGEATTENTION_VERSION = "2.2.0"
# post6 is the known-good Windows baseline, while this pinned follow-up commit
# carries its compatibility work forward and fixes Linux builds with new Torch.
SAGEATTENTION_LINUX_SPEC = "git+https://github.com/woct0rdho/SageAttention.git@890b4ccacb39cf1693e6d96336db61d6c511a2dd"
SAGEATTENTION_WINDOWS_REPO = "woct0rdho/SageAttention"
CMPUNLOCKER_REPO = "https://github.com/lesj0610/cmpunlocker.git"

INSTALL_STATE: dict[str, Any] = {
    "busy": False,
    "target": "",
    "message": "",
    "started_at": 0.0,
    "finished_at": 0.0,
    "ok": None,
}
INSTALL_LOCK = threading.Lock()
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


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


def _safe_exists(path: Path) -> bool:
    """Windows에서 권한(개발자 모드 등)이 없어 symlink를 따르지 못하면 False로 취급합니다."""
    try:
        return path.exists()
    except OSError:
        return False


def _current_model_root() -> str:
    llama = _read_json(LLAMA_SETTINGS_FILE, {})
    if os.name == "nt" and not llama:
        llama = _read_json(LEGACY_LLAMA_SETTINGS_FILE, {})
    return str(llama.get("model_root") or settings.get("model_root"))


def _default_model_path(model_root: str) -> str:
    alias = Path(model_root) / "vllm" / "Qwen3.8-27B-MTP-NVFP4"
    if _safe_exists(alias):
        return str(alias)
    snapshot = (
        Path(model_root)
        / "hub"
        / "models--sakamakismile--Qwen3.8-27B-MTP-NVFP4"
        / "snapshots"
        / MODEL_REVISION
    )
    return str(snapshot) if _safe_exists(snapshot) else MODEL_ID


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
        "comfyui_model_root": str(settings.get("comfyui_model_root") or default_model_root()),
        "llama_install_root": str(settings.get("llama_install_root")),
        "cmpunlocker_profile": "auto",
        "cmpunlocker_profile_mode_version": 2,
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
        # Older builds silently persisted 8gb as the default. Treat that legacy
        # value as auto once so a 10GB card is never patched with the wrong layout.
        if "cmpunlocker_profile_mode_version" not in saved:
            saved = {**saved, "cmpunlocker_profile": "auto"}
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
        "server_root", "model_root", "comfyui_dir", "comfyui_model_root", "llama_install_root",
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
    if merged.get("cmpunlocker_profile") not in {"auto", "8gb", "10gb"}:
        raise HTTPException(400, "CMP unlock profile은 auto, 8gb 또는 10gb여야 합니다")
    if not re.fullmatch(r"[0-9, ]*", str(merged.get("gpu_devices") or "")):
        raise HTTPException(400, "GPU 목록은 0,1 형식으로 입력하세요")

    _write_json(INFRA_SETTINGS_FILE, merged)
    comfy_dir = Path(merged["comfyui_dir"])
    portable_python = comfy_dir.parent / "python_embeded" / "python.exe"
    comfy_python = portable_python if os.name == "nt" and portable_python.is_file() else _venv_python(comfy_dir / "venv")
    resolved_comfy_models = resolve_model_root(merged["comfyui_model_root"])
    if (comfy_dir / "main.py").is_file():
        try:
            _, resolved_comfy_models = ensure_model_config(
                str(comfy_dir), merged["comfyui_model_root"], try_mount=True,
            )
        except ModelPathError as error:
            raise HTTPException(400, str(error)) from error
    settings.save({
        "server_root": merged["server_root"],
        "model_root": merged["model_root"],
        "comfyui_dir": merged["comfyui_dir"],
        "comfyui_python": str(comfy_python),
        "comfyui_model_root": str(resolved_comfy_models),
        "llama_install_root": merged["llama_install_root"],
        "llama_version_glob": str(Path(merged["llama_install_root"]) / ("llama-*" if os.name != "nt" else "llama*")),
        "vllm_env": merged["vllm_env"],
        "vllm_dflash_env": merged["vllm_dflash_env"],
        "vllm_port": str(merged["port"]),
        "autostart_vllm": bool(merged.get("autostart_vllm")),
    })
    llama = _read_json(LLAMA_SETTINGS_FILE, {})
    llama["model_root"] = merged["model_root"]
    _write_json(LLAMA_SETTINGS_FILE, llama)
    return merged


def _python_version(executable: Path) -> str | None:
    return _package_version(executable, "vllm")


def _package_version(executable: Path, package: str) -> str | None:
    if not executable.is_file():
        return None
    script = "from importlib.metadata import version; print(version(" + repr(package) + "))"
    try:
        result = subprocess.run(
            [str(executable), "-c", script], capture_output=True, text=True,
            timeout=30, creationflags=NO_WINDOW,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _gpu_snapshot() -> list[dict[str, Any]]:
    command = [
        "nvidia-smi",
        "--query-gpu=index,name,pci.device_id,memory.total,compute_cap,driver_version",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=8, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 6:
            continue
        try:
            capability = float(parts[4])
            device_id = parts[2].lower().replace("0x", "")[:4]
            gpus.append({
                "index": int(parts[0]), "name": parts[1], "pci_device_id": device_id,
                "memory_mb": int(parts[3]), "compute_capability": capability, "driver": parts[5],
                "native_nvfp4": capability >= 12.0,
                "cmp170hx": device_id in {"20c2", "2082"} or "cmp 170hx" in parts[1].lower(),
            })
        except ValueError:
            continue
    return gpus


def _path_state(path: str) -> dict[str, Any]:
    target = Path(path)
    state: dict[str, Any] = {"path": str(target), "exists": _safe_exists(target), "is_dir": False}
    if state["exists"]:
        try:
            state["is_dir"] = target.is_dir()
        except OSError:
            pass
        try:
            usage = shutil.disk_usage(target)
            state["free_gb"] = round(usage.free / 1024**3, 1)
        except OSError:
            pass
    return state


def _comfy_python_path(configured: dict[str, Any]) -> Path:
    directory = Path(configured["comfyui_dir"])
    portable = directory.parent / "python_embeded" / "python.exe"
    if os.name == "nt" and portable.is_file():
        return portable
    return _venv_python(directory / "venv")


def _cmpunlocker_state(configured: dict[str, Any]) -> dict[str, Any]:
    directory = Path(configured["server_root"]) / "tools" / "cmpunlocker"
    if os.name == "nt":
        return {
            "available": False, "installed": False, "directory": str(directory),
            "repository": CMPUNLOCKER_REPO, "reason": "Linux x86-64 전용",
        }
    kernel = os.uname().release
    module_dir = Path("/lib/modules") / kernel / "updates" / "cmpunlocker"
    gpus = _gpu_snapshot()
    supported_versions = {"610.57.04", "610.43.03", "610.43.02"}
    version_file = directory / "driver" / "VERSION"
    try:
        listed_versions = {
            line.strip() for line in version_file.read_text(encoding="utf-8").splitlines()
            if re.fullmatch(r"\d+\.\d+\.\d+", line.strip())
        }
        if listed_versions:
            supported_versions = listed_versions
    except OSError:
        pass
    driver_ok = any(str(gpu.get("driver", "")) in supported_versions for gpu in gpus)
    return {
        "available": True,
        "installed": module_dir.is_dir(),
        "checkout": (directory / ".git").is_dir(),
        "directory": str(directory),
        "repository": CMPUNLOCKER_REPO,
        "profile": configured.get("cmpunlocker_profile", "auto"),
        "cmp_gpu_found": any(gpu.get("cmp170hx") for gpu in gpus),
        "driver_supported": driver_ok,
        "kernel_headers": (Path("/lib/modules") / kernel / "build").exists(),
        "module_directory": str(module_dir),
        "cold_reboot_required": True,
    }


def runtime_status() -> dict[str, Any]:
    configured = load_settings()
    stable_version = _python_version(_venv_python(Path(configured["vllm_env"])))
    dflash_version = _python_version(_venv_python(Path(configured["vllm_dflash_env"])))
    comfy_python = _comfy_python_path(configured)
    sage_version = _package_version(comfy_python, "sageattention")
    model = configured["vllm_model"]
    model_local = os.path.isabs(model)
    model_ok = _safe_exists(Path(model)) if model_local else True
    gpus = _gpu_snapshot()
    selected = {
        int(value) for value in str(configured.get("gpu_devices") or "0").split(",")
        if value.strip().isdigit()
    }
    selected_gpus = [gpu for gpu in gpus if gpu["index"] in selected]
    native_nvfp4 = bool(selected_gpus) and all(gpu["native_nvfp4"] for gpu in selected_gpus)
    cmp170hx = bool(selected_gpus) and all(gpu["cmp170hx"] for gpu in selected_gpus)
    accelerator_ready = native_nvfp4 or cmp170hx
    warnings = []
    if cmp170hx and not native_nvfp4:
        warnings.append("CMP 170HX가 감지되어 native NVFP4 검증을 건너뛰고 실행을 허용합니다.")
    elif not accelerator_ready:
        warnings.append("선택 GPU가 native NVFP4 GPU 또는 CMP 170HX로 확인되지 않았습니다.")
    if not model_ok:
        warnings.append(f"로컬 모델 폴더가 없습니다: {model}")
    resolved_comfy_models = resolve_model_root(configured["comfyui_model_root"])
    if not _safe_exists(resolved_comfy_models):
        warnings.append(f"ComfyUI 모델 폴더가 없습니다: {resolved_comfy_models}")
    if configured["profile"] == "dflash" and not dflash_version:
        warnings.append("DFlash2 실험 환경이 아직 설치되지 않았습니다.")
    if configured["profile"] == "mtp" and not stable_version:
        warnings.append("vLLM 안정 환경이 설치되지 않았습니다.")
    return {
        "settings": configured,
        "nas": nas_mount.status(),
        "linux_setup": linux_setup.status(),
        "service": vllm_service.info(),
        "stable": {"installed": bool(stable_version), "version": stable_version, "compatible": stable_version == VLLM_COMPAT_VERSION},
        "dflash": {"installed": bool(dflash_version), "version": dflash_version, "experimental": True},
        "comfyui": {
            "python": str(comfy_python), "installed": Path(configured["comfyui_dir"], "main.py").is_file(),
            "model_root": str(resolved_comfy_models),
            "sageattention_version": sage_version,
            "sageattention_ready": bool(sage_version and sage_version.startswith(SAGEATTENTION_VERSION)),
        },
        "model": {"value": model, "local": model_local, "available": model_ok, "id": MODEL_ID},
        "gpus": gpus,
        "native_nvfp4_ready": accelerator_ready and model_ok,
        "native_nvfp4_verified": native_nvfp4,
        "cmp170hx_ready": cmp170hx,
        "platform": "windows" if os.name == "nt" else "linux",
        "cmpunlocker": _cmpunlocker_state(configured),
        "warnings": warnings,
        "paths": {
            key: _path_state(configured[key]) for key in (
                "server_root", "model_root", "comfyui_dir", "comfyui_model_root", "llama_install_root",
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


def _run_logged(command: list[str], *, cwd: str | None = None, env: dict[str, str] | None = None) -> None:
    safe_command = " ".join(command)
    _install_log(f"실행: {safe_command}")
    with INFRA_LOG_FILE.open("a", encoding="utf-8") as log_stream:
        subprocess.run(
            command, cwd=cwd, env=env, stdout=log_stream, stderr=subprocess.STDOUT,
            check=True, creationflags=NO_WINDOW,
        )


def _ensure_venv(path: str) -> Path:
    env_path = Path(path)
    python = _venv_python(env_path)
    base_python = sys.executable or (shutil.which("python") or shutil.which("python3") or "python3")

    def pip_ready(executable: Path) -> bool:
        if not executable.is_file():
            return False
        result = subprocess.run(
            [str(executable), "-m", "pip", "--version"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=15, creationflags=NO_WINDOW,
        )
        return result.returncode == 0

    if not pip_ready(python):
        env_path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            version_result = subprocess.run(
                [base_python, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
                capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW,
            )
            if version_result.returncode:
                raise RuntimeError("venv용 Python 버전을 확인하지 못했습니다")
            package = f"python{version_result.stdout.strip()}-venv"
            ensurepip = subprocess.run(
                [base_python, "-m", "ensurepip", "--version"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=15, creationflags=NO_WINDOW,
            )
            if ensurepip.returncode:
                prefix: list[str] = []
                if os.geteuid() != 0:
                    sudo_check = subprocess.run(
                        ["sudo", "-n", "true"], capture_output=True, text=True,
                        timeout=10, creationflags=NO_WINDOW,
                    )
                    if sudo_check.returncode:
                        raise RuntimeError(
                            f"{package} 설치가 필요하지만 비대화형 sudo를 사용할 수 없습니다"
                        )
                    prefix = ["sudo", "-n"]
                _run_logged([*prefix, "apt-get", "install", "-y", package])
        # A failed `python -m venv` leaves Python symlinks behind without pip.
        # --upgrade repairs that directory in place after ensurepip is installed.
        command = [base_python, "-m", "venv"]
        if python.is_file():
            command.append("--upgrade")
        command.append(str(env_path))
        _run_logged(command)
        if not pip_ready(python):
            raise RuntimeError(f"가상환경 복구 후에도 pip를 실행할 수 없습니다: {env_path}")
    return python


def _comfy_running(directory: Path) -> bool:
    target = str(directory.resolve()).lower()
    for process in psutil.process_iter(["cmdline", "cwd"]):
        try:
            command = " ".join(process.info.get("cmdline") or []).lower()
            cwd = str(process.info.get("cwd") or "").lower()
            if "main.py" in command and (target in command or cwd == target):
                return True
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
            continue
    return False


def _sync_git_repository(repository: str, directory: Path) -> None:
    """Clone a managed repository or fast-forward an existing checkout."""
    if (directory / ".git").is_dir():
        _run_logged(["git", "pull", "--ff-only"], cwd=str(directory))
    elif directory.exists() and any(directory.iterdir()):
        raise RuntimeError(f"비어 있지 않은 비-git 폴더입니다: {directory}")
    else:
        directory.parent.mkdir(parents=True, exist_ok=True)
        _run_logged(["git", "clone", repository, str(directory)])


def _install_comfy_custom_nodes(directory: Path, python: Path) -> None:
    """Install/update the custom nodes bundled with the unified environment."""
    custom_nodes = directory / "custom_nodes"
    custom_nodes.mkdir(parents=True, exist_ok=True)

    INSTALL_STATE["message"] = "ComfyUI-Manager 설치 / 업데이트"
    _sync_git_repository(COMFYUI_MANAGER_REPO, custom_nodes / "ComfyUI-Manager")

    INSTALL_STATE["message"] = "ComfyUI JH Nodes 설치 / 업데이트"
    jh_nodes = custom_nodes / "ComfyUI_JH_Nodes"
    _sync_git_repository(COMFYUI_JH_NODES_REPO, jh_nodes)
    requirements = jh_nodes / "requirements.txt"
    if not requirements.is_file():
        raise RuntimeError(f"ComfyUI JH Nodes 의존성 파일이 없습니다: {requirements}")
    INSTALL_STATE["message"] = "ComfyUI JH Nodes 의존성 설치"
    _run_logged([str(python), "-m", "pip", "install", "-r", str(requirements)])


def _python_torch_info(python: Path) -> dict[str, Any]:
    script = (
        "import json,sys,torch; "
        "print(json.dumps({'python':list(sys.version_info[:3]),'torch':torch.__version__.split('+')[0],"
        "'cuda':torch.version.cuda,'cuda_available':torch.cuda.is_available()}))"
    )
    result = subprocess.run(
        [str(python), "-c", script], capture_output=True, text=True,
        timeout=60, creationflags=NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError("ComfyUI 환경에서 PyTorch를 불러오지 못했습니다: " + (result.stderr.strip() or result.stdout.strip()))
    return json.loads(result.stdout.strip())


def _sage_wheel_compatible(name: str, torch_parts: tuple[int, int, int]) -> bool:
    match = re.search(r"torch(\d+)\.(\d+)\.(\d+)(andhigher)?", name.lower())
    if not match:
        return False
    wheel_torch = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return torch_parts == wheel_torch or (bool(match.group(4)) and torch_parts >= wheel_torch)


def _patch_sageattention_pure_cuda(python: Path) -> None:
    """Match the known-good Windows Sage build's pure-CUDA Q/K path.

    The user's working Windows post6 wheel changes all CUDA entry points from
    per_thread (Triton quantization) to per_warp (CUDA quantization).  The
    unpatched Linux v2.2 source can return NaNs for Qwen/Krea workflows even
    though a small random-tensor smoke test succeeds.
    """
    locate = subprocess.run(
        [str(python), "-c", "import pathlib,sageattention; print(pathlib.Path(sageattention.__file__).with_name('core.py'))"],
        capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW,
    )
    if locate.returncode:
        raise RuntimeError("SageAttention core.py 위치 확인 실패: " + (locate.stderr.strip() or locate.stdout.strip()))
    core = Path(locate.stdout.strip())
    try:
        source = core.read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeError(f"SageAttention core.py를 읽지 못했습니다: {error}") from error
    marker = 'qk_quant_gran: str = "per_thread"'
    replacement = 'qk_quant_gran: str = "per_warp"'
    count = source.count(marker)
    if count:
        source = source.replace(marker, replacement)
        temporary = core.with_suffix(".py.tmp")
        temporary.write_text(source, encoding="utf-8")
        os.replace(temporary, core)
        _run_logged([str(python), "-m", "compileall", "-q", "-f", str(core)])
    elif source.count('qk_quant_gran: str = "per_warp"') < 3:
        raise RuntimeError("SageAttention pure-CUDA 호환 패치를 적용할 위치를 찾지 못했습니다")
    _run_logged([
        str(python), "-c",
        "import inspect,sageattention.core as c; "
        "names=('sageattn_qk_int8_pv_fp16_cuda','sageattn_qk_int8_pv_fp8_cuda','sageattn_qk_int8_pv_fp8_cuda_sm90'); "
        "values=[inspect.signature(getattr(c,n)).parameters['qk_quant_gran'].default for n in names]; "
        "assert values==['per_warp']*3, values; print('SageAttention pure-CUDA per_warp patch OK')",
    ])


def _ensure_linux_nvcc(cuda_version: str) -> str:
    """Install the matching NVIDIA compiler package on supported Ubuntu hosts."""
    match = re.fullmatch(r"(\d+)\.(\d+)", cuda_version)
    if not match:
        raise RuntimeError(f"PyTorch CUDA 버전을 해석할 수 없습니다: {cuda_version}")
    major, minor = match.groups()
    candidates = [
        shutil.which("nvcc"),
        f"/usr/local/cuda-{major}.{minor}/bin/nvcc",
        "/usr/local/cuda/bin/nvcc",
    ]
    found = next((value for value in candidates if value and Path(value).is_file()), None)
    headers_ready = any(Path(path).is_file() for path in (
        f"/usr/local/cuda-{major}.{minor}/include/cusparse.h",
        "/usr/local/cuda/include/cusparse.h",
    )) and all(Path(f"/usr/local/cuda-{major}.{minor}/include/{name}").is_file() for name in (
        "cublas_v2.h", "cublasLt.h", "cusolverDn.h",
    )) and Path(
        f"/usr/include/python{sys.version_info.major}.{sys.version_info.minor}/Python.h"
    ).is_file()
    if found and headers_ready:
        return found

    os_release: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                os_release[key] = value.strip().strip('"')
    except OSError as error:
        raise RuntimeError(f"CUDA Toolkit 설치용 운영체제 정보를 읽지 못했습니다: {error}") from error
    version_id = os_release.get("VERSION_ID", "")
    if os_release.get("ID") != "ubuntu" or not re.fullmatch(r"\d{2}\.\d{2}", version_id):
        raise RuntimeError("nvcc 자동 설치는 Ubuntu에서만 지원합니다")
    if os.uname().machine != "x86_64":
        raise RuntimeError("nvcc 자동 설치는 Ubuntu x86-64에서만 지원합니다")

    prefix: list[str] = []
    if os.geteuid() != 0:
        sudo_check = subprocess.run(
            ["sudo", "-n", "true"], capture_output=True, text=True,
            timeout=10, creationflags=NO_WINDOW,
        )
        if sudo_check.returncode:
            raise RuntimeError("CUDA Toolkit 설치가 필요하지만 비대화형 sudo를 사용할 수 없습니다")
        prefix = ["sudo", "-n"]

    keyring_check = subprocess.run(
        ["dpkg-query", "-W", "-f=${Status}", "cuda-keyring"],
        capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW,
    )
    if "ok installed" not in keyring_check.stdout:
        distro = "ubuntu" + version_id.replace(".", "")
        url = f"https://developer.download.nvidia.com/compute/cuda/repos/{distro}/x86_64/cuda-keyring_1.1-1_all.deb"
        response = requests.get(url, timeout=60)
        response.raise_for_status()
        temporary = tempfile.NamedTemporaryFile(prefix="cuda-keyring-", suffix=".deb", delete=False)
        try:
            temporary.write(response.content)
            temporary.close()
            _run_logged([*prefix, "dpkg", "-i", temporary.name])
        finally:
            try:
                Path(temporary.name).unlink()
            except OSError:
                pass
        _run_logged([*prefix, "apt-get", "update"])

    packages = [
        f"python{sys.version_info.major}.{sys.version_info.minor}-dev",
        f"cuda-nvcc-{major}-{minor}",
        f"libcusparse-dev-{major}-{minor}",
        f"libcublas-dev-{major}-{minor}",
        f"libcusolver-dev-{major}-{minor}",
    ]
    apt_env = dict(os.environ)
    apt_env["DEBIAN_FRONTEND"] = "noninteractive"
    _run_logged([*prefix, "apt-get", "install", "-y", *packages], env=apt_env)
    expected = Path(f"/usr/local/cuda-{major}.{minor}/bin/nvcc")
    if not expected.is_file():
        raise RuntimeError(f"{packages[0]} 설치 후 nvcc를 찾지 못했습니다")
    return str(expected)


def _install_sageattention(python: Path) -> None:
    info = _python_torch_info(python)
    if not info.get("cuda_available"):
        raise RuntimeError("ComfyUI PyTorch에서 CUDA GPU를 사용할 수 없어 SageAttention 설치를 중단합니다")
    if os.name == "nt":
        cuda = str(info.get("cuda") or "")
        cuda_key = "cu" + "".join(cuda.split(".")[:2]) if re.fullmatch(r"\d+\.\d+", cuda) else ""
        torch_parts = tuple(int(x) for x in re.findall(r"\d+", str(info.get("torch") or ""))[:3])
        if not cuda_key or len(torch_parts) != 3:
            raise RuntimeError(
                f"Windows CUDA/PyTorch 버전을 판별할 수 없습니다: torch {info.get('torch')} / CUDA {cuda}"
            )
        _run_logged([str(python), "-m", "pip", "install", "--upgrade", "triton-windows"])
        response = requests.get(
            f"https://api.github.com/repos/{SAGEATTENTION_WINDOWS_REPO}/releases?per_page=10", timeout=30
        )
        response.raise_for_status()
        assets = [
            asset for release in response.json()
            if str(release.get("tag_name", "")).startswith("v2.2.0-windows")
            for asset in release.get("assets", [])
            if cuda_key in asset.get("name", "").lower()
            and asset.get("name", "").lower().endswith("win_amd64.whl")
        ]
        def compatible(asset: dict[str, Any]) -> bool:
            return _sage_wheel_compatible(asset.get("name", ""), torch_parts)
        asset = next((candidate for candidate in assets if compatible(candidate)), None)
        if not asset:
            raise RuntimeError(f"torch {info.get('torch')} / CUDA {cuda}용 SageAttention 2.2.0 Windows wheel이 없습니다")
        _run_logged([str(python), "-m", "pip", "install", "--upgrade", asset["browser_download_url"]])
    else:
        cuda = str(info.get("cuda") or "")
        nvcc = _ensure_linux_nvcc(cuda)
        build_env = dict(os.environ)
        build_env.setdefault("CUDA_HOME", str(Path(nvcc).resolve().parent.parent))
        build_env.setdefault("EXT_PARALLEL", "4")
        build_env.setdefault("MAX_JOBS", str(min(os.cpu_count() or 4, 32)))
        build_env.setdefault("NVCC_APPEND_FLAGS", "--threads 8")
        arches = sorted({
            f"{float(gpu['compute_capability']):.1f}" for gpu in _gpu_snapshot()
            if float(gpu["compute_capability"]) >= 8.0
        })
        if arches:
            build_env["TORCH_CUDA_ARCH_LIST"] = ";".join(arches)
        _run_logged([str(python), "-m", "pip", "install", "--upgrade", "ninja", "packaging", "wheel", "setuptools"])
        _run_logged([
            str(python), "-m", "pip", "install", "--force-reinstall", "--no-deps",
            SAGEATTENTION_LINUX_SPEC, "--no-build-isolation",
        ], env=build_env)
    _patch_sageattention_pure_cuda(python)
    _run_logged([
        str(python), "-c",
        "from importlib.metadata import version; from sageattention import sageattn; "
        "v=version('sageattention'); assert v.startswith('2.2.0'), v; print('SageAttention',v,'import OK')",
    ])


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
            _sync_git_repository(COMFYUI_REPO, directory)
            portable_python = directory.parent / "python_embeded" / "python.exe"
            python = portable_python if os.name == "nt" and portable_python.is_file() else _ensure_venv(str(directory / "venv"))
            _run_logged([str(python), "-m", "pip", "install", "--upgrade", "pip"])
            _run_logged([str(python), "-m", "pip", "install", "-r", str(directory / "requirements.txt")])
            _install_comfy_custom_nodes(directory, python)
            ensure_model_config(str(directory), configured["comfyui_model_root"], try_mount=True)
            _install_sageattention(python)
        elif target.startswith("cmpunlocker-"):
            if os.name == "nt":
                raise RuntimeError("cmpunlocker는 Linux x86-64 전용입니다")
            profile = target.removeprefix("cmpunlocker-")
            if profile not in {"auto", "8gb", "10gb"}:
                raise RuntimeError("CMP unlock profile은 auto, 8gb 또는 10gb여야 합니다")
            unlock_state = _cmpunlocker_state(configured)
            missing = []
            # nvidia-smi and its driver version are unavailable before the
            # NVIDIA driver is active. An explicit profile is the operator's
            # override, so hardware/driver detection gates only auto mode.
            if profile == "auto":
                if not unlock_state.get("cmp_gpu_found"):
                    missing.append("CMP 170HX")
                if not unlock_state.get("driver_supported"):
                    missing.append("nvidia-open 610.43.02/03")
            if not unlock_state.get("kernel_headers"):
                missing.append("현재 커널 headers")
            if missing:
                raise RuntimeError("CMP unlocker 사전 조건 미충족: " + ", ".join(missing))
            directory = Path(configured["server_root"]) / "tools" / "cmpunlocker"
            if (directory / ".git").is_dir():
                _run_logged(["git", "pull", "--ff-only"], cwd=str(directory))
            elif directory.exists() and any(directory.iterdir()):
                raise RuntimeError(f"비어 있지 않은 비-git 폴더입니다: {directory}")
            else:
                directory.parent.mkdir(parents=True, exist_ok=True)
                _run_logged(["git", "clone", CMPUNLOCKER_REPO, str(directory)])
            script = directory / "install.sh"
            if not script.is_file():
                raise RuntimeError(f"CMP unlocker 설치 스크립트가 없습니다: {script}")
            install_command = ["bash", str(script)]
            if profile != "auto":
                install_command.append(f"--profile={profile}")
            if os.geteuid() != 0:
                sudo_check = subprocess.run(
                    ["sudo", "-n", "true"], capture_output=True, text=True,
                    timeout=10, creationflags=NO_WINDOW,
                )
                if sudo_check.returncode:
                    manual = "sudo bash install.sh" + (f" --profile={profile}" if profile != "auto" else "")
                    raise RuntimeError(
                        "관리자 권한이 필요하지만 비대화형 sudo가 허용되지 않았습니다. "
                        f"터미널에서 {directory} 폴더로 이동해 `{manual}`을 실행하세요"
                    )
                install_command = ["sudo", "-n", *install_command]
            _run_logged(install_command, cwd=str(directory))
            _install_log("cmpunlocker 설치 완료 — 반드시 완전 종료 후 전원을 차단했다가 다시 부팅하세요")
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


@router.get("/nas")
def infrastructure_nas_get():
    return nas_mount.status()


@router.post("/nas/settings")
def infrastructure_nas_settings_save(values: dict[str, Any]):
    try:
        username = str(values.get("username") or "").strip()
        password = str(values.get("password") or "")
        current = nas_mount.status()
        if not password and username and current["credentials_saved"] and username != current["credentials_username"]:
            raise nas_mount.MountError("NAS 계정을 변경하려면 암호도 다시 입력하세요")
        if not password and username and not current["credentials_saved"]:
            raise nas_mount.MountError("NAS 암호도 함께 입력하세요")
        configured = nas_mount.save_settings(values)
        if password:
            nas_mount.save_credentials(username, password)
        return {"ok": True, "settings": configured, "nas": nas_mount.status()}
    except nas_mount.MountError as error:
        raise HTTPException(400, str(error)) from error


@router.post("/nas/mount")
def infrastructure_nas_mount():
    try:
        return {"ok": True, "nas": nas_mount.mount()}
    except nas_mount.MountError as error:
        raise HTTPException(400, str(error)) from error


@router.post("/nas/unmount")
def infrastructure_nas_unmount():
    try:
        return {"ok": True, "nas": nas_mount.unmount()}
    except nas_mount.MountError as error:
        raise HTTPException(400, str(error)) from error


@router.delete("/nas/credentials")
def infrastructure_nas_credentials_delete():
    nas_mount.delete_credentials()
    return {"ok": True, "nas": nas_mount.status()}


@router.post("/nas/open/{target}")
def infrastructure_nas_open(target: str):
    snapshot = nas_mount.status()
    targets = {
        "main": (snapshot["main_local"], snapshot["main_mounted"]),
        "comfyui": (snapshot["comfyui_local"], snapshot["comfyui_mounted"]),
    }
    if target not in targets:
        raise HTTPException(404, "알 수 없는 NAS 폴더입니다")
    path, mounted = targets[target]
    if not mounted:
        raise HTTPException(409, "NAS가 아직 마운트되지 않았습니다")
    try:
        if os.name == "nt":
            os.startfile(path)
        else:
            subprocess.Popen(
                ["xdg-open", path], start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except OSError as error:
        raise HTTPException(500, f"파일 탐색기를 열지 못했습니다: {error}") from error
    return {"ok": True, "path": path}


@router.get("/linux-setup")
def infrastructure_linux_setup_get():
    return linux_setup.status()


@router.post("/linux-setup")
def infrastructure_linux_setup_apply(values: dict[str, Any]):
    try:
        state = linux_setup.start(values)
        return {"ok": True, "state": state}
    except RuntimeError as error:
        raise HTTPException(400, str(error)) from error


@router.post("/install/{target}")
def infrastructure_install(target: str):
    allowed = {
        "vllm-compatible", "vllm-latest", "vllm-dflash", "comfyui",
        "cmpunlocker-auto", "cmpunlocker-8gb", "cmpunlocker-10gb",
    }
    if target not in allowed:
        raise HTTPException(404, f"알 수 없는 설치 대상: {target}")
    if target == "comfyui" and _comfy_running(Path(load_settings()["comfyui_dir"])):
        raise HTTPException(409, "ComfyUI가 실행 중입니다. 중지한 뒤 설치/업데이트하세요")
    if target.startswith("cmpunlocker-") and os.name == "nt":
        raise HTTPException(400, "cmpunlocker 설치는 Linux x86-64에서만 지원합니다")
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
        raise HTTPException(409, "선택 GPU(CMP 170HX 또는 native NVFP4)와 로컬 모델 준비 상태를 확인하세요")
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
