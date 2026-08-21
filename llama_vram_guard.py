"""Public llama.cpp proxy that frees ComfyUI VRAM before human inference.

The public llama.cpp UI/API stays on 8080.  The managed llama-server runs on
8082 so ComfyUI custom nodes can call it directly without waiting on their own
queue and deadlocking.
"""

import asyncio
import json
import logging
import subprocess
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask


TRIGGER_PATHS = {
    "/completion",
    "/completions",
    "/chat/completions",
    "/v1/completions",
    "/v1/chat/completions",
    "/responses",
    "/v1/responses",
    "/infill",
    "/embedding",
    "/embeddings",
    "/v1/embeddings",
    "/rerank",
    "/reranking",
    "/v1/rerank",
    "/v1/reranking",
    "/v1/messages",
    "/tokenize",
    "/detokenize",
    "/apply-template",
    "/chat/completions/input_tokens",
    "/v1/chat/completions/input_tokens",
    "/responses/input_tokens",
    "/v1/responses/input_tokens",
    "/v1/messages/count_tokens",
}
HOP_BY_HOP_HEADERS = {
    "connection",
    "content-encoding",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
LLAMA_IDLE_VRAM_LIMIT_MB = 1024


def scan_llama_vram_usage():
    """Return compute-memory usage for local llama-server processes.

    PyTorch's allocator only reports memory owned by ComfyUI, so a handoff
    must use the driver view to detect memory that is still owned by
    llama.cpp.  A sleeping llama-server keeps a small CUDA context alive.
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,process_name,used_gpu_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"nvidia-smi 실행 실패: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "nvidia-smi failed").strip()
        raise RuntimeError(detail)

    usages = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.rsplit(",", 2)]
        if len(parts) != 3 or "llama-server" not in parts[1].lower():
            continue
        try:
            usages.append({"pid": int(parts[0]), "process_name": parts[1], "used_memory_mb": int(parts[2])})
        except ValueError:
            continue
    return usages


def create_guard_app(
    *,
    backend_url="http://127.0.0.1:8082",
    comfy_url="http://127.0.0.1:8188",
    settings_file=None,
):
    app = FastAPI(title="llama.cpp VRAM guard", docs_url=None, redoc_url=None)
    app.state.backend_url = backend_url.rstrip("/")
    app.state.comfy_url = comfy_url.rstrip("/")
    app.state.settings_file = Path(settings_file) if settings_file else None
    app.state.cleanup_lock = asyncio.Lock()
    app.state.http = None

    @app.on_event("startup")
    async def startup():
        app.state.http = httpx.AsyncClient(timeout=None, follow_redirects=False)

    @app.on_event("shutdown")
    async def shutdown():
        if app.state.http is not None:
            await app.state.http.aclose()

    def cleanup_enabled():
        path = app.state.settings_file
        if path is None or not path.exists():
            return True
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("vram_cleanup_enabled", True) is not False
        except Exception:
            return True

    async def wait_for_comfy_idle():
        """Return False when ComfyUI is offline; otherwise wait for its queue."""
        client = app.state.http
        try:
            response = await client.get(f"{app.state.comfy_url}/queue", timeout=3)
            response.raise_for_status()
        except (httpx.HTTPError, ValueError):
            return False

        while True:
            data = response.json()
            running = data.get("queue_running") or []
            pending = data.get("queue_pending") or []
            if not running and not pending:
                return True
            await asyncio.sleep(2)
            try:
                response = await client.get(f"{app.state.comfy_url}/queue", timeout=5)
                response.raise_for_status()
            except httpx.HTTPError:
                return False

    async def release_comfy_vram():
        async with app.state.cleanup_lock:
            if not await wait_for_comfy_idle():
                return {"comfy_online": False, "released": False}
            try:
                response = await app.state.http.post(
                    f"{app.state.comfy_url}/free",
                    json={"unload_models": True, "free_memory": True},
                    timeout=30,
                )
                return {"comfy_online": True, "released": response.is_success}
            except httpx.HTTPError:
                return {"comfy_online": True, "released": False}

    @app.get("/__vram_guard_health")
    async def health():
        return {
            "status": "ok",
            "comfy": app.state.comfy_url,
            "llama_backend": app.state.backend_url,
            "cleanup_enabled": cleanup_enabled(),
        }

    @app.get("/__vram_guard/wait-backend-release")
    async def wait_backend_release(timeout: float = 60, max_used_mb: int = LLAMA_IDLE_VRAM_LIMIT_MB):
        """Block ComfyUI's next GPU step until llama.cpp has really unloaded."""
        timeout = min(120.0, max(1.0, float(timeout)))
        max_used_mb = min(4096, max(64, int(max_used_mb)))
        deadline = asyncio.get_running_loop().time() + timeout
        last_usages = []
        while True:
            try:
                last_usages = await asyncio.to_thread(scan_llama_vram_usage)
            except RuntimeError as error:
                return JSONResponse(
                    {"released": False, "error": str(error)},
                    status_code=503,
                )
            if not last_usages or all(item["used_memory_mb"] <= max_used_mb for item in last_usages):
                return {
                    "released": True,
                    "max_used_mb": max_used_mb,
                    "llama_processes": last_usages,
                }
            if asyncio.get_running_loop().time() >= deadline:
                return JSONResponse(
                    {
                        "released": False,
                        "error": "llama.cpp가 제한 시간 안에 VRAM을 반환하지 않았습니다",
                        "max_used_mb": max_used_mb,
                        "llama_processes": last_usages,
                    },
                    status_code=503,
                )
            await asyncio.sleep(0.25)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def proxy(path: str, request: Request):
        request_path = "/" + path
        if request.method == "POST" and request_path in TRIGGER_PATHS and cleanup_enabled():
            await release_comfy_vram()

        target = f"{app.state.backend_url}{request_path}"
        if request.url.query:
            target += "?" + request.url.query
        headers = {key: value for key, value in request.headers.items() if key.lower() not in HOP_BY_HOP_HEADERS}
        try:
            upstream_request = app.state.http.build_request(
                request.method,
                target,
                headers=headers,
                content=await request.body(),
            )
            upstream = await app.state.http.send(upstream_request, stream=True)
        except httpx.HTTPError as error:
            return JSONResponse({"error": f"llama-server 접근 실패: {error}"}, status_code=502)

        response_headers = {
            key: value for key, value in upstream.headers.items() if key.lower() not in HOP_BY_HOP_HEADERS
        }
        return StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=response_headers,
            background=BackgroundTask(upstream.aclose),
        )

    return app


def run_guard(*, host="0.0.0.0", port=8080, backend_port=8082, comfy_port=8188, settings_file=None):
    app = create_guard_app(
        backend_url=f"http://127.0.0.1:{backend_port}",
        comfy_url=f"http://127.0.0.1:{comfy_port}",
        settings_file=settings_file,
    )
    logging.getLogger("uvicorn.error").info(
        "llama VRAM guard listening on %s:%s -> 127.0.0.1:%s", host, port, backend_port
    )
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run_guard()
