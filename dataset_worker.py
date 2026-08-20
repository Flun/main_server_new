import argparse
import contextlib
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import traceback
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import requests

from dataset_store import (
    DATASET_ROOT,
    IMAGE_EXTENSIONS,
    add_image,
    connect,
    dataset_dir,
    ensure_dataset_dirs,
    get_job,
    instagram_download_archive_path,
    now_ts,
    set_dataset_status,
    shared_instagram_cookie_path,
    update_image,
    update_job,
)


def cancelled(job_id):
    job = get_job(job_id)
    return not job or job["cancel_requested"]


@contextlib.contextmanager
def exclusive_worker_lock():
    """Serialize dataset workers across server restarts or duplicate managers."""
    lock_path = DATASET_ROOT / "dataset_worker.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    try:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def progress(job_id, current, total, message):
    ratio = current / total if total else 0
    update_job(job_id, current=current, total=total, progress=min(max(ratio, 0), 1), message=message)


def unique_destination(folder, name):
    clean = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in Path(name).name)
    target = folder / (clean[:140] or "image.jpg")
    counter = 1
    while target.exists():
        target = folder / f"{Path(clean).stem[:120]}_{counter}{Path(clean).suffix.lower()}"
        counter += 1
    return target


def register_folder(dataset_id, source, folder, before=None, original_url="", job_id=""):
    before = before or set()
    paths = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS and p.resolve() not in before]
    added = 0
    duplicates = 0
    for path in paths:
        if add_image(dataset_id, source, path, original_url, job_id):
            added += 1
        else:
            duplicates += 1
    return added, duplicates


def collect_bing(job):
    try:
        from bing_image_downloader import download
    except ImportError as exc:
        raise RuntimeError("bing-image-downloader가 설치되지 않았습니다. setup_dataset_env.bat을 실행하세요.") from exc
    query = str(job["params"].get("query", "")).strip()
    limit = min(max(int(job["params"].get("limit", 100)), 1), 1000)
    if not query:
        raise ValueError("검색어가 필요합니다.")
    folder = ensure_dataset_dirs(job["dataset_id"]) / "raw" / "web" / job["id"]
    before = {p.resolve() for p in folder.rglob("*") if p.is_file()}
    progress(job["id"], 0, limit, f"Bing 검색: {query}")
    download(
        query,
        limit=limit,
        output_dir=str(folder),
        adult_filter_off=False,
        force_replace=False,
        timeout=20,
        verbose=True,
    )
    count, duplicates = register_folder(job["dataset_id"], "bing", folder, before, f"bing:{query}", job["id"])
    progress(job["id"], count, max(count, 1), f"새 이미지 {count}개 · 완전 중복 {duplicates}개 제외")


def collect_search(job):
    try:
        from ddgs import DDGS
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("ddgs가 설치되지 않았습니다. setup_dataset_env.bat을 실행하세요.") from exc
    query = str(job["params"].get("query", "")).strip()
    limit = min(max(int(job["params"].get("limit", 100)), 1), 500)
    if not query:
        raise ValueError("검색어가 필요합니다.")
    folder = ensure_dataset_dirs(job["dataset_id"]) / "raw" / "web" / job["id"]
    folder.mkdir(parents=True, exist_ok=True)
    progress(job["id"], 0, limit, f"이미지 검색: {query}")
    query_variants = [
        query,
        f'"{query}" portrait',
        f'"{query}" 인물 사진',
        f'"{query}" 화보',
        f'"{query}" interview',
        f'"{query}" close up',
        f'"{query}" 셀카',
        f'"{query}" 방송',
        f'"{query}" Instagram',
        f'"{query}" YouTube',
        f'"{query}" live',
    ]
    results = []
    seen_urls = set()
    with connect() as conn:
        existing_urls = {
            row["original_url"]
            for row in conn.execute(
                "SELECT original_url FROM images WHERE dataset_id=? AND original_url<>''",
                (job["dataset_id"],),
            ).fetchall()
        }
    known_url_skips = 0
    search_errors = 0
    for variant in query_variants:
        if len(results) >= limit * 2 or cancelled(job["id"]):
            break
        try:
            candidates = DDGS().images(
                variant,
                max_results=min(max(limit, 50), 200),
                safesearch="moderate",
            )
            for candidate in candidates:
                image_url = candidate.get("image") or candidate.get("thumbnail")
                if not image_url or image_url in seen_urls:
                    continue
                seen_urls.add(image_url)
                if image_url in existing_urls:
                    known_url_skips += 1
                    continue
                results.append(candidate)
                if len(results) >= limit * 2:
                    break
        except Exception:
            search_errors += 1
        progress(job["id"], 0, limit, f"검색 후보 {len(results)}개 확보")
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    downloaded = 0
    failed = 0
    duplicates = 0
    for index, result in enumerate(results, 1):
        if downloaded >= limit:
            break
        if cancelled(job["id"]):
            return
        image_url = result.get("image") or result.get("thumbnail")
        if not image_url:
            continue
        target = folder / f"{index:04d}.jpg"
        try:
            response = session.get(image_url, timeout=20, stream=True)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            suffix = ".png" if "png" in content_type else ".webp" if "webp" in content_type else ".jpg"
            target = target.with_suffix(suffix)
            total_bytes = 0
            with open(target, "wb") as handle:
                for chunk in response.iter_content(1024 * 128):
                    total_bytes += len(chunk)
                    if total_bytes > 50 * 1024 * 1024:
                        raise ValueError("image exceeds 50 MB")
                    handle.write(chunk)
            with Image.open(target) as image:
                image.verify()
            if add_image(job["dataset_id"], "search", target, image_url, job["id"]):
                downloaded += 1
            else:
                duplicates += 1
        except Exception:
            failed += 1
            target.unlink(missing_ok=True)
        progress(job["id"], downloaded, limit, f"요청 {limit} · 후보 {len(results)} · 저장 {downloaded} · 중복 {duplicates + known_url_skips} · 실패 {failed}")
    if not downloaded and not duplicates and not known_url_skips:
        raise RuntimeError("검색 결과에서 다운로드 가능한 이미지를 찾지 못했습니다.")
    suffix = f" · 검색 오류 {search_errors}" if search_errors else ""
    progress(
        job["id"], downloaded, limit,
        f"요청 {limit} · 후보 {len(results)} · 저장 {downloaded} · 중복 {duplicates + known_url_skips} · 실패 {failed}{suffix}",
    )


def collect_instagram(job):
    params = job["params"]
    raw_url = str(params.get("url", "")).strip()
    if not raw_url.startswith("http"):
        raw_url = f"https://www.instagram.com/{raw_url.strip('/')}/"
    parsed = urlparse(raw_url)
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"instagram.com", "www.instagram.com"}:
        raise ValueError("instagram.com의 HTTPS URL만 허용됩니다.")
    username = parsed.path.strip("/").split("/", 1)[0]
    if not username or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._" for ch in username):
        raise ValueError("올바른 Instagram 사용자명 또는 프로필 URL을 입력하세요.")
    url = f"https://www.instagram.com/{username}/"
    root = ensure_dataset_dirs(job["dataset_id"])
    folder = root / "raw" / "instagram" / job["id"]
    before = {p.resolve() for p in folder.rglob("*") if p.is_file()}
    include = [value for value in params.get("include", ["posts", "tagged"]) if value in {"posts", "tagged", "reels", "highlights"}]
    cmd = [sys.executable, "-m", "gallery_dl", "-D", str(folder), "-o", "extractor.instagram.videos=false"]
    cmd += ["--download-archive", str(instagram_download_archive_path(job["dataset_id"]))]
    if include:
        cmd += ["-o", f"extractor.instagram.include={','.join(include)}"]
    max_posts = min(max(int(params.get("limit", 200)), 1), 1000)
    cmd += ["-o", f"extractor.instagram.max-posts={max_posts}"]
    cookie_file = shared_instagram_cookie_path()
    browser = params.get("browser")
    if cookie_file.is_file():
        cmd += ["--cookies", str(cookie_file)]
    elif browser in {"chrome", "edge", "firefox", "brave"}:
        cmd += ["--cookies-from-browser", browser]
    cmd.append(url)
    progress(job["id"], 0, max_posts, "Instagram 수집 중")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "gallery-dl 실패")[-3000:]
        if "Permission denied" in detail and "Cookies" in detail:
            detail += "\nChrome 쿠키 DB가 잠겨 있습니다. 로컬 Chrome 동기화 확장을 사용하거나 cookies.txt를 업로드하세요."
        elif "NotFoundError" in detail and not cookie_file.is_file():
            detail += "\n비로그인 요청이 차단되었을 수 있습니다. Chrome 동기화 확장 또는 cookies.txt로 로그인 쿠키를 등록한 뒤 다시 시도하세요."
        raise RuntimeError(detail)
    count, duplicates = register_folder(job["dataset_id"], "instagram", folder, before, url, job["id"])
    progress(job["id"], count, max(count, 1), f"새 이미지 {count}개 · 완전 중복 {duplicates}개 제외")


def import_folder(job):
    source_path = Path(str(job["params"].get("path", ""))).expanduser().resolve()
    if not source_path.is_dir():
        raise ValueError("가져올 폴더가 존재하지 않습니다.")
    destination = ensure_dataset_dirs(job["dataset_id"]) / "raw" / "imported"
    sources = [p for p in source_path.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS]
    added = 0
    duplicates = 0
    for index, source in enumerate(sources, 1):
        if cancelled(job["id"]):
            return
        target = unique_destination(destination, source.name)
        shutil.copy2(source, target)
        if add_image(job["dataset_id"], "import", target, str(source), job["id"]):
            added += 1
        else:
            duplicates += 1
        progress(job["id"], index, len(sources), f"새 이미지 {added}개 · 완전 중복 {duplicates}개 제외")


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cosine_similarity(left, right):
    dot = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(a) ** 2 for a in left))
    right_norm = math.sqrt(sum(float(b) ** 2 for b in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else -1


def prepare_onnx_cuda_dlls(onnxruntime):
    """Keep NVIDIA wheel DLL directories available for cuDNN's delayed loads."""
    site_packages = Path(onnxruntime.__file__).resolve().parents[1]
    bin_dirs = list((site_packages / "nvidia").glob("*/bin"))
    handles = []
    for bin_dir in bin_dirs:
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            handles.append(os.add_dll_directory(str(bin_dir)))
    if "CUDAExecutionProvider" in onnxruntime.get_available_providers():
        onnxruntime.preload_dlls(directory="")
    return handles


def load_face_model_and_references(dataset_id):
    try:
        import cv2
        import onnxruntime
        from insightface.app import FaceAnalysis
    except ImportError as exc:
        raise RuntimeError("얼굴 개선 환경이 없습니다. setup_dataset_env.bat을 실행하세요.") from exc
    cuda_dll_handles = prepare_onnx_cuda_dlls(onnxruntime)
    face_app = FaceAnalysis(
        name="buffalo_l",
        providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    face_app.prepare(ctx_id=0, det_size=(640, 640))
    reference_embeddings = []
    reference_dir = dataset_dir(dataset_id) / "references"
    for reference in reference_dir.iterdir():
        if reference.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        image = cv2.imread(str(reference))
        faces = face_app.get(image) if image is not None else []
        if faces:
            largest = max(
                faces,
                key=lambda face: float(
                    (face.bbox[2] - face.bbox[0]) * (face.bbox[3] - face.bbox[1])
                ),
            )
            reference_embeddings.append(largest.embedding.tolist())
    if not reference_embeddings:
        raise ValueError("기준 이미지에서 얼굴을 찾지 못했습니다.")
    return face_app, reference_embeddings, cuda_dll_handles


def best_target_face(faces, reference_embeddings):
    if not faces:
        return None
    return max(
        faces,
        key=lambda face: max(
            cosine_similarity(reference, face.embedding)
            for reference in reference_embeddings
        ),
    )


def crop_around_face(image, bbox, padding=4.0):
    width, height = image.size
    left, top, right, bottom = [float(value) for value in bbox]
    face_width = max(right - left, 1.0)
    face_height = max(bottom - top, 1.0)
    side = min(max(face_width, face_height) * padding, width, height)
    center_x = (left + right) / 2
    center_y = (top + bottom) / 2 + face_height * 0.35
    crop_left = min(max(center_x - side / 2, 0), width - side)
    crop_top = min(max(center_y - side / 2, 0), height - side)
    box = (
        int(round(crop_left)),
        int(round(crop_top)),
        int(round(crop_left + side)),
        int(round(crop_top + side)),
    )
    return image.crop(box)


def upscale_and_sharpen(image, minimum_side=1024):
    import cv2
    import numpy as np
    from PIL import Image

    width, height = image.size
    short_side = min(width, height)
    if short_side < minimum_side:
        scale = min(minimum_side / max(short_side, 1), 2.0)
        image = image.resize(
            (max(int(round(width * scale)), 1), max(int(round(height * scale)), 1)),
            Image.Resampling.LANCZOS,
        )
    rgb = np.asarray(image.convert("RGB"))
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    softened = cv2.GaussianBlur(bgr, (0, 0), 1.15)
    sharpened = cv2.addWeighted(bgr, 1.65, softened, -0.65, 0)
    return Image.fromarray(cv2.cvtColor(sharpened, cv2.COLOR_BGR2RGB))


def save_repaired_image(image, path, source_format):
    source_format = (source_format or "JPEG").upper()
    if source_format not in {"JPEG", "PNG", "WEBP", "BMP"}:
        source_format = "JPEG"
    temporary = path.with_name(path.name + ".repair.tmp")
    options = {}
    if source_format == "JPEG":
        options = {"quality": 95, "subsampling": 0, "optimize": True}
    elif source_format == "WEBP":
        options = {"quality": 95, "method": 6}
    image.save(temporary, format=source_format, **options)
    os.replace(temporary, path)


def repair_images(job):
    try:
        import cv2
        import numpy as np
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RuntimeError("이미지 개선 환경이 없습니다. setup_dataset_env.bat을 실행하세요.") from exc

    params = job["params"]
    mode = str(params.get("mode", "auto"))
    if mode not in {"auto", "small_face", "blurry"}:
        raise ValueError("지원하지 않는 이미지 개선 방식입니다.")
    image_ids = [str(value) for value in params.get("image_ids", [])][:500]
    explicit_images = bool(image_ids)
    clauses = ["dataset_id=?"]
    args = [job["dataset_id"]]
    if image_ids:
        clauses.append("id IN (" + ",".join("?" for _ in image_ids) + ")")
        args.extend(image_ids)
    elif mode == "small_face":
        clauses.append("instr(reason,'얼굴이 작음')>0")
    elif mode == "blurry":
        clauses.append("instr(reason,'흐림 후보')>0")
    else:
        clauses.append("(instr(reason,'얼굴이 작음')>0 OR instr(reason,'흐림 후보')>0)")
    with connect() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM images WHERE " + " AND ".join(clauses) + " ORDER BY created_at",
                args,
            ).fetchall()
        ]
    if not rows:
        progress(job["id"], 0, 1, "개선할 이미지가 없습니다.")
        return

    needs_crop = mode in {"auto", "small_face"} and (
        explicit_images or any("얼굴이 작음" in row["reason"] for row in rows)
    )
    face_app = None
    reference_embeddings = []
    cuda_dll_handles = []
    if needs_crop:
        face_app, reference_embeddings, cuda_dll_handles = load_face_model_and_references(
            job["dataset_id"]
        )

    backup_dir = ensure_dataset_dirs(job["dataset_id"]) / "repairs" / "originals"
    backup_dir.mkdir(parents=True, exist_ok=True)
    repaired = 0
    skipped = 0
    for index, item in enumerate(rows, 1):
        if cancelled(job["id"]):
            return
        path = Path(item["path"])
        original_backup = backup_dir / f"{item['id']}{path.suffix.lower()}"
        try:
            if not original_backup.exists():
                shutil.copy2(path, original_backup)
            with Image.open(path) as source:
                source_format = source.format
                image = ImageOps.exif_transpose(source).convert("RGB")

            do_crop = mode in {"auto", "small_face"} and (explicit_images or "얼굴이 작음" in item["reason"])
            do_sharpen = mode in {"auto", "blurry"} and (explicit_images or "흐림 후보" in item["reason"])
            if do_crop:
                cv_image = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
                face = best_target_face(face_app.get(cv_image), reference_embeddings)
                if face is None:
                    raise ValueError("크롭할 기준 인물 얼굴을 찾지 못했습니다.")
                image = crop_around_face(image, face.bbox)
            if do_crop or do_sharpen:
                image = upscale_and_sharpen(image)
            if not do_crop and not do_sharpen:
                skipped += 1
                progress(job["id"], index, len(rows), f"건너뜀 {skipped} · 개선 {repaired}")
                continue
            save_repaired_image(image, path, source_format)
            repair_kinds = set(filter(None, str(item.get("repair_kind") or "").split("+")))
            if do_crop:
                repair_kinds.add("small_face")
            if do_sharpen:
                repair_kinds.add("blurry")
            repair_kind = "+".join(value for value in ("small_face", "blurry") if value in repair_kinds)
            update_image(
                item["id"],
                sha256=file_sha256(path),
                width=image.size[0],
                height=image.size[1],
                status="review",
                reason="개선 완료 · 재분석 대기",
                repair_kind=repair_kind,
                repaired_at=now_ts(),
            )
            repaired += 1
        except Exception as exc:
            skipped += 1
            update_image(item["id"], reason=f"개선 실패: {str(exc)[:240]}")
        progress(job["id"], index, len(rows), f"개선 {repaired} · 건너뜀 {skipped}")

    if repaired:
        analyze_params = params.get("analyze_params") or {}
        progress(job["id"], len(rows), len(rows), f"{repaired}장 개선 · 재분석 시작")
        analyze({**job, "params": analyze_params})
        progress(
            job["id"],
            len(rows),
            len(rows),
            f"{repaired}장 개선 · 재분석 완료",
        )
    else:
        progress(job["id"], len(rows), len(rows), f"개선 0장 · 건너뜀 {skipped}")


def analyze(job):
    try:
        import cv2
        import imagehash
        import numpy as np
        import onnxruntime
        from PIL import Image, ImageOps
        from insightface.app import FaceAnalysis
    except ImportError as exc:
        raise RuntimeError("얼굴 분석 환경이 없습니다. setup_dataset_env.bat을 실행하세요.") from exc

    params = job["params"]
    threshold = min(max(float(params.get("similarity_threshold", 0.4)), 0.1), 0.95)
    min_side = min(max(int(params.get("min_side", 512)), 128), 4096)
    min_face_ratio = min(max(float(params.get("min_face_ratio", 0.04)), 0.005), 0.8)
    blur_threshold = min(max(float(params.get("blur_threshold", 65)), 1), 1000)
    cuda_dll_handles = prepare_onnx_cuda_dlls(onnxruntime)
    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    face_app = FaceAnalysis(name="buffalo_l", providers=providers)
    face_app.prepare(ctx_id=0, det_size=(640, 640))

    references = [p for p in (dataset_dir(job["dataset_id"]) / "references").iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS]
    if not references:
        raise ValueError("기준 얼굴 이미지를 먼저 등록하세요.")
    reference_embeddings = []
    for reference in references:
        image = cv2.imread(str(reference))
        faces = face_app.get(image) if image is not None else []
        if faces:
            largest = max(faces, key=lambda face: float((face.bbox[2] - face.bbox[0]) * (face.bbox[3] - face.bbox[1])))
            reference_embeddings.append(largest.embedding.tolist())
    if not reference_embeddings:
        raise ValueError("기준 이미지에서 얼굴을 찾지 못했습니다.")

    with connect() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM images WHERE dataset_id=? ORDER BY created_at", (job["dataset_id"],)).fetchall()]
    exact_seen = {}
    phash_seen = []
    for index, item in enumerate(rows, 1):
        if cancelled(job["id"]):
            return
        path = Path(item["path"])
        try:
            sha = file_sha256(path)
            with Image.open(path) as pil_source:
                pil_image = ImageOps.exif_transpose(pil_source).convert("RGB")
                width, height = pil_image.size
                phash_value = imagehash.phash(pil_image)
                cv_image = cv2.cvtColor(np.asarray(pil_image), cv2.COLOR_RGB2BGR)
            gray = cv2.cvtColor(cv_image, cv2.COLOR_BGR2GRAY)
            blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
            faces = face_app.get(cv_image)
            image_area = max(width * height, 1)
            target_similarity = -1.0
            largest_ratio = 0.0
            for face in faces:
                bbox = face.bbox
                face_area = max(float((bbox[2] - bbox[0]) * (bbox[3] - bbox[1])), 0)
                largest_ratio = max(largest_ratio, face_area / image_area)
                for reference_embedding in reference_embeddings:
                    target_similarity = max(target_similarity, cosine_similarity(reference_embedding, face.embedding))

            resolution_score = min(min(width, height) / max(min_side, 1), 1)
            face_score = min(largest_ratio / max(min_face_ratio, 0.001), 1)
            blur_score = min(blur / max(blur_threshold, 1), 1)
            quality = round((resolution_score * 0.35 + face_score * 0.4 + blur_score * 0.25) * 100, 1)

            status = "review"
            reasons = []
            if sha in exact_seen:
                status, reasons = "duplicate", [f"완전 중복: {exact_seen[sha]}"]
            else:
                near = next((seen_id for seen_hash, seen_id in phash_seen if phash_value - seen_hash <= 6), None)
                if near:
                    status, reasons = "duplicate", [f"유사 중복 후보: {near}"]
                elif not faces:
                    status, reasons = "rejected", ["얼굴 없음"]
                elif target_similarity < threshold:
                    status, reasons = "rejected", ["기준 인물 유사도 미달"]
                else:
                    if min(width, height) < min_side:
                        reasons.append("낮은 해상도")
                    if largest_ratio < min_face_ratio:
                        reasons.append("얼굴이 작음")
                    if blur < blur_threshold:
                        reasons.append("흐림 후보")
                    if len(faces) > 1:
                        reasons.append("다중 얼굴")
                    status = "review" if reasons else "accepted"
            exact_seen.setdefault(sha, item["id"])
            phash_seen.append((phash_value, item["id"]))
            update_image(
                item["id"], sha256=sha, phash=str(phash_value), width=width, height=height,
                face_count=len(faces), target_similarity=round(target_similarity, 4),
                quality_score=quality, status=status, reason=", ".join(reasons),
            )
        except Exception as exc:
            update_image(item["id"], status="rejected", reason=f"분석 실패: {str(exc)[:300]}")
        progress(job["id"], index, len(rows), path.name)


def export_dataset(job):
    root = ensure_dataset_dirs(job["dataset_id"])
    export_path = root / "exports" / f"dataset_{now_ts()}.zip"
    with connect() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM images WHERE dataset_id=? AND status='accepted' ORDER BY created_at",
            (job["dataset_id"],),
        ).fetchall()]
    manifest = []
    with zipfile.ZipFile(export_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, item in enumerate(rows, 1):
            path = Path(item["path"])
            arcname = f"images/{index:04d}_{path.name}"
            archive.write(path, arcname)
            manifest.append({key: value for key, value in item.items() if key != "path"} | {"file": arcname})
            progress(job["id"], index, len(rows), path.name)
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    update_job(job["id"], message=str(export_path))


HANDLERS = {
    "collect_bing": collect_bing,
    "collect_search": collect_search,
    "collect_instagram": collect_instagram,
    "import_folder": import_folder,
    "analyze": analyze,
    "repair": repair_images,
    "export": export_dataset,
}


def run_job(job_id):
    with exclusive_worker_lock():
        job = get_job(job_id)
        if not job:
            raise ValueError("Job not found")
        if job["status"] in {"completed", "failed", "cancelled"}:
            return 0
        handler = HANDLERS.get(job["kind"])
        if not handler:
            raise ValueError(f"Unsupported job kind: {job['kind']}")
        update_job(job_id, status="running", started_at=now_ts(), message="작업 시작", error="")
        set_dataset_status(job["dataset_id"], "running")
        try:
            handler(job)
            if cancelled(job_id):
                update_job(job_id, status="cancelled", finished_at=now_ts(), message="사용자가 취소함")
            else:
                latest = get_job(job_id)
                update_job(job_id, status="completed", progress=1, finished_at=now_ts(), message=latest["message"] or "완료")
            set_dataset_status(job["dataset_id"], "ready")
        except Exception as exc:
            update_job(job_id, status="failed", finished_at=now_ts(), error=str(exc), message="작업 실패")
            set_dataset_status(job["dataset_id"], "error")
            traceback.print_exc()
            return 1
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    raise SystemExit(run_job(args.job))
