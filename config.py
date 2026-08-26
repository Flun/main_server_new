import json
import os

from comfy_model_paths import default_model_root

IS_WINDOWS = os.name == "nt"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 설정 파일은 플랫폼별로 분리해 Linux/Windows 설치 환경이 같은 폴더를 공유해도
# 서로의 경로를 오염시키지 않습니다.
SETTINGS_FILE = os.path.join(BASE_DIR, "windows_settings.json" if IS_WINDOWS else "linux_settings.json")

def _default_user_profile():
    """실제 사용자 프로필 경로.

    Windows에서 USERPROFILE 환경 변수가 실행 도구(샌드박스/IDE 등)에 의해
    오버라이드될 수 있으므로, 'X:\\Users\\<이름>' 형태인지 검증하고 아니면
    USERNAME+SYSTEMDRIVE로 직접 구성합니다.
    """
    value = os.environ.get("USERPROFILE", "")
    parts = [p for p in value.replace("/", "\\").split("\\") if p]
    if len(parts) == 3 and parts[1].lower() == "users" and os.path.isdir(value):
        return value
    username = os.environ.get("USERNAME") or os.environ.get("USER", "")
    systemdrive = os.environ.get("SYSTEMDRIVE", "C:").rstrip("\\")
    candidate = f"{systemdrive}\\Users\\{username}"
    if username and os.path.isdir(candidate):
        return candidate
    return os.path.expanduser("~")


_USERPROFILE = _default_user_profile()

DEFAULTS_LINUX = {
    "server_root": "/home/flux",
    "comfyui_dir": "/opt/ComfyUI",
    "comfyui_python": "/opt/ComfyUI/venv/bin/python",
    "comfyui_model_root": default_model_root(),
    "comfyui_port": "8188",
    "llama_install_root": "/opt",
    "llama_version_glob": "/opt/llama-*",
    "llama_port": "8080",
    "model_root": "/mnt/main-server-models/model",
    "vllm_env": "/home/flux/vllm-env",
    "vllm_dflash_env": "/home/flux/vllm-dflash-env",
    "vllm_port": "8000",
    "bot_dir": "/opt/comfy_bridge",
    "watcher_dir": "/home/flux/Documents/New project",
}

DEFAULTS_WINDOWS = {
    "server_root": _USERPROFILE,
    # 이 머신의 실제 ComfyUI_windows_portable 레이아웃에 맞춘 기본값.
    "comfyui_dir": r"C:\ComfyUI_windows_portable\ComfyUI",
    "comfyui_python": r"C:\ComfyUI_windows_portable\python_embeded\python.exe",
    "comfyui_model_root": default_model_root(),
    "comfyui_port": "8188",
    # llama.cpp는 C:\ 루트의 전용 폴더(C:\llama)에 설치합니다.
    # .unsloth 빌드와 공유하지 않으며, git 레포의 CUDA 빌드를
    # CUDA 버전별로 따로 폴더에 둡니다 (llama-<tag>-cuda<ver>).
    "llama_install_root": r"C:\llama",
    "llama_version_glob": r"C:\llama\llama-*",
    "llama_port": "8080",
    "model_root": r"D:\model",
    "vllm_env": r"C:\vllm-env",
    "vllm_dflash_env": r"C:\vllm-dflash-env",
    "vllm_port": "8000",
    "bot_dir": os.path.join(_USERPROFILE, "comfy_bridge"),
    "watcher_dir": os.path.join(_USERPROFILE, "Documents", "New project"),
}

DEFAULTS = dict(DEFAULTS_WINDOWS if IS_WINDOWS else DEFAULTS_LINUX)
DEFAULTS.update({
    "autostart": False,
    "autostart_llama": False,
    "autostart_comfyui": False,
    "autostart_bot": False,
    "autostart_watcher": False,
    "autostart_vllm": False,
    # GPU 전력/클럭/팬 튜닝 마스터 토글 (끄면 부팅 시 저장값 재적용과 UI 적용을 모두 차단)
    "gpu_tuning_enabled": True,
})

ENV_MAP = {
    "server_root": "SERVER_ROOT",
    "comfyui_dir": "COMFY_DIR",
    "comfyui_python": "COMFY_PYTHON",
    "comfyui_model_root": "COMFY_MODEL_ROOT",
    "comfyui_port": "COMFY_PORT",
    "llama_install_root": "LLAMA_INSTALL_ROOT",
    "llama_version_glob": "LLAMA_VERSION_GLOB",
    "llama_port": "LLAMA_PORT",
    "model_root": "MODEL_ROOT",
    "vllm_env": "VLLM_ENV",
    "vllm_dflash_env": "VLLM_DFLASH_ENV",
    "vllm_port": "VLLM_PORT",
    "bot_dir": "BOT_DIR",
    "watcher_dir": "WATCHER_DIR",
    "autostart": "AUTOSTART",
    "autostart_llama": "AUTOSTART_LLAMA",
    "autostart_comfyui": "AUTOSTART_COMFYUI",
    "autostart_bot": "AUTOSTART_BOT",
    "autostart_watcher": "AUTOSTART_WATCHER",
    "autostart_vllm": "AUTOSTART_VLLM",
}


class Settings:
    def __init__(self):
        self._data = {}
        self.reload()

    def reload(self):
        self._data = {}
        if not os.path.exists(SETTINGS_FILE):
            return
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self._data = loaded
        except Exception:
            self._data = {}

    def get(self, key, default=None):
        if key in self._data and self._data[key] not in (None, ""):
            return self._data[key]
        env = ENV_MAP.get(key)
        if env and os.environ.get(env):
            return os.environ[env]
        # 1-arg 호출은 기존과 동일하게 미상 키에 "" 를 반환합니다.
        return DEFAULTS.get(key, "" if default is None else default)

    def all(self):
        return {k: self.get(k) for k in DEFAULTS}

    def save(self, values):
        merged = dict(self._data)
        merged.update({k: v for k, v in values.items() if k in DEFAULTS})
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        self._data = merged
        return self.all()


settings = Settings()
