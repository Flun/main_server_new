from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import os
import re
import shlex
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, quote, unquote, urlparse, urlunparse

import psutil
import requests
import urllib3
import websockets
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = Path(__file__).resolve().parent
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
SETTINGS_FILE = BASE_DIR / "vast_settings.json"
ENV_FILE = BASE_DIR / "vast_qwen.env"
PROXY_SCRIPT = BASE_DIR / "vast_qwen_proxy.py"

def require_local_request(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(status_code=403, detail="Vast Remote는 토큰을 다루므로 이 PC에서만 접근할 수 있습니다.")


router = APIRouter(dependencies=[Depends(require_local_request)])
settings_lock = threading.Lock()

MODEL_FOLDERS = {
    "llama": "/workspace/llama/models",
    "checkpoints": "/workspace/ComfyUI/models/checkpoints",
    "diffusion_models": "/workspace/ComfyUI/models/diffusion_models",
    "text_encoders": "/workspace/ComfyUI/models/text_encoders",
    "vae": "/workspace/ComfyUI/models/vae",
    "vae_approx": "/workspace/ComfyUI/models/vae_approx",
    "loras": "/workspace/ComfyUI/models/loras",
    "controlnet": "/workspace/ComfyUI/models/controlnet",
    "clip_vision": "/workspace/ComfyUI/models/clip_vision",
    "upscale_models": "/workspace/ComfyUI/models/upscale_models",
}


class VastError(RuntimeError):
    pass


class ConnectionSettings(BaseModel):
    jupyter_url: str = ""
    env_file: str = str(ENV_FILE)
    proxy_script: str = str(PROXY_SCRIPT)
    local_proxy_host: str = "127.0.0.1"
    local_proxy_port: int = Field(default=18081, ge=1, le=65535)
    request_timeout: float = Field(default=1800, ge=1, le=86400)
    connect_timeout: float = Field(default=20, ge=1, le=300)


class LlamaOptions(BaseModel):
    model: str = "/workspace/llama/models/Qwen3.8-27B-UD-Q8_K_XL.gguf"
    host: str = "127.0.0.1"
    port: int = Field(default=18080, ge=1, le=65535)
    context: int = Field(default=262144, ge=1024)
    parallel: int = Field(default=1, ge=1, le=64)
    gpu_layers: str = "auto"
    fit: bool = True
    fit_target: int = Field(default=2048, ge=0, le=65536)
    batch: int = Field(default=512, ge=1)
    ubatch: int = Field(default=256, ge=1)
    cache_k: str = "q8_0"
    cache_v: str = "q8_0"
    flash_attn: Literal["auto", "on", "off"] = "auto"
    mtp: bool = True
    mtp_n: int = Field(default=2, ge=1, le=16)
    threads: int | None = Field(default=None, ge=1)
    threads_batch: int | None = Field(default=None, ge=1)
    numa: Literal["disabled", "distribute", "isolate", "numactl"] = "disabled"
    load_mode: Literal["auto", "mmap", "mlock", "mmap+mlock", "none"] = "auto"
    extra_args: str = ""


class ComfyOptions(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=18188, ge=1, le=65535)
    vram_headroom: float = Field(default=2.0, ge=0, le=64)
    reserve_vram: float | None = Field(default=None, ge=0, le=64)
    preview_method: Literal["none", "auto", "latent2rgb", "taesd"] = "none"
    cache_none: bool = False
    disable_smart_memory: bool = False
    disable_async_offload: bool = False
    disable_pinned_memory: bool = False
    disable_xformers: bool = False
    fast_disk: bool = True
    fast_fp16_accumulation: bool = False
    lowvram: bool = False
    extra_args: str = ""


class SavedSettings(BaseModel):
    connection: ConnectionSettings = Field(default_factory=ConnectionSettings)
    llama: LlamaOptions = Field(default_factory=LlamaOptions)
    comfy: ComfyOptions = Field(default_factory=ComfyOptions)


class ModelDownload(BaseModel):
    url: str
    category: str
    filename: str = ""


class TunnelOptions(BaseModel):
    port: int = Field(default=18080, ge=1, le=65535)
    update_proxy: bool = True


class SetupOptions(BaseModel):
    h3_zip: str = str(Path.home() / "Desktop" / "h3_custom_nodes.zip")
    h3_script: str = str(Path.home() / "Desktop" / "setup_h3_final_v2.sh")


def _model_dump(value: BaseModel) -> dict[str, Any]:
    return value.model_dump() if hasattr(value, "model_dump") else value.dict()


def load_settings() -> SavedSettings:
    with settings_lock:
        if not SETTINGS_FILE.is_file():
            return SavedSettings()
        try:
            return SavedSettings(**json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception:
            return SavedSettings()


def save_settings(settings: SavedSettings) -> SavedSettings:
    with settings_lock:
        SETTINGS_FILE.write_text(
            json.dumps(_model_dump(settings), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return settings


def parse_jupyter_url(raw: str) -> tuple[str, str]:
    raw = raw.strip()
    if not raw:
        raise VastError("Open Jupyter 전체 URL을 입력하세요.")
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    token = parse_qs(parsed.query).get("token", [""])[0]
    if not token:
        raise VastError("URL에 ?token=... 값이 없습니다. Jupyter 터미널에서 printenv JUPYTER_TOKEN으로 확인할 수 있습니다.")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise VastError("올바른 http/https Jupyter URL이 아닙니다.")
    base = urlunparse((parsed.scheme, parsed.netloc, "", "", "", "")).rstrip("/")
    return base, token


def jupyter_access_url(raw: str) -> str:
    base, token = parse_jupyter_url(raw)
    return f"{base}/tree?token={quote(token)}"


def read_env_value(path: Path, name: str) -> str:
    if not path.is_file():
        raise VastError(f"환경 파일을 찾지 못했습니다: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(name + "="):
            value = line.split("=", 1)[1].strip()
            if value:
                return value
    raise VastError(f"{path}에서 {name}을 찾지 못했습니다.")


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    positions = {line.split("=", 1)[0]: i for i, line in enumerate(lines) if "=" in line}
    for key, value in updates.items():
        if key in positions:
            lines[positions[key]] = f"{key}={value}"
        else:
            lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def shell(value: Any) -> str:
    return shlex.quote(str(value))


def safe_extra_args(value: str) -> list[str]:
    if any(ch in value for ch in "\r\n\x00"):
        raise VastError("추가 인자에는 줄바꿈이나 제어 문자를 사용할 수 없습니다.")
    return shlex.split(value, posix=True)


def clean_terminal(text: str) -> str:
    text = re.sub(r"\x1b\][^\x07]*\x07", "", text)
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    return text.replace("\r", "")


class JupyterClient:
    def __init__(self, full_url: str):
        self.base, self.token = parse_jupyter_url(full_url)
        self.http = requests.Session()
        self.http.verify = False
        self.http.headers["Authorization"] = f"token {self.token}"
        response = self.http.get(f"{self.base}/api/status", timeout=20)
        response.raise_for_status()

    def upload_bytes(self, data: bytes, remote_path: str) -> None:
        payload = {"type": "file", "format": "base64", "content": base64.b64encode(data).decode("ascii")}
        url = f"{self.base}/api/contents/{quote(remote_path.lstrip('/'), safe='/')}"
        response = self.http.put(url, json=payload, timeout=900)
        response.raise_for_status()

    def upload_file(self, local_path: Path, remote_path: str) -> None:
        if not local_path.is_file():
            raise VastError(f"로컬 파일을 찾지 못했습니다: {local_path}")
        self.upload_bytes(local_path.read_bytes(), remote_path)

    def read(self, remote_path: str, tail: int | None = None) -> str:
        url = f"{self.base}/api/contents/{quote(remote_path.lstrip('/'), safe='/')}"
        response = self.http.get(url, params={"content": 1}, timeout=90)
        if response.status_code == 404:
            return ""
        response.raise_for_status()
        payload = response.json()
        content = payload.get("content") or ""
        if payload.get("format") == "base64":
            content = base64.b64decode(content).decode("utf-8", errors="replace")
        text = str(content)
        return "\n".join(text.splitlines()[-tail:]) if tail else text

    def execute(self, command: str, marker: str, timeout: int = 900) -> str:
        return asyncio.run(self._execute(command, marker, timeout))

    async def _execute(self, command: str, marker: str, timeout: int) -> str:
        response = self.http.post(f"{self.base}/api/terminals", json={}, timeout=30)
        response.raise_for_status()
        name = str(response.json()["name"])
        parsed = urlparse(self.base)
        websocket_url = f"{'wss' if parsed.scheme == 'https' else 'ws'}://{parsed.netloc}/terminals/websocket/{name}?token={quote(self.token)}"
        ssl_context = None
        if parsed.scheme == "https":
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        output = ""
        try:
            async with websockets.connect(websocket_url, ssl=ssl_context, origin=self.base, open_timeout=30, max_size=None) as ws:
                await ws.send(json.dumps(["set_size", 40, 180, 1440, 900]))
                await ws.send(json.dumps(["stdin", "stty -echo\r"]))
                await asyncio.sleep(0.2)
                try:
                    while True:
                        await asyncio.wait_for(ws.recv(), timeout=0.05)
                except asyncio.TimeoutError:
                    pass
                await ws.send(json.dumps(["stdin", command + f"; printf '\\n{marker}\\n'\r"]))
                async with asyncio.timeout(timeout):
                    while marker not in output:
                        raw = await ws.recv()
                        message = json.loads(raw)
                        if isinstance(message, list) and len(message) >= 2 and message[0] == "stdout":
                            output = (output + str(message[1]))[-200000:]
        finally:
            self.http.delete(f"{self.base}/api/terminals/{quote(name)}", timeout=30)
        return clean_terminal(output)


def client() -> JupyterClient:
    return JupyterClient(load_settings().connection.jupyter_url)


def upload_api_key(remote: JupyterClient) -> None:
    config = load_settings().connection
    key = read_env_value(Path(config.env_file), "OPENAI_API_KEY")
    remote.execute("mkdir -p /workspace/llama", "VAST_KEY_DIR")
    remote.upload_bytes((key + "\n").encode(), "workspace/llama/api_key.txt")
    remote.execute("chmod 600 /workspace/llama/api_key.txt", "VAST_KEY_READY")


def remote_status(remote: JupyterClient) -> dict[str, Any]:
    script = r'''
import csv, io, json, os, re, shutil, subprocess
def run(args):
    try: return subprocess.run(args, capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW).stdout.strip()
    except Exception: return ""
gpu_raw = run(["nvidia-smi", "--query-gpu=index,name,temperature.gpu,utilization.gpu,memory.used,memory.total,memory.free,power.draw,power.limit,driver_version", "--format=csv,noheader,nounits"])
gpu = []
for row in csv.reader(io.StringIO(gpu_raw)):
    if len(row) >= 10:
        gpu.append({"index": int(row[0]), "name": row[1].strip(), "temperature": float(row[2]), "utilization": float(row[3]), "memory_used_mib": float(row[4]), "memory_total_mib": float(row[5]), "memory_free_mib": float(row[6]), "power_w": float(row[7]), "power_limit_w": float(row[8]), "driver": row[9].strip()})
proc_raw = run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader,nounits"])
processes = []
for row in csv.reader(io.StringIO(proc_raw)):
    if len(row) >= 3:
        try: processes.append({"pid": int(row[0]), "name": row[1].strip(), "memory_mib": float(row[2])})
        except ValueError: pass
def pid_alive(path):
    try:
        pid = int(open(path).read().strip()); os.kill(pid, 0); return {"running": True, "pid": pid}
    except Exception: return {"running": False, "pid": None}
mem = run(["free", "-b"]).splitlines()
ram = {}
if len(mem) > 1:
    p = mem[1].split(); ram = {"total": int(p[1]), "used": int(p[2]), "free": int(p[3]), "available": int(p[6])}
disk = shutil.disk_usage("/workspace")
tunnel = ""
try:
    text = open("/workspace/llama/cloudflared.log", errors="replace").read()
    hits = re.findall(r"https://[-a-z0-9]+\.trycloudflare\.com", text)
    tunnel = hits[-1] if hits else ""
except Exception: pass
print("__VAST_JSON__" + json.dumps({"gpus": gpu, "processes": processes, "ram": ram, "disk": {"total": disk.total, "used": disk.used, "free": disk.free}, "load": os.getloadavg(), "cpu_count": os.cpu_count(), "services": {"llama": pid_alive("/workspace/llama/llama-server.pid"), "comfy": pid_alive("/workspace/comfy_h3.pid"), "tunnel": pid_alive("/workspace/llama/cloudflared.pid")}, "tunnel_url": tunnel}))
'''
    encoded = base64.b64encode(script.encode()).decode()
    output = remote.execute(f"python3 -c {shell('import base64;exec(base64.b64decode(' + repr(encoded) + '))')}", "VAST_STATUS_DONE", timeout=60)
    marker = "__VAST_JSON__"
    if marker not in output:
        raise VastError("원격 리소스 응답을 해석하지 못했습니다.")
    payload = output.rsplit(marker, 1)[1].split("VAST_STATUS_DONE", 1)[0].strip().splitlines()[0]
    data = json.loads(payload)
    data["jupyter_access_url"] = jupyter_access_url(load_settings().connection.jupyter_url)
    return data


def render_script(args: list[str], *, cwd: str | None = None, cuda: bool = False) -> str:
    lines = ["#!/usr/bin/env bash", "set -Eeuo pipefail", "umask 077", "ulimit -n 65535 2>/dev/null || true"]
    if cwd:
        lines.append(f"cd {shell(cwd)}")
    if cuda:
        lines += [
            "export CUDA_HOME=/usr/local/cuda-13.0",
            'export PATH="$CUDA_HOME/bin:$PATH"',
            'export LD_LIBRARY_PATH="$CUDA_HOME/lib64:$CUDA_HOME/targets/x86_64-linux/lib:${LD_LIBRARY_PATH:-}"',
            "export CUDA_DEVICE_ORDER=PCI_BUS_ID",
            "export MALLOC_ARENA_MAX=2",
        ]
    separator = " " + "\\" + "\n  "
    rendered = separator.join(
        shell(arg)
        if arg != "__API_KEY__"
        else '"$(tr -d \'\\r\\n\' < /workspace/llama/api_key.txt)"'
        for arg in args
    )
    lines.append("exec " + rendered)
    return "\n".join(lines) + "\n"


def start_llama(remote: JupyterClient, options: LlamaOptions) -> None:
    upload_api_key(remote)
    if not options.model.startswith("/workspace/llama/models/"):
        raise VastError("llama.cpp 모델은 /workspace/llama/models 아래만 허용됩니다.")
    if options.ubatch > options.batch:
        raise VastError("Ubatch는 Batch보다 클 수 없습니다.")
    if options.gpu_layers not in {"auto", "all"}:
        try: int(options.gpu_layers)
        except ValueError as exc: raise VastError("GPU layers는 auto, all 또는 정수여야 합니다.") from exc
    args = [
        "/workspace/llama/llama.cpp/build/bin/llama-server", "-m", options.model,
        "--host", options.host, "--port", str(options.port), "-ngl", options.gpu_layers,
        "-c", str(options.context), "--parallel", str(options.parallel),
        "--fit", "on" if options.fit else "off",
        "--cache-type-k", options.cache_k, "--cache-type-v", options.cache_v,
        "-b", str(options.batch), "-ub", str(options.ubatch),
        "--flash-attn", options.flash_attn, "--load-mode", options.load_mode, "--jinja",
        "--api-key", "__API_KEY__",
    ]
    if options.fit:
        args += ["--fit-target", str(options.fit_target)]
    if options.mtp:
        args += ["--spec-type", "draft-mtp", "--spec-draft-n-max", str(options.mtp_n)]
    if options.threads:
        args += ["--threads", str(options.threads)]
    if options.threads_batch:
        args += ["--threads-batch", str(options.threads_batch)]
    if options.numa != "disabled":
        args += ["--numa", options.numa]
    args += safe_extra_args(options.extra_args)
    remote.upload_bytes(render_script(args, cuda=True).encode(), "workspace/llama/start-qwen-managed.sh")
    remote.execute(
        "chmod +x /workspace/llama/start-qwen-managed.sh && bash -n /workspace/llama/start-qwen-managed.sh",
        "VAST_LLAMA_SCRIPT_OK",
    )
    command = (
        "if [ -s /workspace/llama/llama-server.pid ]; then kill $(cat /workspace/llama/llama-server.pid) 2>/dev/null || true; fi; "
        + f"fuser -k {options.port}/tcp 2>/dev/null || true; "
        + ": > /workspace/llama/llama-server.log; "
        + "nohup /workspace/llama/start-qwen-managed.sh >> /workspace/llama/llama-server.log 2>&1 & echo $! > /workspace/llama/llama-server.pid"
    )
    remote.execute(command, "VAST_LLAMA_SUBMITTED")
    time.sleep(2)
    state = remote.execute(
        "if [ -s /workspace/llama/llama-server.pid ] && kill -0 $(cat /workspace/llama/llama-server.pid) 2>/dev/null; then echo VAST_PROCESS_ALIVE; else echo VAST_PROCESS_DEAD; fi",
        "VAST_LLAMA_PROCESS_CHECK",
    )
    if "VAST_PROCESS_DEAD" in state:
        raise VastError("llama.cpp가 시작 직후 종료되었습니다:\n" + remote.read("workspace/llama/llama-server.log", tail=80))


def stop_remote_service(remote: JupyterClient, pid_file: str, port: int, marker: str) -> None:
    command = f"if [ -s {shell(pid_file)} ]; then kill $(cat {shell(pid_file)}) 2>/dev/null || true; fi; fuser -k {port}/tcp 2>/dev/null || true"
    remote.execute(command, marker)


def start_comfy(remote: JupyterClient, options: ComfyOptions) -> None:
    args = ["/venv/main/bin/python", "/workspace/ComfyUI/main.py", "--listen", options.host, "--port", str(options.port), "--enable-cors-header"]
    if options.vram_headroom:
        args += ["--vram-headroom", str(options.vram_headroom)]
    if options.reserve_vram is not None:
        args += ["--reserve-vram", str(options.reserve_vram)]
    if options.preview_method:
        args += ["--preview-method", options.preview_method]
    for enabled, flag in (
        (options.cache_none, "--cache-none"),
        (options.disable_smart_memory, "--disable-smart-memory"),
        (options.disable_async_offload, "--disable-async-offload"),
        (options.disable_pinned_memory, "--disable-pinned-memory"),
        (options.disable_xformers, "--disable-xformers"),
        (options.fast_disk, "--fast-disk"),
        (options.lowvram, "--lowvram"),
    ):
        if enabled:
            args.append(flag)
    if options.fast_fp16_accumulation:
        args += ["--fast", "fp16_accumulation"]
    args += safe_extra_args(options.extra_args)
    remote.upload_bytes(render_script(args, cwd="/workspace/ComfyUI", cuda=True).encode(), "workspace/start-comfy-managed.sh")
    remote.execute(
        "chmod +x /workspace/start-comfy-managed.sh && bash -n /workspace/start-comfy-managed.sh",
        "VAST_COMFY_SCRIPT_OK",
    )
    command = (
        "if [ -s /workspace/comfy_h3.pid ]; then kill $(cat /workspace/comfy_h3.pid) 2>/dev/null || true; fi; "
        + f"fuser -k {options.port}/tcp 2>/dev/null || true; "
        + ": > /workspace/comfy_h3.log; nohup /workspace/start-comfy-managed.sh >> /workspace/comfy_h3.log 2>&1 & echo $! > /workspace/comfy_h3.pid"
    )
    remote.execute(command, "VAST_COMFY_SUBMITTED")


def start_tunnel(remote: JupyterClient, options: TunnelOptions) -> str:
    remote.execute(
        "mkdir -p /workspace/llama; if [ ! -x /workspace/llama/cloudflared ]; then wget --tries=5 --timeout=30 https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O /workspace/llama/cloudflared && chmod +x /workspace/llama/cloudflared; fi",
        "VAST_CLOUDFLARED_READY", timeout=300,
    )
    remote.execute(
        "if [ -s /workspace/llama/cloudflared.pid ]; then kill $(cat /workspace/llama/cloudflared.pid) 2>/dev/null || true; fi; "
        f": > /workspace/llama/cloudflared.log; nohup /workspace/llama/cloudflared tunnel --url http://127.0.0.1:{options.port} --no-autoupdate >> /workspace/llama/cloudflared.log 2>&1 & echo $! > /workspace/llama/cloudflared.pid",
        "VAST_TUNNEL_SUBMITTED",
    )
    for _ in range(30):
        log = remote.read("workspace/llama/cloudflared.log", tail=80)
        matches = re.findall(r"https://[-a-z0-9]+\.trycloudflare\.com", log)
        if matches:
            url = matches[-1]
            if options.update_proxy:
                config = load_settings().connection
                update_env_file(Path(config.env_file), {"OPENAI_BASE_URL": url + "/v1"})
            return url
        time.sleep(1)
    raise VastError("Cloudflare 터널 주소 생성 시간이 초과되었습니다.")


def model_download(remote: JupyterClient, request: ModelDownload) -> dict[str, str]:
    parsed = urlparse(request.url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise VastError("올바른 http/https 모델 URL을 입력하세요.")
    if request.category not in MODEL_FOLDERS:
        raise VastError("지원하지 않는 모델 폴더입니다.")
    filename = Path(request.filename.strip() or unquote(Path(parsed.path).name)).name
    if not filename or filename in {".", ".."}:
        raise VastError("파일명을 확인하세요.")
    folder = MODEL_FOLDERS[request.category]
    destination = f"{folder}/{filename}"
    log_path = f"/workspace/model_download_{int(time.time())}.log"
    inner = (
        f"mkdir -p {shell(folder)}; wget --tries=8 --timeout=30 --retry-connrefused --continue {shell(request.url)} "
        f"-O {shell(destination + '.part')} && test -s {shell(destination + '.part')} && mv -f {shell(destination + '.part')} {shell(destination)} && echo MODEL_DOWNLOAD_DONE"
    )
    remote.execute(f"nohup bash -lc {shell(inner)} > {shell(log_path)} 2>&1 & echo $! > {shell(log_path + '.pid')}", "VAST_DOWNLOAD_SUBMITTED")
    return {"destination": destination, "log_path": log_path}


def stop_local_proxy() -> int:
    stopped = 0
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if "vast_qwen_proxy.py" in cmdline and proc.pid != os.getpid():
                proc.terminate(); stopped += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return stopped


def start_local_proxy() -> int:
    config = load_settings().connection
    proxy = Path(config.proxy_script)
    if not proxy.is_file():
        raise VastError(f"프록시 스크립트를 찾지 못했습니다: {proxy}")
    update_env_file(Path(config.env_file), {
        "PROXY_LISTEN_HOST": config.local_proxy_host,
        "PROXY_LISTEN_PORT": str(config.local_proxy_port),
        "PROXY_REQUEST_TIMEOUT": str(config.request_timeout),
        "PROXY_CONNECT_TIMEOUT": str(config.connect_timeout),
    })
    stop_local_proxy()
    stdout = open(BASE_DIR / "vast_qwen_proxy.log", "a", encoding="utf-8")
    stderr = open(BASE_DIR / "vast_qwen_proxy.err.log", "a", encoding="utf-8")
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen([sys.executable, str(proxy)], cwd=str(proxy.parent), stdout=stdout, stderr=stderr, creationflags=creationflags)
    stdout.close(); stderr.close()
    return process.pid


def api_error(exc: Exception) -> HTTPException:
    status = 400 if isinstance(exc, (VastError, ValueError)) else 502
    return HTTPException(status_code=status, detail=str(exc))


@router.get("/vast")
def vast_page():
    return FileResponse(BASE_DIR / "vast.html", headers={"Cache-Control": "no-store"})


@router.get("/api/vast/settings")
def get_vast_settings():
    data = _model_dump(load_settings())
    if data["connection"]["jupyter_url"]:
        try: data["connection"]["access_url"] = jupyter_access_url(data["connection"]["jupyter_url"])
        except Exception: data["connection"]["access_url"] = ""
    else:
        data["connection"]["access_url"] = ""
    data["model_folders"] = MODEL_FOLDERS
    return data


@router.post("/api/vast/settings")
def put_vast_settings(settings: SavedSettings):
    return _model_dump(save_settings(settings))


@router.post("/api/vast/connect")
def connect_vast(settings: SavedSettings):
    try:
        save_settings(settings)
        remote = JupyterClient(settings.connection.jupyter_url)
        return {"status": "connected", "resources": remote_status(remote), "access_url": jupyter_access_url(settings.connection.jupyter_url)}
    except Exception as exc:
        raise api_error(exc)


@router.get("/api/vast/status")
def vast_status():
    try: return remote_status(client())
    except Exception as exc: raise api_error(exc)


@router.post("/api/vast/llama/start")
def vast_llama_start(options: LlamaOptions):
    try:
        current = load_settings(); current.llama = options; save_settings(current)
        start_llama(client(), options)
        return {"status": "submitted", "message": "llama.cpp 시작 요청을 보냈습니다. 로그에서 로딩 상태를 확인하세요."}
    except Exception as exc: raise api_error(exc)


@router.post("/api/vast/llama/stop")
def vast_llama_stop():
    try:
        settings = load_settings(); stop_remote_service(client(), "/workspace/llama/llama-server.pid", settings.llama.port, "VAST_LLAMA_STOPPED")
        return {"status": "stopped"}
    except Exception as exc: raise api_error(exc)


@router.post("/api/vast/comfy/start")
def vast_comfy_start(options: ComfyOptions):
    try:
        current = load_settings(); current.comfy = options; save_settings(current)
        start_comfy(client(), options)
        return {"status": "submitted", "message": "ComfyUI 시작 요청을 보냈습니다. 로그에서 로딩 상태를 확인하세요."}
    except Exception as exc: raise api_error(exc)


@router.post("/api/vast/comfy/stop")
def vast_comfy_stop():
    try:
        settings = load_settings(); stop_remote_service(client(), "/workspace/comfy_h3.pid", settings.comfy.port, "VAST_COMFY_STOPPED")
        return {"status": "stopped"}
    except Exception as exc: raise api_error(exc)


@router.post("/api/vast/tunnel/start")
def vast_tunnel_start(options: TunnelOptions):
    try: return {"status": "started", "url": start_tunnel(client(), options)}
    except Exception as exc: raise api_error(exc)


@router.post("/api/vast/tunnel/stop")
def vast_tunnel_stop():
    try:
        remote = client(); remote.execute("if [ -s /workspace/llama/cloudflared.pid ]; then kill $(cat /workspace/llama/cloudflared.pid) 2>/dev/null || true; fi", "VAST_TUNNEL_STOPPED")
        return {"status": "stopped"}
    except Exception as exc: raise api_error(exc)


@router.post("/api/vast/models/download")
def vast_model_download(request: ModelDownload):
    try: return {"status": "submitted", **model_download(client(), request)}
    except Exception as exc: raise api_error(exc)


@router.get("/api/vast/logs/{service}")
def vast_logs(service: Literal["llama", "comfy", "tunnel", "setup_llama", "setup_h3"]):
    paths = {"llama": "workspace/llama/llama-server.log", "comfy": "workspace/comfy_h3.log", "tunnel": "workspace/llama/cloudflared.log", "setup_llama": "workspace/setup_llama.log", "setup_h3": "workspace/setup_h3.log"}
    try: return {"log": clean_terminal(client().read(paths[service], tail=250))}
    except Exception as exc: raise api_error(exc)


@router.get("/api/vast/download-log")
def vast_download_log(path: str):
    if not re.fullmatch(r"/workspace/model_download_\d+\.log", path):
        raise HTTPException(status_code=400, detail="허용되지 않는 로그 경로입니다.")
    try: return {"log": clean_terminal(client().read(path.lstrip("/"), tail=100))}
    except Exception as exc: raise api_error(exc)


@router.post("/api/vast/proxy/{action}")
def vast_proxy(action: Literal["start", "stop", "restart", "test"]):
    try:
        if action == "stop": return {"status": "stopped", "count": stop_local_proxy()}
        if action == "restart": stop_local_proxy()
        if action in {"start", "restart"}: return {"status": "started", "pid": start_local_proxy()}
        config = load_settings().connection
        key = read_env_value(Path(config.env_file), "OPENAI_API_KEY")
        response = requests.get(f"http://127.0.0.1:{config.local_proxy_port}/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=30)
        response.raise_for_status()
        return {"status": "ok", "models": response.json().get("data", [])}
    except Exception as exc: raise api_error(exc)


@router.post("/api/vast/setup")
def vast_setup(options: SetupOptions):
    try:
        remote = client()
        h3_zip, h3_script = Path(options.h3_zip), Path(options.h3_script)
        llama_script = BASE_DIR / "vast_setup_llama.sh"
        for path in (h3_zip, h3_script, llama_script):
            if not path.is_file(): raise VastError(f"설치 파일을 찾지 못했습니다: {path}")
        upload_api_key(remote)
        remote.upload_file(h3_zip, "workspace/h3_custom_nodes.zip")
        remote.upload_file(h3_script, "workspace/setup_h3_final_v2.sh")
        remote.upload_file(llama_script, "workspace/setup_llama_qwen.sh")
        preflight = "export DEBIAN_FRONTEND=noninteractive; apt-get update && apt-get install -y git wget curl ca-certificates cmake ninja-build build-essential cuda-compiler-13-0 cuda-cudart-dev-13-0 libcublas-dev-13-0 libcusparse-dev-13-0 libcusolver-dev-13-0"
        remote.execute(preflight, "VAST_PREFLIGHT_DONE", timeout=1800)
        remote.execute("chmod +x /workspace/setup_h3_final_v2.sh /workspace/setup_llama_qwen.sh; nohup bash /workspace/setup_h3_final_v2.sh > /workspace/setup_h3.log 2>&1 & nohup bash /workspace/setup_llama_qwen.sh > /workspace/setup_llama.log 2>&1 &", "VAST_SETUP_SUBMITTED")
        return {"status": "submitted", "message": "ComfyUI H3와 llama.cpp 설치를 동시에 시작했습니다."}
    except Exception as exc: raise api_error(exc)
