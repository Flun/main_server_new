import json
import os

IS_WINDOWS = os.name == "nt"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "linux_settings.json")

DEFAULTS = {
    "server_root": "/home/flux",
    "comfyui_dir": "/opt/ComfyUI",
    "comfyui_python": "/opt/ComfyUI/venv/bin/python",
    "comfyui_port": "8188",
    "llama_install_root": "/opt",
    "llama_version_glob": "/opt/llama-*",
    "llama_port": "8080",
    "model_root": "/mnt/model",
    "vllm_env": "/home/flux/vllm-env",
    "vllm_dflash_env": "/home/flux/vllm-dflash-env",
    "vllm_port": "8000",
    "bot_dir": "/opt/comfy_bridge",
    "watcher_dir": "/home/flux/Documents/New project",
    "autostart": False,
    "autostart_llama": False,
    "autostart_comfyui": False,
    "autostart_bot": False,
    "autostart_watcher": False,
    "autostart_vllm": False,
}

ENV_MAP = {
    "server_root": "SERVER_ROOT",
    "comfyui_dir": "COMFY_DIR",
    "comfyui_python": "COMFY_PYTHON",
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
        if not IS_WINDOWS and os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self._data = loaded
            except Exception:
                self._data = {}

    def get(self, key):
        if key in self._data and self._data[key] not in (None, ""):
            return self._data[key]
        env = ENV_MAP.get(key)
        if env and os.environ.get(env):
            return os.environ[env]
        return DEFAULTS.get(key, "")

    def all(self):
        return {k: self.get(k) for k in DEFAULTS}

    def save(self, values):
        if IS_WINDOWS:
            raise PermissionError("linux_settings는 Linux에서만 사용합니다")
        merged = dict(self._data)
        merged.update({k: v for k, v in values.items() if k in DEFAULTS})
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        self._data = merged
        return self.all()


settings = Settings()
