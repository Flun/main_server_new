"""Stable external ComfyUI model-folder mapping across Windows and Linux."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path


IS_WINDOWS = os.name == "nt"
LINUX_MODEL_DEVICE = Path("/dev/disk/by-uuid/06ECC18DECC17787")
LINUX_MODEL_MOUNT = Path("/mnt/main-server-comfy")
PORTABLE_MODEL_SUFFIX = Path("ComfyUI_windows_portable/ComfyUI/models")
WINDOWS_DEFAULT = Path(r"C:\ComfyUI_windows_portable\ComfyUI\models")
CONFIG_NAME = "main_server_extra_model_paths.yaml"


class ModelPathError(RuntimeError):
    pass


def _run(command: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=timeout)


def _linux_mountpoint(*, try_mount: bool = False) -> Path | None:
    if not LINUX_MODEL_DEVICE.exists():
        return None
    if LINUX_MODEL_MOUNT.is_mount():
        return LINUX_MODEL_MOUNT
    result = _run(["findmnt", "-n", "-o", "TARGET", "--source", str(LINUX_MODEL_DEVICE)])
    mountpoint = result.stdout.strip().splitlines()
    if result.returncode == 0 and mountpoint:
        return Path(mountpoint[0])
    if try_mount:
        # Compatibility fallback for machines not yet migrated to the fixed
        # boot-time mount installed by setup_env.sh.
        try:
            _run(["udisksctl", "mount", "-b", str(LINUX_MODEL_DEVICE), "--no-user-interaction"], timeout=60)
        except (OSError, subprocess.SubprocessError):
            pass
        result = _run(["findmnt", "-n", "-o", "TARGET", "--source", str(LINUX_MODEL_DEVICE)])
        mountpoint = result.stdout.strip().splitlines()
        if result.returncode == 0 and mountpoint:
            return Path(mountpoint[0])
    return None


def _linux_uuid() -> str:
    if not LINUX_MODEL_DEVICE.exists():
        return "06ECC18DECC17787"
    result = _run(["lsblk", "-n", "-o", "UUID", str(LINUX_MODEL_DEVICE)])
    value = result.stdout.strip().splitlines()
    return value[0] if result.returncode == 0 and value and re.fullmatch(r"[A-Za-z0-9-]+", value[0]) else "06ECC18DECC17787"


def default_model_root() -> str:
    if IS_WINDOWS:
        return str(WINDOWS_DEFAULT)
    mountpoint = _linux_mountpoint()
    if mountpoint:
        return str(mountpoint / PORTABLE_MODEL_SUFFIX)
    return str(LINUX_MODEL_MOUNT / PORTABLE_MODEL_SUFFIX)


def resolve_model_root(value: str, *, try_mount: bool = False) -> Path:
    raw = os.path.expandvars(os.path.expanduser(str(value or default_model_root()).strip()))
    if not raw or not os.path.isabs(raw):
        raise ModelPathError("ComfyUI 모델 폴더는 절대 경로여야 합니다")
    selected = Path(os.path.normpath(raw))
    if not IS_WINDOWS:
        normalized = selected.as_posix().rstrip("/")
        suffix = PORTABLE_MODEL_SUFFIX.as_posix()
        # Paths selected from /media/<user>/<UUID>/... are tied to the same
        # physical partition. Resolve its live mount point on every launch.
        if normalized.endswith("/" + suffix) and LINUX_MODEL_DEVICE.exists():
            mountpoint = _linux_mountpoint(try_mount=try_mount)
            if mountpoint:
                selected = mountpoint / PORTABLE_MODEL_SUFFIX
    return selected


MODEL_FOLDERS: dict[str, tuple[str, ...]] = {
    "checkpoints": ("checkpoints", "StableDiffusion"),
    "configs": ("configs",),
    "loras": ("loras", "Lora", "LyCORIS"),
    "vae": ("vae",),
    "text_encoders": ("text_encoders", "clip"),
    "diffusion_models": ("unet", "diffusion_models"),
    "clip_vision": ("clip_vision",),
    "style_models": ("style_models",),
    "embeddings": ("embeddings", "TextualInversion"),
    "diffusers": ("diffusers",),
    "vae_approx": ("vae_approx", "ApproxVAE"),
    "controlnet": ("controlnet", "ControlNet", "T2IAdapter"),
    "gligen": ("gligen",),
    "upscale_models": ("upscale_models", "ESRGAN", "RealESRGAN", "SwinIR", "BSRGAN"),
    "latent_upscale_models": ("latent_upscale_models",),
    "hypernetworks": ("hypernetworks", "Hypernetwork"),
    "photomaker": ("photomaker",),
    "classifiers": ("classifiers",),
    "model_patches": ("model_patches",),
    "audio_encoders": ("audio_encoders",),
    "background_removal": ("background_removal", "RMBG", "BiRefNet"),
    "frame_interpolation": ("frame_interpolation",),
    "geometry_estimation": ("geometry_estimation",),
    "optical_flow": ("optical_flow",),
    "detection": ("detection", "AfterDetailer"),
    # JH Auto Image Feed registers this category and recursively resolves
    # bbox/*.pt and segm/*.pt through folder_paths.  Mapping only the generic
    # detection folders leaves these workflow model references unresolved.
    "ultralytics": ("ultralytics",),
}


def ensure_model_config(comfyui_dir: str, model_root: str, *, try_mount: bool = False) -> tuple[Path, Path]:
    directory = Path(comfyui_dir)
    if not (directory / "main.py").is_file():
        raise ModelPathError(f"ComfyUI 설치 폴더가 아닙니다: {directory}")
    resolved = resolve_model_root(model_root, try_mount=try_mount)
    if not resolved.is_dir():
        raise ModelPathError(f"ComfyUI 모델 폴더를 찾을 수 없습니다: {resolved}")

    lines = ["# Generated by main_server. Edit the integrated path setting instead.", "main_server_models:"]
    lines.append(f"  base_path: {json.dumps(str(resolved), ensure_ascii=False)}")
    lines.append("  is_default: true")
    for key, folders in MODEL_FOLDERS.items():
        existing = [folder for folder in folders if (resolved / folder).is_dir()]
        selected = existing or [folders[0]]
        if len(selected) == 1:
            lines.append(f"  {key}: {json.dumps(selected[0], ensure_ascii=False)}")
        else:
            lines.append(f"  {key}: |")
            lines.extend(f"    {folder}" for folder in selected)
    config_path = directory / CONFIG_NAME
    temporary = config_path.with_suffix(config_path.suffix + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(temporary, config_path)
    return config_path, resolved
