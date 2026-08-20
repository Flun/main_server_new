import io
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from dataset_store import (
    BASE_DIR,
    DATASET_ROOT,
    IMAGE_EXTENSIONS,
    add_image,
    add_reference,
    claim_next_queued_job,
    create_dataset,
    create_completed_job,
    create_job,
    dataset_dir,
    delete_image,
    delete_images,
    delete_reference,
    ensure_dataset_dirs,
    get_dataset,
    get_image,
    get_job,
    get_reference,
    list_datasets,
    list_images,
    list_jobs,
    list_references,
    now_ts,
    recover_running_jobs,
    repair_backup_path,
    restore_repaired_image,
    set_dataset_status,
    shared_instagram_cookie_path,
    update_image,
    update_job,
)


router = APIRouter()


IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
    (b"BM", "image/bmp", ".bmp"),
)


def inline_image_response(path: Path, filename: str = ""):
    with open(path, "rb") as handle:
        header = handle.read(16)
    media_type = "application/octet-stream"
    extension = path.suffix.lower() or ".bin"
    for signature, candidate_type, candidate_extension in IMAGE_SIGNATURES:
        if header.startswith(signature):
            media_type, extension = candidate_type, candidate_extension
            break
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        media_type, extension = "image/webp", ".webp"
    safe_stem = Path(filename or path.name).stem or "image"
    return FileResponse(
        str(path),
        media_type=media_type,
        filename=f"{safe_stem}{extension}",
        content_disposition_type="inline",
    )


@router.get("/api/dataset-tools/chrome-cookie-extension.zip")
def chrome_cookie_extension_download():
    extension_root = BASE_DIR / "chrome_instagram_cookie_bridge"
    if not extension_root.is_dir():
        raise HTTPException(status_code=404, detail="Chrome extension files not found")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in extension_root.rglob("*"):
            if path.is_file():
                archive.write(path, Path("chrome_instagram_cookie_bridge") / path.relative_to(extension_root))
    return Response(
        buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="chrome_instagram_cookie_bridge.zip"'},
    )


class DatasetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    subject_name: str = Field(default="", max_length=100)


class JobCreate(BaseModel):
    kind: str
    params: Dict[str, Any] = Field(default_factory=dict)


class Decision(BaseModel):
    status: str


class RestoreRequest(BaseModel):
    analyze_params: Dict[str, Any] = Field(default_factory=dict)


class BulkDelete(BaseModel):
    statuses: List[str] = Field(default_factory=list)
    issues: List[str] = Field(default_factory=list)
    quality_below: Optional[float] = None
    similarity_below: Optional[float] = None
    job_id: Optional[str] = None
    repaired_only: bool = False
    delete_all: bool = False


class BrowserCookie(BaseModel):
    domain: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=512)
    value: str = Field(default="", max_length=16384)
    path: str = Field(default="/", max_length=2048)
    secure: bool = False
    httpOnly: bool = False
    expirationDate: Optional[float] = None


class BrowserCookieSync(BaseModel):
    cookies: List[BrowserCookie] = Field(min_length=1, max_length=5000)


class WorkerManager:
    def __init__(self):
        self.process = None
        self.job_id = None
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="dataset-worker-manager", daemon=True)
        self.thread.start()

    @property
    def python_executable(self):
        venv_py = (
            BASE_DIR / ".dataset_venv" / "bin" / "python"
            if os.name != "nt"
            else BASE_DIR / ".dataset_venv" / "Scripts" / "python.exe"
        )
        return str(venv_py if venv_py.exists() else Path(sys.executable))

    def _finish_orphan(self, return_code):
        if not self.job_id:
            return
        job = get_job(self.job_id)
        if job and job["status"] == "running":
            if job["cancel_requested"]:
                update_job(self.job_id, status="cancelled", finished_at=now_ts(), message="사용자가 취소함")
            else:
                update_job(
                    self.job_id,
                    status="failed",
                    finished_at=now_ts(),
                    message="worker가 비정상 종료됨",
                    error=f"worker exit code: {return_code}",
                )
            set_dataset_status(job["dataset_id"], "ready" if job["cancel_requested"] else "error")

    def _loop(self):
        logs = DATASET_ROOT / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        while not self.stop_event.wait(1):
            with self.lock:
                if self.process:
                    return_code = self.process.poll()
                    if return_code is None:
                        continue
                    self._finish_orphan(return_code)
                    self.process = None
                    self.job_id = None
                job_id = claim_next_queued_job()
                if not job_id:
                    continue
                log_path = logs / f"{job_id}.log"
                log_handle = open(log_path, "a", encoding="utf-8")
                creationflags = 0
                if os.name == "nt":
                    creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                try:
                    self.process = subprocess.Popen(
                        [self.python_executable, str(BASE_DIR / "dataset_worker.py"), "--job", job_id],
                        cwd=str(BASE_DIR),
                        stdout=log_handle,
                        stderr=subprocess.STDOUT,
                        creationflags=creationflags,
                    )
                    self.job_id = job_id
                except Exception as exc:
                    update_job(job_id, status="queued", started_at=None, message="worker 시작 재시도", error=str(exc))
                    self.process = None
                    self.job_id = None
                finally:
                    log_handle.close()

    def cancel(self, job_id):
        job = get_job(job_id)
        if job and job["status"] == "queued":
            update_job(job_id, cancel_requested=1, status="cancelled", finished_at=now_ts(), message="사용자가 취소함")
            set_dataset_status(job["dataset_id"], "ready")
            return
        update_job(job_id, cancel_requested=1)
        with self.lock:
            if self.job_id == job_id and self.process and self.process.poll() is None:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                else:
                    self.process.terminate()


recover_running_jobs()
manager = WorkerManager()


def require_dataset(dataset_id):
    dataset = get_dataset(dataset_id)
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return dataset


@router.get("/dataset")
def dataset_page():
    return FileResponse(
        str(BASE_DIR / "dataset.html"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@router.get("/api/datasets")
def datasets_list():
    return list_datasets()


@router.post("/api/datasets")
def datasets_create(data: DatasetCreate):
    return create_dataset(data.name, data.subject_name)


@router.get("/api/datasets/{dataset_id}")
def dataset_detail(dataset_id: str):
    return require_dataset(dataset_id)


@router.post("/api/datasets/{dataset_id}/references")
async def upload_references(dataset_id: str, files: List[UploadFile] = File(...)):
    require_dataset(dataset_id)
    folder = ensure_dataset_dirs(dataset_id) / "references"
    saved = []
    duplicates = 0
    for upload in files[:10]:
        suffix = Path(upload.filename or "").suffix.lower()
        if suffix not in IMAGE_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"Unsupported image: {upload.filename}")
        target = folder / f"{uuid.uuid4().hex}{suffix}"
        size = 0
        with open(target, "wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > 25 * 1024 * 1024:
                    handle.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail="Reference image is too large")
                handle.write(chunk)
        reference_id = add_reference(dataset_id, target, upload.filename or target.name)
        saved.append(reference_id)
    return {"saved": saved, "dataset": get_dataset(dataset_id)}


@router.get("/api/datasets/{dataset_id}/references")
def references_list(dataset_id: str):
    require_dataset(dataset_id)
    references = list_references(dataset_id)
    for reference in references:
        reference["thumbnail_url"] = f"/api/dataset-references/{reference['id']}/file"
    return references


@router.get("/api/dataset-references/{reference_id}/file")
def reference_file(reference_id: str):
    reference = get_reference(reference_id)
    if not reference:
        raise HTTPException(status_code=404, detail="Reference not found")
    path = Path(reference["path"]).resolve()
    root = dataset_dir(reference["dataset_id"]).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Reference file not found")
    return inline_image_response(path, reference["original_name"])


@router.delete("/api/dataset-references/{reference_id}")
def reference_delete(reference_id: str):
    reference = get_reference(reference_id)
    if not reference:
        raise HTTPException(status_code=404, detail="Reference not found")
    delete_reference(reference_id)
    return {"deleted": True, "dataset": get_dataset(reference["dataset_id"])}


@router.post("/api/datasets/{dataset_id}/uploads")
async def upload_source_images(dataset_id: str, files: List[UploadFile] = File(...)):
    require_dataset(dataset_id)
    folder = ensure_dataset_dirs(dataset_id) / "raw" / "imported"
    upload_job = create_completed_job(dataset_id, "upload", {"file_count": len(files)}, "브라우저 업로드")
    saved = []
    for upload in files[:500]:
        suffix = Path(upload.filename or "").suffix.lower()
        if suffix not in IMAGE_EXTENSIONS:
            continue
        target = folder / f"{uuid.uuid4().hex}{suffix}"
        size = 0
        with open(target, "wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > 50 * 1024 * 1024:
                    handle.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail=f"Image is too large: {upload.filename}")
                handle.write(chunk)
        if add_image(dataset_id, "upload", target, upload.filename or "", upload_job["id"]):
            saved.append(target.name)
        else:
            duplicates += 1
    return {"saved": len(saved), "duplicates": duplicates, "job_id": upload_job["id"], "dataset": get_dataset(dataset_id)}


@router.post("/api/instagram-cookies")
@router.post("/api/datasets/{dataset_id}/instagram-cookies")
async def upload_instagram_cookies(file: UploadFile = File(...), dataset_id: Optional[str] = None):
    if dataset_id:
        require_dataset(dataset_id)
    content = await file.read(5 * 1024 * 1024 + 1)
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="cookies.txt is too large")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="cookies.txt는 UTF-8 텍스트여야 합니다.")
    if ".instagram.com" not in text and "instagram.com" not in text:
        raise HTTPException(status_code=400, detail="Instagram 쿠키가 포함된 cookies.txt가 아닙니다.")
    target = shared_instagram_cookie_path()
    target.write_text(text, encoding="utf-8")
    return {"saved": True, "filename": file.filename, "scope": "server"}


@router.get("/api/instagram-cookies")
@router.get("/api/datasets/{dataset_id}/instagram-cookies")
def instagram_cookie_status(dataset_id: Optional[str] = None):
    if dataset_id:
        require_dataset(dataset_id)
    exists = shared_instagram_cookie_path().is_file()
    return {"configured": exists, "scope": "server"}


@router.delete("/api/instagram-cookies")
@router.delete("/api/datasets/{dataset_id}/instagram-cookies")
def instagram_cookie_delete(dataset_id: Optional[str] = None):
    if dataset_id:
        require_dataset(dataset_id)
    path = shared_instagram_cookie_path()
    path.unlink(missing_ok=True)
    return {"configured": False, "scope": "server"}


@router.post("/api/instagram-cookie-sync")
@router.post("/api/datasets/{dataset_id}/instagram-cookie-sync")
def instagram_cookie_sync(data: BrowserCookieSync, dataset_id: Optional[str] = None):
    if dataset_id:
        require_dataset(dataset_id)
    lines = ["# Netscape HTTP Cookie File", "# Synced locally from Chrome extension", ""]
    accepted = 0
    has_session = False
    for cookie in data.cookies:
        domain = cookie.domain.strip().lower()
        if domain != "instagram.com" and not domain.endswith(".instagram.com"):
            continue
        values = (domain, cookie.name, cookie.value, cookie.path)
        if any("\t" in value or "\r" in value or "\n" in value for value in values):
            continue
        domain = domain if domain.startswith(".") else "." + domain
        domain_field = "#HttpOnly_" + domain if cookie.httpOnly else domain
        expires = max(int(cookie.expirationDate or 0), 0)
        lines.append("\t".join((
            domain_field,
            "TRUE",
            cookie.path or "/",
            "TRUE" if cookie.secure else "FALSE",
            str(expires),
            cookie.name,
            cookie.value,
        )))
        accepted += 1
        has_session = has_session or cookie.name == "sessionid"
    if not accepted:
        raise HTTPException(status_code=400, detail="Instagram 쿠키를 찾지 못했습니다.")
    target = shared_instagram_cookie_path()
    temporary = target.with_suffix(".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(target)
    return {"saved": True, "count": accepted, "has_session": has_session, "scope": "server"}


@router.post("/api/datasets/{dataset_id}/jobs")
def jobs_create(dataset_id: str, data: JobCreate):
    require_dataset(dataset_id)
    allowed = {"collect_bing", "collect_search", "collect_instagram", "import_folder", "analyze", "repair", "export"}
    if data.kind not in allowed:
        raise HTTPException(status_code=400, detail="Unsupported job kind")
    if data.kind == "analyze" and get_dataset(dataset_id)["reference_count"] == 0:
        raise HTTPException(status_code=400, detail="기준 얼굴 이미지를 먼저 등록하세요.")
    if data.kind == "repair" and data.params.get("mode") in {"auto", "small_face"}:
        if get_dataset(dataset_id)["reference_count"] == 0:
            raise HTTPException(status_code=400, detail="얼굴 크롭에는 기준 얼굴 이미지가 필요합니다.")
    return create_job(dataset_id, data.kind, data.params)


@router.get("/api/datasets/{dataset_id}/jobs")
def dataset_jobs(dataset_id: str):
    require_dataset(dataset_id)
    return list_jobs(dataset_id)


@router.get("/api/dataset-jobs/{job_id}")
def job_detail(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/api/dataset-jobs/{job_id}/cancel")
def job_cancel(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] in {"completed", "failed", "cancelled"}:
        return job
    manager.cancel(job_id)
    return get_job(job_id)


@router.get("/api/datasets/{dataset_id}/images")
def dataset_images(dataset_id: str, status: str = "all", limit: int = 200, offset: int = 0):
    require_dataset(dataset_id)
    images = list_images(dataset_id, status, limit, offset)
    for image in images:
        image["thumbnail_url"] = f"/api/dataset-images/{image['id']}/file"
        backup = repair_backup_path(image)
        image["is_repaired"] = bool(backup)
        image["original_url"] = f"/api/dataset-images/{image['id']}/original" if backup else ""
    return images


@router.get("/api/dataset-images/{image_id}/file")
def image_file(image_id: str):
    image = get_image(image_id)
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")
    path = Path(image["path"]).resolve()
    root = DATASET_ROOT.resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Image file not found")
    return inline_image_response(path)


@router.get("/api/dataset-images/{image_id}/original")
def image_original_file(image_id: str):
    image = get_image(image_id)
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")
    backup = repair_backup_path(image)
    if not backup:
        raise HTTPException(status_code=404, detail="Original backup not found")
    return inline_image_response(backup, filename=f"{image_id}_original")


@router.post("/api/dataset-images/{image_id}/restore")
def image_restore(image_id: str, data: RestoreRequest):
    image = get_image(image_id)
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")
    try:
        restored = restore_repaired_image(image_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    analyze_job = None
    if get_dataset(restored["dataset_id"])["reference_count"]:
        analyze_job = create_job(restored["dataset_id"], "analyze", data.analyze_params)
    return {"image": restored, "analyze_job": analyze_job}


@router.post("/api/dataset-images/{image_id}/decision")
def image_decision(image_id: str, data: Decision):
    image = get_image(image_id)
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")
    if data.status not in {"accepted", "review", "rejected", "duplicate"}:
        raise HTTPException(status_code=400, detail="Invalid status")
    update_image(image_id, status=data.status, reason="수동 판정")
    return get_image(image_id)


@router.delete("/api/dataset-images/{image_id}")
def image_delete(image_id: str):
    image = get_image(image_id)
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")
    delete_image(image_id)
    return {"deleted": True}


@router.post("/api/datasets/{dataset_id}/images/delete")
def images_bulk_delete(dataset_id: str, data: BulkDelete):
    require_dataset(dataset_id)
    allowed_statuses = {"new", "accepted", "review", "rejected", "duplicate"}
    if any(status not in allowed_statuses for status in data.statuses):
        raise HTTPException(status_code=400, detail="Invalid image status")
    try:
        result = delete_images(
            dataset_id,
            statuses=data.statuses,
            quality_below=data.quality_below,
            similarity_below=data.similarity_below,
            job_id=data.job_id,
            issues=data.issues,
            repaired_only=data.repaired_only,
            delete_all=data.delete_all,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result | {"dataset": get_dataset(dataset_id)}


@router.delete("/api/dataset-jobs/{job_id}/results")
def job_results_delete(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="실행 중인 작업 결과는 삭제할 수 없습니다.")
    result = delete_images(job["dataset_id"], job_id=job_id)
    update_job(job_id, message=f"결과 {result['deleted_records']}개 삭제됨")
    return result | {"dataset": get_dataset(job["dataset_id"])}


@router.get("/api/datasets/{dataset_id}/export")
def download_export(dataset_id: str):
    require_dataset(dataset_id)
    folder = dataset_dir(dataset_id) / "exports"
    exports = sorted(folder.glob("*.zip"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not exports:
        raise HTTPException(status_code=404, detail="완료된 내보내기가 없습니다.")
    return FileResponse(str(exports[0]), filename=exports[0].name, media_type="application/zip")
    add_image,
    add_reference,
    delete_images,
    delete_reference,
