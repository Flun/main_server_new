import asyncio
import copy
import json
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import List
from urllib.parse import quote, urlparse

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

import logging

logger = logging.getLogger("media")

BASE_DIR = Path(__file__).resolve().parent
MEDIA_WORK_DIR = BASE_DIR / "media_jobs"
MEDIA_UPLOAD_DIR = MEDIA_WORK_DIR / "uploads"
MEDIA_OUTPUT_DIR = MEDIA_WORK_DIR / "outputs"
MEDIA_TEMP_DIR = MEDIA_WORK_DIR / "temp"
MEDIA_DOWNLOAD_DIR = MEDIA_WORK_DIR / "downloads"
MEDIA_AI_MODEL_DIR = MEDIA_WORK_DIR / "ai_models"
MEDIA_AI_PACKAGE_DIR = BASE_DIR / ".media_ai_packages"
MEDIA_AI_WORKER = BASE_DIR / "media_ai_worker.py"
MEDIA_ALLOWED_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".webm", ".avi", ".wmv", ".m4v",
    ".mp3", ".wav", ".aac", ".flac", ".ogg", ".opus", ".m4a"
}

for media_dir in (MEDIA_UPLOAD_DIR, MEDIA_OUTPUT_DIR, MEDIA_TEMP_DIR, MEDIA_DOWNLOAD_DIR, MEDIA_AI_MODEL_DIR):
    os.makedirs(media_dir, exist_ok=True)

router = APIRouter()
def find_media_tool(name):
    path = shutil.which(name)
    if path:
        return path
    local_candidates = [
        Path.cwd() / name,
        Path("/usr/local/bin") / name,
        Path("/usr/bin") / name,
        Path("/opt/ffmpeg") / "bin" / name,
        BASE_DIR / "ffmpeg" / "bin" / (name + ".exe" if os.name == "nt" else name),
    ]
    for candidate in local_candidates:
        if candidate.exists():
            return str(candidate)
    return None

def require_media_tool(name):
    path = find_media_tool(name)
    if not path:
        raise HTTPException(status_code=500, detail=f"{name} executable was not found in PATH.")
    return path

class YoutubeDownloadRequest(BaseModel):
    url: str

class MediaUrlDownloadRequest(BaseModel):
    url: str
    resolution: str = "1080"
    video_codec: str = "efficient"
    container: str = "mp4"
    period: str = "all"

MEDIA_DOWNLOAD_EXTENSIONS = MEDIA_ALLOWED_EXTENSIONS | {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp",
}
MEDIA_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp"}
MEDIA_DOWNLOAD_DOMAINS = {
    "youtube.com", "youtu.be", "x.com", "twitter.com", "t.co", "twimg.com",
    "reddit.com", "redd.it", "redditmedia.com", "redditstatic.com",
    "instagram.com", "cdninstagram.com", "fbcdn.net",
    "tiktok.com", "facebook.com", "fb.watch", "vimeo.com", "twitch.tv",
    "imgur.com", "imgur.io",
}

def is_allowed_media_download_host(hostname):
    hostname = (hostname or "").lower().rstrip(".")
    return any(hostname == domain or hostname.endswith("." + domain) for domain in MEDIA_DOWNLOAD_DOMAINS)

def validate_media_download_url(value):
    try:
        parsed = urlparse((value or "").strip())
    except ValueError:
        parsed = None
    if (
        not parsed
        or parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or not is_allowed_media_download_host(parsed.hostname)
    ):
        raise HTTPException(
            status_code=400,
            detail="지원 URL을 입력하세요: YouTube, X/Twitter, Reddit, Instagram, TikTok, Facebook, Vimeo, Twitch, Imgur.",
        )
    return value.strip()

def media_download_cookie_file(url):
    if "instagram.com" not in (urlparse(url).hostname or "").lower():
        return None
    try:
        from dataset_store import shared_instagram_cookie_path
        path = shared_instagram_cookie_path()
        return path if path.is_file() else None
    except Exception:
        return None

def build_media_download_format(resolution, video_codec):
    height_filter = "" if resolution == "best" else f"[height<=?{resolution}]"
    codec_filters = {
        "av1": "[vcodec^=av01]",
        "h265": "[vcodec^=hev]",
        "vp9": "[vcodec^=vp9]",
        "h264": "[vcodec^=avc]",
    }
    codec_filter = codec_filters.get(video_codec, "")
    preferred = f"bv*{height_filter}{codec_filter}+ba/b{height_filter}{codec_filter}"
    fallback = f"bv*{height_filter}+ba/b{height_filter}"
    return preferred if not codec_filter else f"{preferred}/{fallback}"

MEDIA_DOWNLOAD_PERIOD_DAYS = {
    "today": 0,
    "month": 30,
    "year": 365,
    "five_years": 365 * 5,
    "all": None,
}

def safe_download_component(value, fallback="unknown", max_length=120):
    value = " ".join(str(value or "").split())
    value = "".join("_" if ch in '<>:"/\\|?*' or ord(ch) < 32 else ch for ch in value)
    value = value.strip(" ._")[:max_length].rstrip(" ._") or fallback
    if value.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        value = f"_{value}"
    return value

def classify_media_url(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    if host == "youtu.be" or host.endswith(".youtu.be"):
        return "youtube", False
    if host == "youtube.com" or host.endswith(".youtube.com"):
        first = parts[0].lower() if parts else ""
        individual = first in {"watch", "shorts", "live", "embed"} or bool(parsed.query and "v=" in parsed.query)
        collection = not individual and bool(parts) and (parts[0].startswith("@") or first in {"channel", "c", "user"})
        return "youtube", collection
    if host == "instagram.com" or host.endswith(".instagram.com"):
        first = parts[0].lower() if parts else ""
        collection = bool(parts) and first not in {"p", "reel", "tv", "stories", "explore", "direct"}
        return "instagram", collection
    return "other", False

def media_url_kind(url):
    platform, collection = classify_media_url(url)
    if platform == "youtube" and collection:
        parts = [part.lower() for part in urlparse(url).path.split("/") if part]
        if len(parts) >= 2 and parts[-1] == "shorts":
            return "youtube_shorts"
    return f"{platform}_{'collection' if collection else 'item'}"

def media_period_date(period):
    if period not in MEDIA_DOWNLOAD_PERIOD_DAYS:
        raise HTTPException(status_code=400, detail="지원하지 않는 다운로드 기간입니다.")
    days = MEDIA_DOWNLOAD_PERIOD_DAYS[period]
    if days is None:
        return None
    return date.today() if days == 0 else date.today() - timedelta(days=days)

def gallery_dl_command():
    try:
        import gallery_dl  # noqa: F401
        return [sys.executable, "-m", "gallery_dl"]
    except ImportError:
        bundled = BASE_DIR / ".dataset_venv" / "bin" / "python" if os.name != "nt" else BASE_DIR / ".dataset_venv" / "Scripts" / "python.exe"
        if bundled.is_file():
            return [str(bundled), "-m", "gallery_dl"]
        raise HTTPException(status_code=500, detail="gallery-dl이 설치되어 있지 않습니다.")

def resolve_instagram_identity(url, cookie_file):
    command = gallery_dl_command() + [
        "--quiet", "--simulate", "--range", "1", "--print", "{username}\t{owner_id}",
    ]
    if cookie_file:
        command += ["--cookies", str(cookie_file)]
    command.append(url)
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    for line in reversed((result.stdout or "").splitlines()):
        username, separator, owner_id = line.strip().partition("\t")
        if separator and username and owner_id.isdigit():
            return username, owner_id
    detail = (result.stderr or result.stdout or "Instagram 계정 정보를 확인하지 못했습니다.")[-1500:]
    raise HTTPException(status_code=400, detail=f"Instagram URL 확인 실패: {detail}")

def resolve_youtube_identity(url):
    try:
        import yt_dlp
    except ImportError:
        raise HTTPException(status_code=500, detail="yt-dlp이 설치되어 있지 않습니다.")
    options = {
        "extract_flat": "in_playlist",
        "playlistend": 1,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
    }
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        message = str(exc).replace("ERROR: ", "").strip()
        raise HTTPException(status_code=400, detail=f"YouTube URL 확인 실패: {message[-1500:]}")
    first = next(iter(info.get("entries") or ()), {}) or {}
    channel = (
        info.get("channel") or info.get("uploader") or info.get("title")
        or first.get("channel") or first.get("uploader") or "youtube"
    )
    return safe_download_component(channel, "youtube")

MEDIA_DOWNLOAD_PERIOD_DAYS = {
    "today": 0,
    "month": 30,
    "year": 365,
    "five_years": 365 * 5,
    "all": None,
}

def safe_download_component(value, fallback="unknown", max_length=120):
    value = " ".join(str(value or "").split())
    value = "".join("_" if ch in '<>:"/\\|?*' or ord(ch) < 32 else ch for ch in value)
    value = value.strip(" ._")[:max_length].rstrip(" ._") or fallback
    reserved = {"CON", "PRN", "AUX", "NUL"}
    reserved.update(f"COM{i}" for i in range(1, 10))
    reserved.update(f"LPT{i}" for i in range(1, 10))
    return f"_{value}" if value.upper() in reserved else value

def classify_media_url(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    if host == "youtu.be" or host.endswith(".youtu.be"):
        return "youtube", False
    if host == "youtube.com" or host.endswith(".youtube.com"):
        first = parts[0].lower() if parts else ""
        individual = first in {"watch", "shorts", "live", "embed"} or "v=" in parsed.query
        collection = not individual and bool(parts) and (parts[0].startswith("@") or first in {"channel", "c", "user"})
        return "youtube", collection
    if host == "instagram.com" or host.endswith(".instagram.com"):
        first = parts[0].lower() if parts else ""
        collection = bool(parts) and first not in {"p", "reel", "tv", "stories", "explore", "direct"}
        return "instagram", collection
    return "other", False

def media_period_date(period):
    if period not in MEDIA_DOWNLOAD_PERIOD_DAYS:
        raise HTTPException(status_code=400, detail="지원하지 않는 다운로드 기간입니다.")
    days = MEDIA_DOWNLOAD_PERIOD_DAYS[period]
    if days is None:
        return None
    return date.today() if days == 0 else date.today() - timedelta(days=days)

def gallery_dl_command():
    try:
        import gallery_dl  # noqa: F401
        return [sys.executable, "-m", "gallery_dl"]
    except ImportError:
        bundled = BASE_DIR / ".dataset_venv" / "bin" / "python" if os.name != "nt" else BASE_DIR / ".dataset_venv" / "Scripts" / "python.exe"
        if bundled.is_file():
            return [str(bundled), "-m", "gallery_dl"]
        raise HTTPException(status_code=500, detail="gallery-dl이 설치되어 있지 않습니다.")

def resolve_instagram_identity(url, cookie_file):
    command = gallery_dl_command() + [
        "--quiet", "--simulate", "--range", "1", "--print", "{username}\t{owner_id}",
    ]
    if cookie_file:
        command += ["--cookies", str(cookie_file)]
    command.append(url)
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    for line in reversed((result.stdout or "").splitlines()):
        username, separator, owner_id = line.strip().partition("\t")
        if separator and username and owner_id.isdigit():
            return username, owner_id
    detail = (result.stderr or result.stdout or "Instagram 계정 정보를 확인하지 못했습니다.")[-1500:]
    raise HTTPException(status_code=400, detail=f"Instagram URL 확인 실패: {detail}")

def resolve_youtube_identity(url):
    try:
        import yt_dlp
    except ImportError:
        raise HTTPException(status_code=500, detail="yt-dlp이 설치되어 있지 않습니다.")
    options = {"extract_flat": "in_playlist", "playlistend": 1, "quiet": True, "no_warnings": True, "socket_timeout": 30}
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        message = str(exc).replace("ERROR: ", "").strip()
        raise HTTPException(status_code=400, detail=f"YouTube URL 확인 실패: {message[-1500:]}")
    first = next(iter(info.get("entries") or ()), {}) or {}
    channel = info.get("channel") or info.get("uploader") or first.get("channel") or first.get("uploader") or info.get("title")
    return safe_download_component(channel, "youtube")

def list_downloaded_media(job_dir):
    return sorted(
        (
            path for path in job_dir.rglob("*")
            if path.is_file()
            and path.suffix.lower() in MEDIA_DOWNLOAD_EXTENSIONS
            and not path.name.endswith((".part", ".ytdl"))
        ),
        key=lambda path: path.name.lower(),
    )

def directory_download_size(path):
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total

def run_gallery_media_download(url, job_dir, cookie_file, cutoff=None, progress_callback=None):
    command = gallery_dl_command() + [
        "--directory", str(job_dir),
        "--filename", "{date:%Y-%m-%d}_{description[:80]}_{post_shortcode}_{num:>02}.{extension}",
        "--windows-filenames",
        "--no-mtime",
        "--Print", "prepare:__MEDIA_PROGRESS__{_filename}",
    ]
    if cutoff:
        command += ["--date-after", cutoff.isoformat()]
    if classify_media_url(url) == ("instagram", True):
        command += ["--option", "extractor.instagram.include=posts,reels"]
    if cookie_file:
        command += ["--cookies", str(cookie_file)]
    command.append(url)
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    output_lines = []
    output_queue = queue.Queue()
    def read_gallery_output():
        for line in process.stdout:
            output_queue.put(line)
    reader = threading.Thread(target=read_gallery_output, daemon=True)
    reader.start()
    item_index = 0
    current_file = ""
    initial_size = last_size = directory_download_size(job_dir)
    last_check = time.monotonic()
    smoothed_speed = 0
    while process.poll() is None:
        while True:
            try:
                line = output_queue.get_nowait()
            except queue.Empty:
                break
            output_lines.append(line)
            if line.startswith("__MEDIA_PROGRESS__"):
                item_index += 1
                current_file = Path(line.removeprefix("__MEDIA_PROGRESS__").strip()).name
        now = time.monotonic()
        current_size = directory_download_size(job_dir)
        elapsed = max(now - last_check, 0.001)
        instant_speed = max(0, current_size - last_size) / elapsed
        smoothed_speed = instant_speed if not smoothed_speed else (smoothed_speed * 0.7 + instant_speed * 0.3)
        if progress_callback:
            progress_callback({
                "stage": "downloading", "item_index": item_index, "item_total": None,
                "current_file": current_file, "speed": smoothed_speed or None,
                "downloaded_bytes": max(0, current_size - initial_size), "total_bytes": None, "eta": None,
            })
        last_size, last_check = current_size, now
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    reader.join(timeout=2)
    while not output_queue.empty():
        output_lines.append(output_queue.get_nowait())
    result = subprocess.CompletedProcess(command, process.returncode, "".join(output_lines), "")
    logger.info("[media-download] gallery-dl exit=%s", result.returncode)
    return result

def run_ytdlp_media_download(url, job_dir, resolution, video_codec, container, cookie_file, cutoff=None, progress_callback=None):
    import yt_dlp

    ffmpeg_path = find_media_tool("ffmpeg")
    # Keep resolution first for every preset.  In particular, "best" must not
    # be sorted primarily by codec or ascending file size, since that can make
    # yt-dlp prefer a tiny 240p encode over the highest-resolution stream.
    sort_fields = ["res" if resolution == "best" else f"res:{resolution}"]
    if video_codec == "efficient":
        sort_fields.append("vcodec")
    options = {
        "format": build_media_download_format(resolution, video_codec),
        "format_sort": sort_fields,
        "merge_output_format": container,
        # Actual dimensions and selected format IDs make downloads made with
        # different quality/codec settings coexist in the same channel folder.
        "outtmpl": str(job_dir / "%(upload_date>%Y-%m-%d)s_%(title).120B_%(id)s_%(resolution)s_%(format_id)s.%(ext)s"),
        "windowsfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "max_filesize": 2 * 1024 * 1024 * 1024,
        "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": container}],
    }
    if progress_callback:
        progress_state = {"item_index": None, "item_started": time.monotonic(), "completed_seconds": 0.0, "completed_items": 0}
        def ytdlp_progress(data):
            info = data.get("info_dict") or {}
            item_index = info.get("playlist_index") or 1
            item_total = info.get("playlist_count") or info.get("n_entries")
            status = data.get("status")
            now = time.monotonic()
            if progress_state["item_index"] is None:
                progress_state["item_index"] = item_index
                progress_state["item_started"] = now
            elif item_index != progress_state["item_index"]:
                progress_state["completed_seconds"] += max(0, now - progress_state["item_started"])
                progress_state["completed_items"] += 1
                progress_state["item_index"] = item_index
                progress_state["item_started"] = now
            eta = data.get("eta")
            if item_total and item_index and item_total > item_index and progress_state["completed_items"]:
                average_item_seconds = progress_state["completed_seconds"] / progress_state["completed_items"]
                eta = (eta or 0) + average_item_seconds * (item_total - item_index)
            progress_callback({
                "stage": "processing" if status == "finished" else "downloading",
                "item_index": item_index,
                "item_total": item_total,
                "current_file": Path(data.get("filename") or info.get("_filename") or info.get("title") or "").name,
                "speed": data.get("speed"),
                "eta": eta,
                "downloaded_bytes": data.get("downloaded_bytes"),
                "total_bytes": data.get("total_bytes") or data.get("total_bytes_estimate"),
            })
        options["progress_hooks"] = [ytdlp_progress]
    if ffmpeg_path:
        options["ffmpeg_location"] = str(Path(ffmpeg_path).parent)
    if cookie_file:
        options["cookiefile"] = str(cookie_file)
    if cutoff:
        options["dateafter"] = cutoff.strftime("%Y%m%d")
    with yt_dlp.YoutubeDL(options) as downloader:
        return downloader.extract_info(url, download=True)

def existing_ytdlp_downloads(info, job_dir):
    """Return files matching the exact formats selected for a yt-dlp result."""
    if not info:
        return []
    entries = list(info.get("entries") or ()) if info.get("_type") in {"playlist", "multi_video"} else [info]
    markers = set()
    for entry in entries:
        entry = entry or {}
        media_id = str(entry.get("id") or "")
        resolution = str(entry.get("resolution") or "")
        format_id = str(entry.get("format_id") or "")
        if media_id and resolution and format_id:
            markers.add(f"_{media_id}_{resolution}_{format_id}")
    if not markers:
        return []
    return [
        path for path in list_downloaded_media(job_dir)
        if any(marker in path.stem for marker in markers)
    ]

def download_social_media(payload, job_dir, before_state=None, progress_callback=None):
    url = validate_media_download_url(payload.url)
    resolution = payload.resolution if payload.resolution in {"best", "2160", "1440", "1080", "720", "480"} else "1080"
    video_codec = payload.video_codec if payload.video_codec in {"efficient", "av1", "h265", "vp9", "h264"} else "efficient"
    container = payload.container if payload.container in {"mp4", "mkv", "webm"} else "mp4"
    cookie_file = media_download_cookie_file(url)
    platform, collection = classify_media_url(url)
    cutoff = media_period_date(payload.period if collection else "all")

    gallery_result = None
    ytdlp_error = None
    ytdlp_info = None
    if platform == "instagram":
        gallery_result = run_gallery_media_download(url, job_dir, cookie_file, cutoff, progress_callback)
    elif platform == "youtube":
        try:
            ytdlp_info = run_ytdlp_media_download(url, job_dir, resolution, video_codec, container, cookie_file, cutoff, progress_callback)
        except Exception as exc:
            ytdlp_error = str(exc).replace("ERROR: ", "").strip()
    else:
        gallery_result = run_gallery_media_download(url, job_dir, cookie_file, progress_callback=progress_callback)
        try:
            ytdlp_info = run_ytdlp_media_download(url, job_dir, resolution, video_codec, container, cookie_file, progress_callback=progress_callback)
        except Exception as exc:
            ytdlp_error = str(exc).replace("ERROR: ", "").strip()

    files = list_downloaded_media(job_dir)
    if before_state is not None:
        files = [
            path for path in files
            if before_state.get(path.resolve()) != (path.stat().st_size, path.stat().st_mtime_ns)
        ]
    if not files:
        existing_files = existing_ytdlp_downloads(ytdlp_info, job_dir)
        if existing_files:
            names = ", ".join(path.name for path in existing_files[:3])
            raise HTTPException(
                status_code=409,
                detail=f"같은 화질과 형식의 파일이 이미 다운로드되어 있습니다: {names}",
            )
        gallery_detail = (gallery_result.stderr or gallery_result.stdout) if gallery_result else None
        details = ytdlp_error or gallery_detail or "선택한 기간에 다운로드할 미디어가 없습니다."
        raise HTTPException(status_code=400, detail=f"미디어 다운로드 실패: {details[-2000:]}")
    return files

def download_social_media_to_named_folder(payload, progress_callback=None):
    url = validate_media_download_url(payload.url)
    platform, _ = classify_media_url(url)
    cookie_file = media_download_cookie_file(url)
    if progress_callback:
        progress_callback({"stage": "resolving", "current_file": "계정 및 미디어 정보 확인 중"})
    if platform == "instagram":
        username, owner_id = resolve_instagram_identity(url, cookie_file)
        folder_name = safe_download_component(f"{username}_{owner_id}", "instagram")
    elif platform == "youtube":
        folder_name = resolve_youtube_identity(url)
    else:
        folder_name = uuid.uuid4().hex
    job_dir = MEDIA_DOWNLOAD_DIR / folder_name
    job_dir.mkdir(parents=True, exist_ok=True)
    if progress_callback:
        progress_callback({"folder": str(job_dir.resolve())})
    before_state = {
        path.resolve(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in list_downloaded_media(job_dir)
    }
    files = download_social_media(payload, job_dir, before_state, progress_callback)
    return job_dir, files

def downloaded_media_results(files):
    results = []
    for path in files:
        relative = path.relative_to(MEDIA_DOWNLOAD_DIR).as_posix()
        suffix = path.suffix.lower()
        kind = "image" if suffix in MEDIA_IMAGE_EXTENSIONS else ("audio" if suffix in {".mp3", ".wav", ".aac", ".flac", ".ogg", ".opus", ".m4a"} else "video")
        results.append({
            "name": path.name,
            "kind": kind,
            "size": path.stat().st_size,
            "path": str(path.resolve()),
            "url": f"/api/media/download-file/{quote(relative, safe='/')}",
        })
    return results

def update_media_download_job(job_id, values):
    with MEDIA_DOWNLOAD_JOBS_LOCK:
        job = MEDIA_DOWNLOAD_JOBS.get(job_id)
        if job:
            job.update(values)
            job["updated_at"] = time.time()

def run_media_download_job(job_id, payload):
    try:
        update_media_download_job(job_id, {"status": "running", "stage": "resolving"})
        progress_callback = lambda values: update_media_download_job(job_id, values)
        job_dir, files = download_social_media_to_named_folder(payload, progress_callback)
        platform, collection = classify_media_url(payload.url)
        results = downloaded_media_results(files)
        update_media_download_job(job_id, {
            "status": "completed", "stage": "completed", "folder": str(job_dir.resolve()),
            "platform": platform, "collection": collection, "url_kind": media_url_kind(payload.url),
            "files": results, "item_index": len(results), "item_total": len(results),
            "current_file": results[-1]["name"] if results else "", "speed": None, "eta": 0,
        })
    except Exception as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        update_media_download_job(job_id, {"status": "failed", "stage": "failed", "error": detail})

def create_media_download_job(payload):
    job_id = uuid.uuid4().hex
    now = time.time()
    platform, collection = classify_media_url(payload.url)
    job = {
        "id": job_id, "status": "queued", "stage": "queued", "created_at": now, "updated_at": now,
        "platform": platform, "collection": collection, "url_kind": media_url_kind(payload.url),
        "folder": "", "files": [], "error": "", "item_index": 0, "item_total": None,
        "current_file": "", "speed": None, "eta": None, "downloaded_bytes": None, "total_bytes": None,
    }
    with MEDIA_DOWNLOAD_JOBS_LOCK:
        finished = sorted(
            (item for item in MEDIA_DOWNLOAD_JOBS.values() if item["status"] in {"completed", "failed"}),
            key=lambda item: item["updated_at"],
        )
        while len(MEDIA_DOWNLOAD_JOBS) >= MEDIA_DOWNLOAD_JOB_LIMIT and finished:
            MEDIA_DOWNLOAD_JOBS.pop(finished.pop(0)["id"], None)
        MEDIA_DOWNLOAD_JOBS[job_id] = job
    threading.Thread(target=run_media_download_job, args=(job_id, payload), daemon=True).start()
    return job

def validate_youtube_url(value):
    try:
        parsed = urlparse((value or "").strip())
    except ValueError:
        parsed = None
    hostname = (parsed.hostname or "").lower() if parsed else ""
    allowed_hosts = {
        "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
        "youtu.be", "www.youtu.be",
    }
    if not parsed or parsed.scheme not in {"http", "https"} or hostname not in allowed_hosts:
        raise HTTPException(status_code=400, detail="Enter a valid YouTube video URL.")
    return value.strip()

def download_youtube_media(url, job_dir):
    try:
        import yt_dlp
    except ImportError:
        raise HTTPException(status_code=500, detail="yt-dlp is not installed. Run pip install yt-dlp.")

    ffmpeg_path = find_media_tool("ffmpeg")
    options = {
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": str(job_dir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "restrictfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "max_filesize": 2 * 1024 * 1024 * 1024,
    }
    if ffmpeg_path:
        options["ffmpeg_location"] = str(Path(ffmpeg_path).parent)

    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=True)
            requested = info.get("requested_downloads") or []
            candidates = [entry.get("filepath") for entry in requested if entry.get("filepath")]
            candidates.append(downloader.prepare_filename(info))
    except yt_dlp.utils.DownloadError as exc:
        message = str(exc).replace("ERROR: ", "").strip()
        raise HTTPException(status_code=400, detail=f"YouTube download failed: {message[-1500:]}")

    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            output = Path(candidate)
            break
    else:
        files = [path for path in job_dir.iterdir() if path.is_file() and not path.name.endswith((".part", ".ytdl"))]
        if not files:
            raise HTTPException(status_code=500, detail="The downloaded YouTube media file was not found.")
        output = max(files, key=lambda path: path.stat().st_mtime)

    video_id = "".join(ch for ch in str(info.get("id") or "video") if ch.isalnum() or ch in "_-")[:48] or "video"
    return output, f"youtube_{video_id}{output.suffix.lower()}"

def safe_media_filename(filename):
    name = Path(filename or "upload.bin").name
    stem = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in Path(name).stem).strip() or "media"
    ext = Path(name).suffix.lower()
    if ext not in MEDIA_ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported media extension: {ext or '(none)'}")
    return f"{stem[:80]}{ext}"

def run_media_command(args):
    logger.info("[media] ffmpeg command: %s", " ".join(str(a) for a in args))
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "FFmpeg failed").strip()
        raise HTTPException(status_code=500, detail=message[-3000:])

def get_media_ai_status():
    try:
        from config import settings as cfg

        comfy_py = cfg.get("comfyui_python")
    except Exception:
        comfy_py = ""
    python_path = Path(comfy_py) if comfy_py else None
    package_ready = (MEDIA_AI_PACKAGE_DIR / "audio_separator").is_dir()
    model_cached = any(MEDIA_AI_MODEL_DIR.glob("model_bs_roformer_ep_317_sdr_12.9755*"))
    return {
        "available": bool(python_path and python_path.is_file() and package_ready and MEDIA_AI_WORKER.is_file()),
        "gpu": "NVIDIA CUDA (ComfyUI runtime)" if python_path and python_path.is_file() else None,
        "model_cached": model_cached,
        "setup": "setup_media_ai.sh",
    }

def run_voice_isolation(ffmpeg_path, source, clip, output, mode, audio_codec):
    status = get_media_ai_status()
    if not status["available"]:
        raise HTTPException(status_code=503, detail="Media AI is not installed. Run setup_media_ai.sh, then restart the server.")

    job_dir = MEDIA_TEMP_DIR / f"voice_{uuid.uuid4().hex}"
    separation_dir = job_dir / "separated"
    job_dir.mkdir(parents=True, exist_ok=False)
    extracted = job_dir / "input.wav"
    try:
        clip_duration = None
        if clip["end"] is not None:
            clip_duration = clip["end"] - clip["start"]
        else:
            ffprobe_path = find_media_tool("ffprobe")
            if ffprobe_path:
                probe = subprocess.run(
                    [ffprobe_path, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(source)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                )
                try:
                    clip_duration = max(float(probe.stdout.strip()) - clip["start"], 0.01) if probe.returncode == 0 else None
                except ValueError:
                    clip_duration = None
        extract_args = [ffmpeg_path, "-y"]
        if clip["start"]:
            extract_args += ["-ss", str(clip["start"])]
        extract_args += ["-i", str(source)]
        # The Roformer model needs at least ten seconds. Padding is removed again
        # during final encoding, so short selections keep their exact duration.
        audio_filter = "apad=whole_dur=12"
        if clip_duration is not None:
            audio_filter = f"atrim=duration={clip_duration}," + audio_filter
        extract_args += ["-vn", "-af", audio_filter, "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(extracted)]
        run_media_command(extract_args)

        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        ffmpeg_dir = str(Path(ffmpeg_path).parent)
        env["PATH"] = ffmpeg_dir + os.pathsep + env.get("PATH", "")
        try:
            from config import settings as cfg

            comfy_py = cfg.get("comfyui_python") or "python3"
        except Exception:
            comfy_py = "python3"
        command = [
            comfy_py,
            str(MEDIA_AI_WORKER.resolve()),
            "--input", str(extracted.resolve()),
            "--output-dir", str(separation_dir.resolve()),
            "--model-dir", str(MEDIA_AI_MODEL_DIR.resolve()),
        ]
        logger.info("[media-ai] voice isolation mode=%s", mode)
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=7200,
        )
        combined_log = "\n".join(part for part in (result.stdout, result.stderr) if part)
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"AI voice isolation failed: {combined_log[-4000:]}")
        result_line = next((line for line in result.stdout.splitlines() if line.startswith("MEDIA_AI_RESULT=")), None)
        isolated = Path(result_line.split("=", 1)[1]) if result_line else None
        if not isolated or not isolated.is_file():
            raise HTTPException(status_code=500, detail="AI voice isolation completed without an output file.")

        encode_args = [ffmpeg_path, "-y", "-i", str(isolated)]
        if clip_duration is not None:
            encode_args += ["-t", str(clip_duration)]
        if mode == "dialogue":
            encode_args += [
                "-af",
                "highpass=f=70,lowpass=f=14000,afftdn=nf=-25:tn=1,dynaudnorm=f=150:g=7",
            ]
        encode_args += build_audio_codec_args(audio_codec) + [str(output)]
        run_media_command(encode_args)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="AI voice isolation exceeded the 2 hour processing limit.")
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)

def parse_media_clips(clips_json, file_count):
    try:
        raw_clips = json.loads(clips_json) if clips_json else []
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid clips_json payload.")
    if not raw_clips:
        raw_clips = [{"index": idx, "enabled": True} for idx in range(file_count)]

    clips = []
    for clip in raw_clips:
        if not clip.get("enabled", True):
            continue
        index = int(clip.get("index", len(clips)))
        if index < 0 or index >= file_count:
            raise HTTPException(status_code=400, detail="Clip index is outside uploaded file range.")
        start = max(float(clip.get("start") or 0), 0)
        end_value = clip.get("end")
        end = None if end_value in (None, "", 0, "0") else max(float(end_value), 0)
        if end is not None and end <= start:
            raise HTTPException(status_code=400, detail="Clip end time must be greater than start time.")
        clips.append({
            "index": index,
            "start": start,
            "end": end,
            "delay": max(float(clip.get("delay") or 0), 0),
            "volume": max(float(clip.get("volume") or 1), 0),
        })
    if not clips:
        raise HTTPException(status_code=400, detail="No enabled clips were provided.")
    return clips

def output_extension(output_kind, fmt):
    if fmt and fmt != "auto":
        return "." + fmt.lower().lstrip(".")
    if output_kind == "audio":
        return ".mp3"
    return ".mp4"

def build_video_codec_args(codec, compression):
    crf_by_level = {"lossless": "0", "high": "18", "medium": "23", "low": "30"}
    if codec == "copy":
        return ["-c:v", "copy"]
    if codec == "hevc":
        return ["-c:v", "libx265", "-crf", crf_by_level.get(compression, "23"), "-preset", "medium"]
    if codec == "vp9":
        return ["-c:v", "libvpx-vp9", "-crf", crf_by_level.get(compression, "30"), "-b:v", "0"]
    if codec == "av1":
        return ["-c:v", "libaom-av1", "-crf", crf_by_level.get(compression, "30"), "-b:v", "0"]
    return ["-c:v", "libx264", "-crf", crf_by_level.get(compression, "23"), "-preset", "medium"]

def build_audio_codec_args(codec):
    if codec == "copy":
        return ["-c:a", "copy"]
    if codec == "mp3":
        return ["-c:a", "libmp3lame", "-b:a", "192k"]
    if codec == "opus":
        return ["-c:a", "libopus", "-b:a", "160k"]
    if codec == "flac":
        return ["-c:a", "flac"]
    if codec == "pcm_s16le":
        return ["-c:a", "pcm_s16le"]
    return ["-c:a", "aac", "-b:a", "192k"]

def audio_codec_for_format(output_format, requested_codec):
    """Return a codec that the selected audio-only container can always accept."""
    required_codecs = {
        "mp3": "mp3",
        "wav": "pcm_s16le",
        "m4a": "aac",
        "aac": "aac",
        "flac": "flac",
        "ogg": "opus",
        "opus": "opus",
    }
    return required_codecs.get((output_format or "").lower(), requested_codec)

async def save_media_uploads(files: List[UploadFile], job_id):
    saved = []
    for idx, upload in enumerate(files):
        filename = safe_media_filename(upload.filename)
        target = MEDIA_UPLOAD_DIR / f"{job_id}_{idx}_{filename}"
        with open(target, "wb") as f:
            shutil.copyfileobj(upload.file, f)
        saved.append(target)
    return saved

def convert_single_media(ffmpeg_path, source, output, output_kind, video_codec, audio_codec, compression, black_video):
    args = [ffmpeg_path, "-y", "-i", str(source)]
    if output_kind == "audio":
        args += ["-vn"] + build_audio_codec_args(audio_codec)
    elif black_video:
        args = [
            ffmpeg_path, "-y",
            "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=30",
            "-i", str(source),
            "-shortest",
        ] + build_video_codec_args(video_codec, compression) + build_audio_codec_args(audio_codec)
    else:
        args += build_video_codec_args(video_codec, compression) + build_audio_codec_args(audio_codec)
    args += [str(output)]
    run_media_command(args)

def merge_media(ffmpeg_path, inputs, clips, output, output_kind, video_codec, audio_codec, compression, black_video):
    segment_paths = []
    for pos, clip in enumerate(clips):
        source = inputs[clip["index"]]
        segment = MEDIA_TEMP_DIR / f"{output.stem}_segment_{pos}.mkv"
        args = [ffmpeg_path, "-y"]
        if clip["start"]:
            args += ["-ss", str(clip["start"])]
        args += ["-i", str(source)]
        if clip["end"] is not None:
            args += ["-to", str(clip["end"] - clip["start"])]

        filters = []
        if clip["volume"] != 1:
            filters.append(f"volume={clip['volume']}")
        if clip["delay"] > 0:
            delay_ms = int(clip["delay"] * 1000)
            filters.append(f"adelay={delay_ms}:all=1")
        if filters:
            args += ["-af", ",".join(filters)]

        if output_kind == "audio":
            args += ["-vn"] + build_audio_codec_args("aac")
        else:
            args += build_video_codec_args("h264", "medium") + build_audio_codec_args("aac")
        args += [str(segment)]
        run_media_command(args)
        segment_paths.append(segment)

    concat_file = MEDIA_TEMP_DIR / f"{output.stem}_concat.txt"
    concat_file.write_text("".join(f"file '{path.resolve().as_posix()}'\n" for path in segment_paths), encoding="utf-8")

    if len(segment_paths) == 1 and black_video and output_kind == "video":
        convert_single_media(ffmpeg_path, segment_paths[0], output, output_kind, video_codec, audio_codec, compression, True)
        return

    args = [ffmpeg_path, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file)]
    if output_kind == "audio":
        args += ["-vn"] + build_audio_codec_args(audio_codec)
    else:
        args += build_video_codec_args(video_codec, compression) + build_audio_codec_args(audio_codec)
    args += [str(output)]
    run_media_command(args)

def mux_media(ffmpeg_path, inputs, clips, output, output_kind, video_codec, audio_codec, compression, black_video):
    args = [ffmpeg_path, "-y"]
    if black_video and output_kind == "video":
        args += ["-f", "lavfi", "-i", "color=c=black:s=1280x720:r=30"]
        media_offset = 1
    else:
        media_offset = 0
    for source in inputs:
        args += ["-i", str(source)]

    audio_filters = []
    audio_labels = []
    for pos, clip in enumerate(clips):
        input_idx = clip["index"] + media_offset
        label = f"a{pos}"
        parts = []
        if clip["end"] is not None:
            parts.append(f"atrim=start={clip['start']}:end={clip['end']}")
        elif clip["start"]:
            parts.append(f"atrim=start={clip['start']}")
        parts.append("asetpts=PTS-STARTPTS")
        if clip["volume"] != 1:
            parts.append(f"volume={clip['volume']}")
        if clip["delay"] > 0:
            delay_ms = int(clip["delay"] * 1000)
            parts.append(f"adelay={delay_ms}:all=1")
        audio_filters.append(f"[{input_idx}:a:0]{','.join(parts)}[{label}]")
        audio_labels.append(f"[{label}]")

    if audio_filters:
        if len(audio_labels) == 1:
            audio_filters.append(f"{audio_labels[0]}anull[aout]")
        else:
            audio_filters.append(f"{''.join(audio_labels)}amix=inputs={len(audio_labels)}:duration=longest:dropout_transition=2[aout]")
        args += ["-filter_complex", ";".join(audio_filters)]

    if output_kind == "audio":
        if audio_filters:
            args += ["-map", "[aout]"]
        else:
            args += ["-map", f"{media_offset}:a:0"]
        args += ["-vn"] + build_audio_codec_args(audio_codec)
    else:
        if black_video:
            args += ["-map", "0:v:0"]
        else:
            args += ["-map", f"{media_offset}:v:0"]
        if audio_filters:
            args += ["-map", "[aout]"]
        else:
            args += ["-map", f"{media_offset}:a:0?"]
        args += ["-shortest"] + build_video_codec_args(video_codec, compression) + build_audio_codec_args(audio_codec)
    args += [str(output)]
    run_media_command(args)

def split_media(ffmpeg_path, source, output, start, end, output_kind, video_codec, audio_codec, compression):
    args = [ffmpeg_path, "-y"]
    if start:
        args += ["-ss", str(max(float(start), 0))]
    args += ["-i", str(source)]
    if end:
        start_value = max(float(start or 0), 0)
        duration = max(float(end) - start_value, 0)
        if duration <= 0:
            raise HTTPException(status_code=400, detail="Split end time must be greater than start time.")
        args += ["-t", str(duration)]
    if output_kind == "audio":
        args += ["-vn"] + build_audio_codec_args(audio_codec)
    else:
        args += build_video_codec_args(video_codec, compression) + build_audio_codec_args(audio_codec)
    args += [str(output)]
    run_media_command(args)


@router.get("/api/media/ffmpeg")
def get_media_ffmpeg_status():
    ffmpeg_path = find_media_tool("ffmpeg")
    ffprobe_path = find_media_tool("ffprobe")
    version = None
    if ffmpeg_path:
        try:
            result = subprocess.run([ffmpeg_path, "-version"], capture_output=True, text=True, encoding="utf-8", errors="replace")
            version = (result.stdout or "").splitlines()[0] if result.stdout else None
        except Exception as e:
            version = str(e)
    return {"ffmpeg": ffmpeg_path, "ffprobe": ffprobe_path, "version": version}

@router.get("/api/media/ai-status")
def media_ai_status():
    return get_media_ai_status()

@router.post("/api/media/youtube")
async def download_youtube_source(payload: YoutubeDownloadRequest):
    url = validate_youtube_url(payload.url)
    request_payload = MediaUrlDownloadRequest(url=url, resolution="best", video_codec="efficient", container="mp4")
    _, files = await asyncio.to_thread(download_social_media_to_named_folder, request_payload)
    output = next((path for path in files if path.suffix.lower() not in MEDIA_IMAGE_EXTENSIONS), files[0])
    download_name = output.name
    return FileResponse(
        str(output),
        filename=download_name,
        media_type="application/octet-stream",
        headers={"X-Media-Filename": download_name},
    )

@router.post("/api/media/download-url")
async def download_media_url(payload: MediaUrlDownloadRequest):
    validate_media_download_url(payload.url)
    try:
        job_dir, files = await asyncio.to_thread(download_social_media_to_named_folder, payload)
    except Exception:
        raise

    results = downloaded_media_results(files)
    platform, collection = classify_media_url(payload.url)
    return {
        "status": "success",
        "folder": str(job_dir.resolve()),
        "platform": platform,
        "collection": collection,
        "files": results,
    }

@router.post("/api/media/download-jobs")
def start_media_download_job(payload: MediaUrlDownloadRequest):
    validate_media_download_url(payload.url)
    job = create_media_download_job(payload)
    return {"job_id": job["id"], "status": job["status"], "url_kind": job["url_kind"]}

@router.get("/api/media/download-jobs/{job_id}")
def get_media_download_job(job_id: str):
    with MEDIA_DOWNLOAD_JOBS_LOCK:
        job = MEDIA_DOWNLOAD_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="다운로드 작업을 찾지 못했습니다.")
        return copy.deepcopy(job)

@router.get("/api/media/download-file/{relative_path:path}")
def get_downloaded_media_file(relative_path: str):
    root = MEDIA_DOWNLOAD_DIR.resolve()
    target = (MEDIA_DOWNLOAD_DIR / relative_path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid download path.")
    if not target.is_file() or target.suffix.lower() not in MEDIA_DOWNLOAD_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Downloaded media file not found.")
    return FileResponse(str(target), filename=target.name, media_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream")

@router.post("/api/media/open-download-folder")
def open_media_download_folder():
    folder = MEDIA_DOWNLOAD_DIR.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            os.startfile(str(folder))
        else:
            subprocess.Popen(
                ["xdg-open", str(folder)], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"다운로드 폴더를 열지 못했습니다: {exc}")
    return {"status": "success", "folder": str(folder)}

@router.post("/api/media/probe")
async def probe_media(file: UploadFile = File(...)):
    ffprobe_path = require_media_tool("ffprobe")
    job_id = uuid.uuid4().hex
    saved = await save_media_uploads([file], job_id)
    target = saved[0]
    result = subprocess.run(
        [
            ffprobe_path, "-v", "error",
            "-show_entries", "format=duration,size,format_name:stream=index,codec_type,codec_name,width,height,sample_rate,channels",
            "-of", "json",
            str(target),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise HTTPException(status_code=400, detail=(result.stderr or "Unable to inspect media.").strip())
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="ffprobe returned invalid JSON.")
    return data

@router.post("/api/media/process")
async def process_media(
    operation: str = Form("merge"),
    output_kind: str = Form("video"),
    output_format: str = Form("mp4"),
    video_codec: str = Form("h264"),
    audio_codec: str = Form("aac"),
    compression: str = Form("medium"),
    clips_json: str = Form(""),
    split_start: str = Form("0"),
    split_end: str = Form(""),
    black_video: bool = Form(False),
    preview: bool = Form(False),
    selected_index: int = Form(0),
    files: List[UploadFile] = File(...),
):
    if not files:
        raise HTTPException(status_code=400, detail="Upload at least one media file.")
    ffmpeg_path = require_media_tool("ffmpeg")
    job_id = uuid.uuid4().hex
    inputs = await save_media_uploads(files, job_id)

    output_kind = output_kind if output_kind in {"video", "audio"} else "video"
    if preview:
        if output_kind == "audio":
            output_format = "mp3"
            audio_codec = "mp3"
        else:
            output_format = "mp4"
            video_codec = "h264"
            audio_codec = "aac"
            compression = "low"
    if output_kind == "audio":
        audio_codec = audio_codec_for_format(output_format, audio_codec)
    if operation in {"isolate_vocals", "isolate_dialogue"}:
        output_kind = "audio"
        audio_codec = audio_codec_for_format(output_format, audio_codec)
    output_prefix = "media_preview" if preview else "media"
    output = MEDIA_OUTPUT_DIR / f"{output_prefix}_{job_id}{output_extension(output_kind, output_format)}"

    if operation in {"isolate_vocals", "isolate_dialogue"}:
        clips = parse_media_clips(clips_json, len(inputs))
        clip = next((item for item in clips if item["index"] == selected_index), clips[0])
        mode = "vocals" if operation == "isolate_vocals" else "dialogue"
        await asyncio.to_thread(run_voice_isolation, ffmpeg_path, inputs[clip["index"]], clip, output, mode, audio_codec)
    elif operation == "split":
        source_index = min(max(selected_index, 0), len(inputs) - 1)
        split_media(ffmpeg_path, inputs[source_index], output, split_start, split_end, output_kind, video_codec, audio_codec, compression)
    elif operation == "convert":
        convert_single_media(ffmpeg_path, inputs[0], output, output_kind, video_codec, audio_codec, compression, black_video)
    elif operation == "mux":
        clips = parse_media_clips(clips_json, len(inputs))
        mux_media(ffmpeg_path, inputs, clips, output, output_kind, video_codec, audio_codec, compression, black_video)
    else:
        clips = parse_media_clips(clips_json, len(inputs))
        merge_media(ffmpeg_path, inputs, clips, output, output_kind, video_codec, audio_codec, compression, black_video)

    download_name = f"media_result{output.suffix}"
    return FileResponse(str(output), filename=download_name, media_type="application/octet-stream")

