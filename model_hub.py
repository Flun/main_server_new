"""Model Hub API: model downloads plus a local Ubuntu filesystem browser."""

import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from config import BASE_DIR, settings

router = APIRouter(prefix="/api/models", tags=["model-hub"])

MODEL_EXTENSIONS = {
    ".gguf", ".safetensors", ".ckpt", ".pt", ".pth", ".bin",
    ".onnx", ".json", ".yaml", ".yml", ".txt", ".model",
}
PRIMARY_MODEL_EXTENSIONS = {".gguf", ".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".onnx"}
MAX_INSPECT_FILES = 2500
MAX_INSTALLED_FILES = 3000
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


class InspectRequest(BaseModel):
    url: str


class DownloadFile(BaseModel):
    name: str
    url: str | None = None
    size: int | None = None


class DownloadRequest(BaseModel):
    source: str
    repo: str | None = None
    revision: str = "main"
    files: list[DownloadFile] = Field(min_length=1, max_length=100)
    destination: str


class FolderRequest(BaseModel):
    parent: str
    name: str


class FsPathRequest(BaseModel):
    path: str


class FsCreateRequest(BaseModel):
    parent: str
    name: str


class FsRenameRequest(BaseModel):
    path: str
    name: str


class FsMoveRequest(BaseModel):
    path: str
    destination: str


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _fs_path(raw: str, *, must_exist: bool = True) -> Path:
    if not str(raw or "").strip():
        raise HTTPException(400, "경로를 입력하세요")
    try:
        path = Path(raw).expanduser().resolve(strict=must_exist)
    except FileNotFoundError as error:
        raise HTTPException(404, f"경로가 없습니다: {raw}") from error
    except (OSError, RuntimeError) as error:
        raise HTTPException(400, f"경로를 해석하지 못했습니다: {error}") from error
    return path


def _fs_entry_path(raw: str) -> Path:
    """Resolve the parent, but keep the final symlink as the managed entry."""
    source = Path(str(raw or "")).expanduser()
    if not str(raw or "").strip() or source.name in {"", ".", ".."}:
        raise HTTPException(400, "관리할 항목 경로가 올바르지 않습니다")
    try:
        target = source.parent.resolve(strict=True) / source.name
        target.lstat()
    except FileNotFoundError as error:
        raise HTTPException(404, f"경로가 없습니다: {raw}") from error
    except OSError as error:
        raise HTTPException(400, f"경로를 해석하지 못했습니다: {error}") from error
    return target


def _leaf_name(name: str) -> str:
    value = str(name or "").strip()
    if not value or value in {".", ".."} or "/" in value or "\\" in value or "\0" in value:
        raise HTTPException(400, "파일 또는 폴더 이름이 올바르지 않습니다")
    return value


def _fs_roots() -> list[dict[str, str]]:
    candidates = [
        ("파일 시스템", Path("/")), ("내 홈", Path.home()),
        ("서비스", Path("/srv")), ("마운트", Path("/mnt")),
        ("외장/추가 볼륨", Path("/media") / os.environ.get("USER", "flux")),
        ("임시 폴더", Path("/tmp")),
    ]
    result, seen = [], set()
    for label, path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not resolved.exists() or str(resolved) in seen:
            continue
        seen.add(str(resolved))
        result.append({"label": label, "path": str(resolved)})
    return result


def _protected_trash_target(path: Path) -> bool:
    protected = {
        Path("/"), Path.home(), Path("/home"), Path("/usr"), Path("/etc"),
        Path("/var"), Path("/opt"), Path("/srv"), Path("/mnt"), Path("/media"), Path("/tmp"),
    }
    protected.add(Path(BASE_DIR).resolve())
    return path in protected


def _base_roots() -> list[dict[str, str]]:
    llama_root = settings.get("model_root")
    try:
        with open(Path(BASE_DIR) / "llama_settings.json", encoding="utf-8") as settings_file:
            saved = json.load(settings_file)
        llama_root = saved.get("model_root") or llama_root
    except (OSError, ValueError, TypeError):
        pass
    candidates = [
        ("llama.cpp 모델", llama_root),
        ("ComfyUI 모델", Path(settings.get("comfyui_dir")) / "models"),
    ]
    roots = []
    seen = set()
    for label, raw in candidates:
        if not raw:
            continue
        path = _resolved(raw)
        key = str(path)
        if key in seen or not path.is_dir():
            continue
        seen.add(key)
        roots.append({"label": label, "path": key})
    return roots


def _assert_model_path(path: str, *, must_exist: bool = True) -> Path:
    target = _resolved(path)
    allowed = [_resolved(item["path"]) for item in _base_roots()]
    if not allowed or not any(target == root or root in target.parents for root in allowed):
        raise HTTPException(400, "모델 루트 폴더 밖의 경로는 사용할 수 없습니다")
    if must_exist and not target.is_dir():
        raise HTTPException(400, f"대상 폴더가 없습니다: {target}")
    return target


def _destination_items() -> list[dict[str, Any]]:
    items = []
    for root in _base_roots():
        base = _resolved(root["path"])
        folders = [base]
        try:
            folders += sorted(
                (p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")),
                key=lambda p: p.name.lower(),
            )
        except OSError:
            pass
        for folder in folders:
            try:
                free = shutil.disk_usage(folder).free
            except OSError:
                free = None
            leaf = "root" if folder == base else folder.name.replace("_", " ")
            items.append({
                "label": f"{root['label']} / {leaf}",
                "path": str(folder),
                "root": str(base),
                "free_bytes": free,
            })
    return items


def _safe_relative_file(name: str) -> str:
    value = unquote(str(name or "")).replace("\\", "/").lstrip("/")
    parts = [part for part in value.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"안전하지 않은 파일 경로입니다: {name}")
    return "/".join(parts)


def _hf_source(url: str) -> dict[str, str] | None:
    parsed = urlparse(url)
    if parsed.hostname not in {"huggingface.co", "www.huggingface.co", "hf.co"}:
        return None
    parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        raise ValueError("Hugging Face 주소에는 소유자와 저장소 이름이 필요합니다")
    repo = "/".join(parts[:2])
    revision = "main"
    filename = ""
    if len(parts) >= 4 and parts[2] in {"resolve", "blob", "tree"}:
        revision = parts[3]
        if parts[2] != "tree" and len(parts) > 4:
            filename = "/".join(parts[4:])
    return {"source": "huggingface", "repo": repo, "revision": revision, "filename": filename}


def _civitai_source(url: str) -> dict[str, str] | None:
    parsed = urlparse(url)
    if parsed.hostname not in {"civitai.com", "www.civitai.com"}:
        return None
    query = parse_qs(parsed.query)
    if query.get("modelVersionId"):
        return {"source": "civitai", "version_id": query["modelVersionId"][0]}
    for pattern in (r"/api/download/models/(\d+)", r"/api/v1/model-versions/(\d+)"):
        match = re.search(pattern, parsed.path)
        if match:
            return {"source": "civitai", "version_id": match.group(1)}
    match = re.search(r"/models/(\d+)", parsed.path)
    if match:
        return {"source": "civitai", "model_id": match.group(1)}
    raise ValueError("지원되는 Civitai 모델 또는 버전 주소가 아닙니다")


def _hf_inspect(source: dict[str, str]) -> dict[str, Any]:
    try:
        from huggingface_hub import HfApi
        from huggingface_hub.hf_api import RepoFile
    except ImportError as error:
        raise RuntimeError("huggingface_hub 패키지가 필요합니다") from error
    entries = HfApi().list_repo_tree(
        source["repo"], recursive=True, expand=False, revision=source["revision"]
    )
    files = []
    wanted = source.get("filename")
    for entry in entries:
        if not isinstance(entry, RepoFile):
            continue
        name = entry.path
        if wanted and name != wanted:
            continue
        suffix = Path(name).suffix.lower()
        files.append({
            "name": name,
            "size": entry.size,
            "kind": suffix.lstrip(".") or "file",
            "recommended": bool(wanted) or suffix in PRIMARY_MODEL_EXTENSIONS,
        })
        if len(files) >= MAX_INSPECT_FILES:
            break
    if wanted and not files:
        raise ValueError(f"저장소에서 파일을 찾지 못했습니다: {wanted}")
    if not files:
        raise ValueError("저장소에 다운로드할 파일이 없습니다")
    return {
        "source": "huggingface",
        "title": source["repo"],
        "subtitle": f"revision: {source['revision']}",
        "repo": source["repo"],
        "revision": source["revision"],
        "files": files,
        "truncated": len(files) >= MAX_INSPECT_FILES,
    }


def _civitai_version_files(version: dict[str, Any], model_name: str = "") -> list[dict[str, Any]]:
    files = []
    version_files = version.get("files") or []
    for item in version_files:
        name = item.get("name") or f"civitai-{version.get('id')}.safetensors"
        metadata = item.get("metadata") or {}
        files.append({
            "name": name,
            "url": item.get("downloadUrl") or version.get("downloadUrl"),
            "size": int(float(item.get("sizeKB") or 0) * 1024) or None,
            "kind": metadata.get("format") or item.get("type") or "model",
            "recommended": bool(item.get("primary")) or len(version_files) == 1,
            "version": version.get("name"),
            "model": model_name or (version.get("model") or {}).get("name"),
            "virus_scan": item.get("virusScanResult"),
            "pickle_scan": item.get("pickleScanResult"),
        })
    return files


def _civitai_inspect(source: dict[str, str]) -> dict[str, Any]:
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        if source.get("version_id"):
            response = client.get(f"https://civitai.com/api/v1/model-versions/{source['version_id']}")
            response.raise_for_status()
            version = response.json()
            model_name = (version.get("model") or {}).get("name") or f"Civitai {source['version_id']}"
            files = _civitai_version_files(version, model_name)
            subtitle = f"{version.get('name') or 'version'} · {version.get('baseModel') or 'base model unknown'}"
        else:
            response = client.get(f"https://civitai.com/api/v1/models/{source['model_id']}")
            response.raise_for_status()
            model = response.json()
            model_name = model.get("name") or f"Civitai {source['model_id']}"
            versions = model.get("modelVersions") or []
            files = []
            for version in versions:
                files.extend(_civitai_version_files(version, model_name))
            subtitle = f"{model.get('type') or 'model'} · {len(versions)} versions"
    if not files:
        raise ValueError("Civitai API에서 다운로드 가능한 파일을 찾지 못했습니다")
    return {"source": "civitai", "title": model_name, "subtitle": subtitle, "files": files}


def _filename_from_headers(response: httpx.Response, fallback_url: str) -> str:
    disposition = response.headers.get("content-disposition", "")
    match = re.search(r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)", disposition, re.I)
    if match:
        return Path(unquote(match.group(1) or match.group(2))).name
    return Path(unquote(urlparse(str(response.url or fallback_url)).path)).name or "download.bin"


def _direct_inspect(url: str) -> dict[str, Any]:
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        response = client.head(url)
        if response.status_code >= 400 or response.status_code == 405:
            response = client.get(url, headers={"Range": "bytes=0-0"})
        response.raise_for_status()
        name = _filename_from_headers(response, url)
        size = response.headers.get("content-range", "").rsplit("/", 1)[-1]
        if not size.isdigit():
            size = response.headers.get("content-length")
        size = int(size) if str(size or "").isdigit() else None
    return {
        "source": "direct",
        "title": name,
        "subtitle": urlparse(url).hostname,
        "files": [{
            "name": name, "url": url, "size": size,
            "kind": Path(name).suffix.lstrip(".") or "file", "recommended": True,
        }],
    }


@router.get("/destinations")
def destinations():
    return {"destinations": _destination_items()}


@router.get("/fs/roots")
def filesystem_roots():
    return {"roots": _fs_roots(), "home": str(Path.home())}


@router.get("/fs/list")
def filesystem_list(path: str = "~", show_hidden: bool = False):
    current = _fs_path(path)
    if not current.is_dir():
        raise HTTPException(400, "폴더 경로가 아닙니다")
    entries = []
    try:
        for item in current.iterdir():
            if not show_hidden and item.name.startswith("."):
                continue
            try:
                stat = item.stat(follow_symlinks=False)
                is_dir = item.is_dir()
                entries.append({
                    "name": item.name, "path": str(item), "is_dir": is_dir,
                    "is_symlink": item.is_symlink(), "size": None if is_dir else stat.st_size,
                    "mtime": int(stat.st_mtime), "readable": os.access(item, os.R_OK),
                    "writable": os.access(item, os.W_OK),
                })
            except OSError:
                entries.append({
                    "name": item.name, "path": str(item), "is_dir": False,
                    "is_symlink": item.is_symlink(), "size": None, "mtime": None,
                    "readable": False, "writable": False,
                })
            if len(entries) >= 2000:
                break
    except PermissionError as error:
        raise HTTPException(403, f"이 폴더를 읽을 권한이 없습니다: {current}") from error
    except OSError as error:
        raise HTTPException(400, f"폴더를 읽지 못했습니다: {error}") from error
    entries.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
    parent = str(current.parent) if current != current.parent else None
    return {
        "path": str(current), "parent": parent, "entries": entries,
        "truncated": len(entries) >= 2000, "writable": os.access(current, os.W_OK),
    }


@router.post("/fs/mkdir")
def filesystem_mkdir(request: FsCreateRequest):
    parent = _fs_path(request.parent)
    if not parent.is_dir():
        raise HTTPException(400, "상위 경로가 폴더가 아닙니다")
    target = parent / _leaf_name(request.name)
    try:
        target.mkdir()
    except FileExistsError as error:
        raise HTTPException(409, "같은 이름의 항목이 이미 있습니다") from error
    except PermissionError as error:
        raise HTTPException(403, "이 위치에 폴더를 만들 권한이 없습니다") from error
    return {"ok": True, "path": str(target)}


@router.post("/fs/rename")
def filesystem_rename(request: FsRenameRequest):
    source = _fs_entry_path(request.path)
    target = source.with_name(_leaf_name(request.name))
    if target.exists():
        raise HTTPException(409, "같은 이름의 항목이 이미 있습니다")
    try:
        source.rename(target)
    except PermissionError as error:
        raise HTTPException(403, "이 항목의 이름을 바꿀 권한이 없습니다") from error
    except OSError as error:
        raise HTTPException(400, f"이름 변경 실패: {error}") from error
    return {"ok": True, "path": str(target)}


@router.post("/fs/move")
def filesystem_move(request: FsMoveRequest):
    source = _fs_entry_path(request.path)
    destination = _fs_path(request.destination)
    if not destination.is_dir():
        raise HTTPException(400, "이동 대상은 폴더여야 합니다")
    target = destination / source.name
    if target.exists():
        raise HTTPException(409, "대상 폴더에 같은 이름의 항목이 있습니다")
    try:
        shutil.move(str(source), str(target))
    except PermissionError as error:
        raise HTTPException(403, "이 항목을 이동할 권한이 없습니다") from error
    except OSError as error:
        raise HTTPException(400, f"이동 실패: {error}") from error
    return {"ok": True, "path": str(target)}


@router.post("/fs/trash")
def filesystem_trash(request: FsPathRequest):
    target = _fs_entry_path(request.path)
    if _protected_trash_target(target):
        raise HTTPException(400, "시스템 핵심 경로 자체는 휴지통으로 보낼 수 없습니다")
    if target.is_symlink():
        raise HTTPException(400, "심볼릭 링크는 대상 오삭제 방지를 위해 휴지통 기능에서 제외됩니다")
    try:
        from send2trash import send2trash
        send2trash(str(target))
    except ImportError as error:
        raise HTTPException(503, "send2trash 패키지가 설치되지 않았습니다") from error
    except PermissionError as error:
        raise HTTPException(403, "이 항목을 휴지통으로 보낼 권한이 없습니다") from error
    except OSError as error:
        raise HTTPException(400, f"휴지통 이동 실패: {error}") from error
    return {"ok": True, "recoverable": True}


@router.post("/fs/upload")
async def filesystem_upload(path: str, file: UploadFile = File(...)):
    destination = _fs_path(path)
    if not destination.is_dir():
        raise HTTPException(400, "업로드 대상은 폴더여야 합니다")
    target = destination / _leaf_name(file.filename or "upload.bin")
    if target.exists():
        raise HTTPException(409, "같은 이름의 파일이 이미 있습니다")
    try:
        with target.open("xb") as output:
            while chunk := await file.read(1024 * 1024):
                output.write(chunk)
    except PermissionError as error:
        raise HTTPException(403, "이 위치에 업로드할 권한이 없습니다") from error
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    return {"ok": True, "path": str(target), "size": target.stat().st_size}


@router.get("/fs/download")
def filesystem_download(path: str):
    target = _fs_path(path)
    if not target.is_file():
        raise HTTPException(400, "다운로드할 파일을 선택하세요")
    return FileResponse(target, filename=target.name)


@router.post("/folders")
def create_folder(request: FolderRequest):
    parent = _assert_model_path(request.parent)
    name = request.name.strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise HTTPException(400, "폴더 이름이 올바르지 않습니다")
    target = parent / name
    try:
        target.mkdir(exist_ok=False)
    except FileExistsError as error:
        raise HTTPException(409, "같은 이름의 폴더가 이미 있습니다") from error
    return {"ok": True, "path": str(target)}


@router.post("/inspect")
def inspect_model(request: InspectRequest):
    url = request.url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(400, "http 또는 https 모델 주소를 입력하세요")
    try:
        hf = _hf_source(url)
        if hf:
            return _hf_inspect(hf)
        civitai = _civitai_source(url)
        if civitai:
            return _civitai_inspect(civitai)
        return _direct_inspect(url)
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(400, f"모델 정보를 가져오지 못했습니다: {error}") from error


def _auth_headers(url: str) -> dict[str, str]:
    headers = {"User-Agent": "main-server-model-hub/2.0"}
    host = urlparse(url).hostname or ""
    if host.endswith("huggingface.co"):
        try:
            from huggingface_hub import get_token
            token = get_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        except Exception:
            pass
    elif host.endswith("civitai.com") and os.environ.get("CIVITAI_API_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['CIVITAI_API_TOKEN']}"
    return headers


def _hf_download_url(repo: str, revision: str, filename: str) -> str:
    from huggingface_hub import hf_hub_url
    return hf_hub_url(repo_id=repo, filename=filename, revision=revision)


def _update_job(job: dict[str, Any]) -> None:
    done = sum(item.get("done_bytes") or 0 for item in job["files"])
    known = [item.get("total_bytes") for item in job["files"]]
    job["done_bytes"] = done
    job["total_bytes"] = sum(known) if known and all(value is not None for value in known) else None
    elapsed = max(time.time() - job.get("transfer_started", time.time()), 0.001)
    job["speed"] = int(done / elapsed)
    if job["total_bytes"] and job["speed"]:
        job["eta"] = max(0, int((job["total_bytes"] - done) / job["speed"]))


def _stream_file(job: dict[str, Any], item: dict[str, Any], url: str, destination: Path) -> None:
    relative = _safe_relative_file(item["name"])
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    resume = partial.stat().st_size if partial.exists() else 0
    headers = _auth_headers(url)
    if resume:
        headers["Range"] = f"bytes={resume}-"
    with httpx.stream("GET", url, headers=headers, timeout=None, follow_redirects=True) as response:
        response.raise_for_status()
        if response.status_code != 206:
            resume = 0
        actual_name = _filename_from_headers(response, url)
        if "/" not in relative and actual_name and actual_name != "download.bin":
            target = destination / Path(actual_name).name
            partial = target.with_name(target.name + ".part")
            resume = partial.stat().st_size if response.status_code == 206 and partial.exists() else 0
        length = response.headers.get("content-length")
        item["total_bytes"] = resume + int(length) if str(length or "").isdigit() else item.get("size")
        item["done_bytes"] = resume
        item["target"] = str(target)
        mode = "ab" if response.status_code == 206 and resume else "wb"
        with partial.open(mode) as output:
            for chunk in response.iter_bytes(1024 * 1024):
                if job["status"] == "cancelled":
                    item["status"] = "cancelled"
                    return
                output.write(chunk)
                item["done_bytes"] += len(chunk)
                _update_job(job)
        os.replace(partial, target)
        item["status"] = "done"


def _download_worker(job: dict[str, Any]) -> None:
    job["status"] = "downloading"
    job["transfer_started"] = time.time()
    try:
        for index, item in enumerate(job["files"], 1):
            if job["status"] == "cancelled":
                return
            item["status"] = "downloading"
            job["status_text"] = f"{index}/{len(job['files'])} · {item['name']}"
            url = item.get("url")
            if job["source"] == "huggingface":
                url = _hf_download_url(job["repo"], job["revision"], item["name"])
            if not url:
                raise ValueError(f"다운로드 주소가 없습니다: {item['name']}")
            _stream_file(job, item, url, _resolved(job["destination"]))
        if job["status"] != "cancelled":
            job["status"] = "done"
            job["status_text"] = f"{len(job['files'])}개 파일 완료"
            job["finished"] = time.time()
    except Exception as error:
        job["status"] = "error"
        job["status_text"] = "다운로드 실패"
        job["error"] = str(error)
        job["finished"] = time.time()


@router.post("/download")
def start_download(request: DownloadRequest):
    destination = _assert_model_path(request.destination)
    files = []
    for item in request.files:
        try:
            name = _safe_relative_file(item.name)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        files.append({
            "name": name, "url": item.url, "size": item.size,
            "total_bytes": item.size, "done_bytes": 0, "status": "queued",
        })
    job = {
        "id": uuid.uuid4().hex[:10], "source": request.source,
        "repo": request.repo, "revision": request.revision or "main",
        "destination": str(destination), "files": files,
        "status": "queued", "status_text": "대기 중",
        "done_bytes": 0,
        "total_bytes": sum(item.size or 0 for item in request.files) or None,
        "speed": 0, "eta": None, "error": None, "created": time.time(),
    }
    with JOBS_LOCK:
        JOBS[job["id"]] = job
        for job_id in sorted(JOBS, key=lambda value: JOBS[value]["created"])[:-30]:
            JOBS.pop(job_id, None)
    threading.Thread(
        target=_download_worker, args=(job,), name=f"model-download-{job['id']}", daemon=True
    ).start()
    return {"ok": True, "job": job}


@router.get("/jobs")
def list_jobs():
    with JOBS_LOCK:
        jobs = sorted(JOBS.values(), key=lambda item: item["created"], reverse=True)
    return {"jobs": jobs}


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "작업을 찾지 못했습니다")
    if job["status"] in {"queued", "downloading"}:
        job["status"] = "cancelled"
        job["status_text"] = "취소됨 (부분 파일은 재개용으로 유지)"
    return {"ok": True, "job": job}


@router.get("/installed")
def installed_models(root: str = "", query: str = ""):
    roots = _base_roots()
    if not roots:
        raise HTTPException(400, "사용 가능한 모델 루트가 없습니다")
    selected = _assert_model_path(root or roots[0]["path"])
    needle = query.strip().lower()
    files = []
    try:
        for path in selected.rglob("*"):
            if not path.is_file() or path.name.endswith(".part") or ".cache" in path.parts:
                continue
            if path.suffix.lower() not in PRIMARY_MODEL_EXTENSIONS:
                continue
            relative = str(path.relative_to(selected))
            if needle and needle not in relative.lower():
                continue
            stat = path.stat()
            files.append({
                "name": path.name, "relative": relative, "path": str(path),
                "size": stat.st_size, "mtime": int(stat.st_mtime),
                "kind": path.suffix.lstrip(".").upper(),
            })
            if len(files) >= MAX_INSTALLED_FILES:
                break
    except OSError as error:
        raise HTTPException(400, f"모델 폴더를 읽지 못했습니다: {error}") from error
    files.sort(key=lambda item: item["mtime"], reverse=True)
    return {"root": str(selected), "files": files, "truncated": len(files) >= MAX_INSTALLED_FILES}
