import os
import re
import shutil
import threading
import time
import uuid

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import FileResponse

from config import IS_WINDOWS, settings

router = APIRouter(prefix="/api", tags=["model-hub"])

# ---------- 파일 탐색기 ----------

MAX_ENTRIES = 500


def _roots():
    if IS_WINDOWS:
        drives = []
        import string

        for letter in string.ascii_uppercase:
            d = f"{letter}:\\"
            if os.path.exists(d):
                drives.append(d)
        return drives
    roots = ["/", "/mnt", "/opt", "/home"]
    for key in ("model_root", "comfyui_dir", "bot_dir", "watcher_dir"):
        p = settings.get(key)
        if p and p not in roots and os.path.exists(p):
            roots.append(p)
    return roots


@router.get("/fs/roots")
def fs_roots():
    roots = _roots()
    items = []
    for r in roots:
        try:
            st = os.stat(r)
            items.append(
                {
                    "name": r if not r.endswith(":\\") else r,
                    "path": r,
                    "is_dir": True,
                    "size": 0,
                    "mtime": int(st.st_mtime),
                }
            )
        except OSError:
            items.append({"name": r, "path": r, "is_dir": True, "size": 0, "mtime": 0})
    return {"roots": items}


def _safe_path(path):
    p = os.path.abspath(path)
    if not os.path.exists(p):
        raise FileNotFoundError(path)
    return p


@router.get("/fs/list")
def fs_list(path: str = "/"):
    p = _safe_path(path)
    if not os.path.isdir(p):
        raise FileNotFoundError(f"디렉터리가 아닙니다: {p}")
    try:
        names = os.listdir(p)
    except PermissionError:
        raise PermissionError(f"권한 없음: {p}")
    entries = []
    for name in names:
        if name.startswith("."):
            continue
        full = os.path.join(p, name)
        try:
            st = os.stat(full)
            entries.append(
                {
                    "name": name,
                    "path": full,
                    "is_dir": os.path.isdir(full),
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                }
            )
        except OSError:
            continue
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return {"path": p, "parent": os.path.dirname(p), "entries": entries[:MAX_ENTRIES], "truncated": len(entries) > MAX_ENTRIES}


@router.post("/fs/mkdir")
def fs_mkdir(path: str, name: str):
    target = os.path.join(path, name)
    if os.path.exists(target):
        raise FileExistsError(target)
    os.makedirs(target, exist_ok=True)
    return {"ok": True, "path": target}


@router.delete("/fs/delete")
def fs_delete(path: str):
    p = _safe_path(path)
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
    else:
        os.remove(p)
    return {"ok": True}


@router.post("/fs/rename")
def fs_rename(path: str, new_name: str):
    p = _safe_path(path)
    new_path = os.path.join(os.path.dirname(p), new_name)
    os.rename(p, new_path)
    return {"ok": True, "path": new_path}


@router.post("/fs/upload")
async def fs_upload(path: str = Form(...), file: UploadFile = File(...)):
    target = os.path.join(path, os.path.basename(file.filename or "file"))
    with open(target, "wb") as out:
        while chunk := await file.read(1024 * 256):
            out.write(chunk)
    return {"ok": True, "path": target, "size": os.path.getsize(target)}


@router.get("/fs/download")
def fs_download(path: str):
    p = _safe_path(path)
    return FileResponse(p, filename=os.path.basename(p))


# ---------- 모델 다운로더 ----------

JOBS = {}
LOCK = threading.Lock()
MAX_JOBS = 20


def _fmt(n):
    if n is None:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    for u in units:
        if v < 1024:
            return f"{v:.1f} {u}"
        v /= 1024
    return f"{v:.1f} PB"


def _human_url(url):
    return url if len(url) <= 90 else url[:87] + "..."


def _hf_id(url):
    m = re.search(r"(?:huggingface\.co|hf\.co)/([^/\s]+)/([^/\s?#]+)", url)
    if not m:
        return None
    repo = f"{m.group(1)}/{m.group(2)}"
    rest = url[m.end() :]
    rest = rest.split("?")[0].split("#")[0]
    rest = rest.lstrip("/")
    if rest.startswith("resolve/"):
        parts = rest.split("/", 2)
        rest = parts[2] if len(parts) > 2 else ""
    return repo, rest


def _civitai_info(url):
    if "civitai.com/api/download/models/" in url:
        return {"type": "civitai", "model_id": url.rstrip("/").rsplit("/", 1)[-1]}
    m = re.search(r"civitai\.com/models/(\d+)", url)
    if m:
        return {"type": "civitai-model", "model_id": m.group(1)}
    m = re.search(r"civitai\.com/api/models/(\d+)", url)
    if m:
        return {"type": "civitai-model", "model_id": m.group(1)}
    return None


def _parse_url(url):
    if not url.startswith(("http://", "https://")):
        raise ValueError("URL이 올바르지 않습니다")
    hf = _hf_id(url)
    if hf:
        return {"type": "hf", "repo": hf[0], "file": hf[1], "url": url}
    civ = _civitai_info(url)
    if civ:
        return {"type": civ["type"], "model_id": civ["model_id"], "url": url}
    return {"type": "direct", "url": url}


@router.get("/models/roots")
def models_roots():
    roots = []
    model_root = settings.get("model_root")
    if model_root and os.path.exists(model_root):
        roots.append({"path": model_root, "label": "model_root (llama)"})
    comfy = os.path.join(settings.get("comfyui_dir"), "models")
    if comfy and os.path.exists(comfy):
        roots.append({"path": comfy, "label": "ComfyUI/models"})
    home = os.path.expanduser("~")
    roots.append({"path": home, "label": "홈"})
    for r in _roots():
        if r not in [x["path"] for x in roots]:
            roots.append({"path": r, "label": r})
    return {"roots": roots}


@router.get("/models/jobs")
def models_jobs():
    with LOCK:
        jobs = sorted(JOBS.values(), key=lambda j: j["created"], reverse=True)
    return {"jobs": jobs}


@router.get("/models/parse")
def models_parse(url: str):
    try:
        return _parse_url(url)
    except ValueError as e:
        return {"error": str(e)}


def _download_worker(job):
    try:
        info = _parse_url(job["url"])
        dest_dir = job["dest_dir"]
        os.makedirs(dest_dir, exist_ok=True)
        if info["type"] == "hf":
            _dl_hf(job, info)
        elif info["type"] in ("civitai", "civitai-model", "direct"):
            _dl_stream(job, info["url"])
        if job["status"] in ("downloading", "queued"):
            job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


def _dl_hf(job, info):
    try:
        from huggingface_hub import HfApi, hf_hub_download
    except ImportError:
        job["status"] = "error"
        job["error"] = "huggingface_hub 미설치 — pip install huggingface_hub"
        return
    api = HfApi()
    repo, sub = info["repo"], info["file"]
    if sub:
        files = [sub]
        job["filename"] = os.path.basename(sub)
    else:
        files = api.list_repo_files(repo)
        job["filename"] = f"{repo} (리포 전체 {len(files)}개)"
    total = len(files)
    for i, fname in enumerate(files, 1):
        if job["status"] == "cancelled":
            job["status"] = "cancelled"
            job["error"] = None
            return
        job["progress"] = i / total
        job["status_text"] = f"[{i}/{total}] {fname}"
        target = os.path.join(job["dest_dir"], fname)
        try:
            hf_hub_download(
                repo_id=repo,
                filename=fname,
                local_dir=job["dest_dir"],
                local_dir_use_symlinks=False,
                resume_download=True,
            )
        except Exception as e:
            job["error"] = f"{fname}: {e}"
            job["status"] = "error"
            return
    job["done_bytes"] = total


def _dl_stream(job, url):
    import httpx

    job["filename"] = os.path.basename(url.split("?")[0]) or "download"
    target = os.path.join(job["dest_dir"], job["filename"])
    tmp = target + ".part"
    headers = {}
    if "civitai" in url:
        headers["User-Agent"] = "main_server/1.0"
    resume = 0
    if os.path.exists(tmp):
        resume = os.path.getsize(tmp)
        headers["Range"] = f"bytes={resume}-"
    with httpx.stream("GET", url, headers=headers, timeout=None, follow_redirects=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        if r.status_code == 206:
            total += resume
        job["total_bytes"] = total if total > resume else total
        job["done_bytes"] = resume
        job["status_text"] = "다운로드 중..."
        last = time.time()
        with open(tmp, "ab") as f:
            for chunk in r.iter_bytes(65536):
                if job["status"] == "cancelled":
                    job["status"] = "cancelled"
                    return
                f.write(chunk)
                job["done_bytes"] += len(chunk)
                now = time.time()
                if now - last > 0.5:
                    last = now
                    job["status_text"] = f"{_fmt(job['done_bytes'])} / {_fmt(job['total_bytes'])}"
    os.replace(tmp, target)


@router.post("/models/download")
def models_download(url: str, dest_dir: str = ""):
    dest_dir = dest_dir or settings.get("model_root")
    if not os.path.isdir(dest_dir):
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError:
            raise FileNotFoundError(f"대상 폴더를 만들 수 없습니다: {dest_dir}")
    job = {
        "id": uuid.uuid4().hex[:8],
        "url": url,
        "display_url": _human_url(url),
        "dest_dir": dest_dir,
        "filename": None,
        "status": "queued",
        "status_text": "대기 중",
        "total_bytes": None,
        "done_bytes": 0,
        "speed": None,
        "eta": None,
        "error": None,
        "created": time.time(),
        "progress": 0,
    }
    with LOCK:
        JOBS[job["id"]] = job
        if len(JOBS) > MAX_JOBS:
            for k in sorted(JOBS, key=lambda x: JOBS[x]["created"])[: len(JOBS) - MAX_JOBS]:
                del JOBS[k]
    threading.Thread(target=_download_worker, args=(job,), daemon=True).start()
    return {"ok": True, "job": job}


@router.post("/models/cancel")
def models_cancel(id: str):
    with LOCK:
        job = JOBS.get(id)
    if job:
        job["status"] = "cancelled"
        job["status_text"] = "취소됨"
    return {"ok": True}