"""Public llama.cpp proxy that frees ComfyUI VRAM before human inference.

The public llama.cpp UI/API stays on 8080.  The managed llama-server runs on
8082 so ComfyUI custom nodes can call it directly without waiting on their own
queue and deadlocking.
"""

import asyncio
import json
import logging
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
