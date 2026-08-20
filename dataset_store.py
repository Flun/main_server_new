import hashlib
import json
import shutil
import sqlite3
import time
import uuid
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATASET_ROOT = BASE_DIR / "dataset_jobs"
DB_PATH = DATASET_ROOT / "datasets.sqlite3"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
CLASSIFIED_STATUSES = ("accepted", "review", "rejected", "duplicate")


def now_ts():
    return int(time.time())


def compute_file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def connect():
    DATASET_ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS datasets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                subject_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'ready',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                progress REAL NOT NULL DEFAULT 0,
                current INTEGER NOT NULL DEFAULT 0,
                total INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                params_json TEXT NOT NULL DEFAULT '{}',
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                started_at INTEGER,
                finished_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
            CREATE TABLE IF NOT EXISTS images (
                id TEXT PRIMARY KEY,
                dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
                source TEXT NOT NULL,
                job_id TEXT NOT NULL DEFAULT '',
                path TEXT NOT NULL UNIQUE,
                original_url TEXT NOT NULL DEFAULT '',
                sha256 TEXT NOT NULL DEFAULT '',
                phash TEXT NOT NULL DEFAULT '',
                width INTEGER NOT NULL DEFAULT 0,
                height INTEGER NOT NULL DEFAULT 0,
                face_count INTEGER NOT NULL DEFAULT 0,
                target_similarity REAL,
                quality_score REAL,
                status TEXT NOT NULL DEFAULT 'new',
                reason TEXT NOT NULL DEFAULT '',
                repair_kind TEXT NOT NULL DEFAULT '',
                repaired_at INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_images_dataset_status
                ON images(dataset_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_images_job ON images(job_id);
            CREATE TABLE IF NOT EXISTS reference_images (
                id TEXT PRIMARY KEY,
                dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
                path TEXT NOT NULL UNIQUE,
                original_name TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL
            );
            """
        )
        image_columns = {row["name"] for row in conn.execute("PRAGMA table_info(images)")}
        if "job_id" not in image_columns:
            conn.execute("ALTER TABLE images ADD COLUMN job_id TEXT NOT NULL DEFAULT ''")
        if "repair_kind" not in image_columns:
            conn.execute("ALTER TABLE images ADD COLUMN repair_kind TEXT NOT NULL DEFAULT ''")
        if "repaired_at" not in image_columns:
            conn.execute("ALTER TABLE images ADD COLUMN repaired_at INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_images_job ON images(job_id)")
        # Register references created by older versions. Their original upload name
        # was not stored, so the UUID filename is retained as a fallback label.
        for dataset in conn.execute("SELECT id FROM datasets").fetchall():
            reference_dir = dataset_dir(dataset["id"]) / "references"
            if not reference_dir.exists():
                continue
            for path in reference_dir.iterdir():
                if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
                    conn.execute(
                        "INSERT OR IGNORE INTO reference_images(id,dataset_id,path,original_name,created_at) VALUES(?,?,?,?,?)",
                        (uuid.uuid4().hex, dataset["id"], str(path.resolve()), path.name, int(path.stat().st_mtime)),
                    )
        # Existing Bing images came from the most recent completed Bing job.
        for dataset in conn.execute("SELECT id FROM datasets").fetchall():
            job = conn.execute(
                "SELECT id FROM jobs WHERE dataset_id=? AND kind='collect_bing' AND status='completed' ORDER BY created_at DESC LIMIT 1",
                (dataset["id"],),
            ).fetchone()
            if job:
                conn.execute(
                    "UPDATE images SET job_id=? WHERE dataset_id=? AND source='bing' AND job_id=''",
                    (job["id"], dataset["id"]),
                )
        # Physically organize already-analyzed images by their current status.
        for image in conn.execute(
            "SELECT id,dataset_id,path,status FROM images WHERE status IN ('accepted','review','rejected','duplicate')"
        ).fetchall():
            source = Path(image["path"]).resolve()
            target = classified_image_path(image["dataset_id"], image["status"], image["id"], source.name)
            if source != target:
                if source.is_file() and not target.exists():
                    shutil.move(str(source), str(target))
                if target.is_file():
                    conn.execute("UPDATE images SET path=? WHERE id=?", (str(target), image["id"]))
        # Content hashes make collection idempotent even when providers return
        # the same bytes under a different URL or filename.
        for image in conn.execute("SELECT id,path FROM images WHERE sha256='' OR sha256 IS NULL").fetchall():
            path = Path(image["path"]).resolve()
            if path.is_file():
                try:
                    conn.execute("UPDATE images SET sha256=? WHERE id=?", (compute_file_sha256(path), image["id"]))
                except OSError:
                    pass
        # Backups created before repair metadata was introduced still count as
        # repaired images in the gallery filter.
        for dataset in conn.execute("SELECT id FROM datasets").fetchall():
            backup_dir = dataset_dir(dataset["id"]) / "repairs" / "originals"
            if not backup_dir.is_dir():
                continue
            for backup in backup_dir.iterdir():
                if not backup.is_file():
                    continue
                image_id = backup.stem
                conn.execute(
                    "UPDATE images SET repair_kind=CASE WHEN repair_kind='' THEN 'legacy' ELSE repair_kind END, "
                    "repaired_at=CASE WHEN repaired_at=0 THEN ? ELSE repaired_at END "
                    "WHERE id=? AND dataset_id=?",
                    (int(backup.stat().st_mtime), image_id, dataset["id"]),
                )
        # Promote the newest legacy per-dataset Instagram cookie file to shared storage.
        shared_cookie = shared_instagram_cookie_path()
        legacy_cookies = sorted(
            DATASET_ROOT.glob("[0-9a-f]*/credentials/instagram_cookies.txt"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if legacy_cookies and not shared_cookie.exists():
            shutil.copy2(legacy_cookies[0], shared_cookie)
        if shared_cookie.exists():
            for legacy_cookie in legacy_cookies:
                legacy_cookie.unlink(missing_ok=True)


def recover_running_jobs():
    with connect() as conn:
        conn.execute(
            "UPDATE jobs SET status='queued', message='서버 재시작 후 재개 대기' "
            "WHERE status='running'"
        )


def dataset_dir(dataset_id):
    value = str(dataset_id)
    if not value or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError("Invalid dataset id")
    path = (DATASET_ROOT / value).resolve()
    if DATASET_ROOT.resolve() not in path.parents:
        raise ValueError("Invalid dataset path")
    return path


def ensure_dataset_dirs(dataset_id):
    root = dataset_dir(dataset_id)
    for relative in ("raw/web", "raw/instagram", "raw/imported", "references", "exports"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    for status in CLASSIFIED_STATUSES:
        (root / "classified" / status).mkdir(parents=True, exist_ok=True)
    return root


def classified_root(dataset_id):
    return dataset_dir(dataset_id) / "classified"


def instagram_download_archive_path(dataset_id):
    folder = dataset_dir(dataset_id) / "collection_state"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "instagram_downloads.sqlite3"


def classified_image_path(dataset_id, status, image_id, filename):
    if status not in CLASSIFIED_STATUSES:
        raise ValueError("Invalid classified image status")
    folder = classified_root(dataset_id) / status
    folder.mkdir(parents=True, exist_ok=True)
    name = Path(filename).name
    if not name.startswith(f"{image_id}_"):
        name = f"{image_id}_{name}"
    return (folder / name).resolve()


def repair_backup_path(image):
    backup_dir = dataset_dir(image["dataset_id"]) / "repairs" / "originals"
    if not backup_dir.is_dir():
        return None
    matches = sorted(path.resolve() for path in backup_dir.glob(f"{image['id']}.*") if path.is_file())
    return matches[0] if matches else None


def shared_instagram_cookie_path():
    folder = DATASET_ROOT / "_shared" / "credentials"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "instagram_cookies.txt"


def create_dataset(name, subject_name=""):
    dataset_id = uuid.uuid4().hex
    stamp = now_ts()
    ensure_dataset_dirs(dataset_id)
    with connect() as conn:
        conn.execute(
            "INSERT INTO datasets(id,name,subject_name,created_at,updated_at) VALUES(?,?,?,?,?)",
            (dataset_id, name.strip(), subject_name.strip(), stamp, stamp),
        )
    return get_dataset(dataset_id)


def row_dict(row):
    return dict(row) if row else None


def get_dataset(dataset_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        if not row:
            return None
        data = row_dict(row)
        counts = conn.execute(
            "SELECT status, COUNT(*) count FROM images WHERE dataset_id=? GROUP BY status",
            (dataset_id,),
        ).fetchall()
        data["counts"] = {item["status"]: item["count"] for item in counts}
        data["reference_count"] = conn.execute(
            "SELECT COUNT(*) FROM reference_images WHERE dataset_id=?", (dataset_id,)
        ).fetchone()[0]
        data["repaired_count"] = conn.execute(
            "SELECT COUNT(*) FROM images WHERE dataset_id=? AND repair_kind<>''",
            (dataset_id,),
        ).fetchone()[0]
        issue_row = conn.execute(
            "SELECT "
            "SUM(CASE WHEN quality_score IS NOT NULL AND face_count=0 THEN 1 ELSE 0 END) no_face, "
            "SUM(CASE WHEN quality_score IS NOT NULL AND face_count>1 THEN 1 ELSE 0 END) multiple_faces, "
            "SUM(CASE WHEN instr(reason,'얼굴이 작음')>0 THEN 1 ELSE 0 END) small_face, "
            "SUM(CASE WHEN instr(reason,'흐림 후보')>0 THEN 1 ELSE 0 END) blurry, "
            "SUM(CASE WHEN instr(reason,'낮은 해상도')>0 THEN 1 ELSE 0 END) low_resolution, "
            "SUM(CASE WHEN instr(reason,'분석 실패:')>0 THEN 1 ELSE 0 END) analysis_failed "
            "FROM images WHERE dataset_id=?",
            (dataset_id,),
        ).fetchone()
        data["issue_counts"] = {key: int(issue_row[key] or 0) for key in issue_row.keys()}
        data["folder_path"] = str(classified_root(dataset_id))
        data["status_folders"] = {
            status: str(classified_root(dataset_id) / status) for status in CLASSIFIED_STATUSES
        }
        return data


def list_datasets():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM datasets ORDER BY created_at DESC").fetchall()
    return [get_dataset(row["id"]) for row in rows]


def create_job(dataset_id, kind, params):
    job_id = uuid.uuid4().hex
    stamp = now_ts()
    with connect() as conn:
        conn.execute(
            "INSERT INTO jobs(id,dataset_id,kind,params_json,created_at) VALUES(?,?,?,?,?)",
            (job_id, dataset_id, kind, json.dumps(params, ensure_ascii=False), stamp),
        )
        conn.execute("UPDATE datasets SET status='queued',updated_at=? WHERE id=?", (stamp, dataset_id))
    return get_job(job_id)


def get_job(job_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    data = row_dict(row)
    if data:
        data["params"] = json.loads(data.pop("params_json") or "{}")
        data["cancel_requested"] = bool(data["cancel_requested"])
    return data


def list_jobs(dataset_id=None, limit=50):
    query = "SELECT id FROM jobs"
    args = []
    if dataset_id:
        query += " WHERE dataset_id=?"
        args.append(dataset_id)
    query += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    args.append(limit)
    with connect() as conn:
        rows = conn.execute(query, args).fetchall()
    jobs = [get_job(row["id"]) for row in rows]
    with connect() as conn:
        for job in jobs:
            job["result_count"] = conn.execute(
                "SELECT COUNT(*) FROM images WHERE job_id=?", (job["id"],)
            ).fetchone()[0]
    return jobs


def update_job(job_id, **values):
    allowed = {"status", "progress", "current", "total", "message", "error", "started_at", "finished_at", "cancel_requested"}
    values = {key: value for key, value in values.items() if key in allowed}
    if not values:
        return
    assignments = ",".join(f"{key}=?" for key in values)
    with connect() as conn:
        conn.execute(f"UPDATE jobs SET {assignments} WHERE id=?", [*values.values(), job_id])


def next_queued_job():
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM jobs WHERE status='queued' AND cancel_requested=0 ORDER BY created_at, rowid LIMIT 1"
        ).fetchone()
    return row["id"] if row else None


def claim_next_queued_job():
    """Atomically claim one FIFO job only when no other job is active."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute("SELECT id FROM jobs WHERE status='running' LIMIT 1").fetchone()
        if active:
            return None
        row = conn.execute(
            "SELECT id FROM jobs WHERE status='queued' AND cancel_requested=0 "
            "ORDER BY created_at, rowid LIMIT 1"
        ).fetchone()
        if not row:
            return None
        stamp = now_ts()
        changed = conn.execute(
            "UPDATE jobs SET status='running',started_at=?,message='worker 시작 대기' "
            "WHERE id=? AND status='queued' AND cancel_requested=0",
            (stamp, row["id"]),
        ).rowcount
        return row["id"] if changed == 1 else None


def set_dataset_status(dataset_id, status):
    with connect() as conn:
        conn.execute(
            "UPDATE datasets SET status=?,updated_at=? WHERE id=?",
            (status, now_ts(), dataset_id),
        )


def add_image(dataset_id, source, path, original_url="", job_id=""):
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Image file not found: {resolved}")
    sha256 = compute_file_sha256(resolved)
    image_id = uuid.uuid4().hex
    duplicate = None
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        duplicate = conn.execute(
            "SELECT id,path FROM images WHERE dataset_id=? AND sha256=? LIMIT 1",
            (dataset_id, sha256),
        ).fetchone()
        if duplicate:
            duplicate = row_dict(duplicate)
        else:
            try:
                conn.execute(
                    "INSERT INTO images(id,dataset_id,source,job_id,path,original_url,sha256,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (image_id, dataset_id, source, job_id, str(resolved), original_url, sha256, now_ts()),
                )
            except sqlite3.IntegrityError:
                row = conn.execute("SELECT id FROM images WHERE path=?", (str(resolved),)).fetchone()
                return row["id"] if row else None
    if duplicate:
        existing = Path(duplicate["path"]).resolve()
        root = dataset_dir(dataset_id).resolve()
        if resolved != existing and root in resolved.parents and resolved.is_file():
            resolved.unlink()
        return None
    return image_id


def get_image(image_id):
    with connect() as conn:
        return row_dict(conn.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone())


def list_images(dataset_id, status=None, limit=200, offset=0):
    query = "SELECT * FROM images WHERE dataset_id=?"
    args = [dataset_id]
    if status == "repaired":
        query += " AND repair_kind<>''"
    elif status and status != "all":
        query += " AND status=?"
        args.append(status)
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    args.extend((min(limit, 500), max(offset, 0)))
    with connect() as conn:
        return [row_dict(row) for row in conn.execute(query, args).fetchall()]


def update_image(image_id, **values):
    allowed = {"sha256", "phash", "width", "height", "face_count", "target_similarity", "quality_score", "status", "reason", "repair_kind", "repaired_at"}
    values = {key: value for key, value in values.items() if key in allowed}
    if not values:
        return
    image = get_image(image_id)
    if not image:
        return
    source = Path(image["path"]).resolve()
    target = None
    status = values.get("status")
    if status in CLASSIFIED_STATUSES:
        target = classified_image_path(image["dataset_id"], status, image_id, source.name)
        if source != target:
            if not source.is_file():
                raise FileNotFoundError(f"Image file not found: {source}")
            shutil.move(str(source), str(target))
            values["path"] = str(target)
    assignments = ",".join(f"{key}=?" for key in values)
    try:
        with connect() as conn:
            conn.execute(f"UPDATE images SET {assignments} WHERE id=?", [*values.values(), image_id])
    except Exception:
        if target and target.is_file() and source != target:
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(source))
        raise


def restore_repaired_image(image_id):
    image = get_image(image_id)
    if not image:
        raise FileNotFoundError("Image not found")
    backup = repair_backup_path(image)
    if not backup:
        raise FileNotFoundError("Original backup not found")

    update_image(image_id, status="review")
    image = get_image(image_id)
    current = Path(image["path"]).resolve()
    temporary = current.with_name(current.name + ".restore.tmp")
    shutil.copy2(backup, temporary)
    temporary.replace(current)
    restored_sha256 = compute_file_sha256(current)
    update_image(
        image_id,
        sha256=restored_sha256,
        phash="",
        width=0,
        height=0,
        face_count=0,
        target_similarity=None,
        quality_score=None,
        status="review",
        reason="원본 복원 · 재분석 대기",
        repair_kind="",
        repaired_at=0,
    )
    backup.unlink(missing_ok=True)
    return get_image(image_id)


def add_reference(dataset_id, path, original_name):
    reference_id = uuid.uuid4().hex
    with connect() as conn:
        conn.execute(
            "INSERT INTO reference_images(id,dataset_id,path,original_name,created_at) VALUES(?,?,?,?,?)",
            (reference_id, dataset_id, str(Path(path).resolve()), original_name, now_ts()),
        )
    return reference_id


def list_references(dataset_id):
    with connect() as conn:
        return [row_dict(row) for row in conn.execute(
            "SELECT * FROM reference_images WHERE dataset_id=? ORDER BY created_at", (dataset_id,)
        ).fetchall()]


def get_reference(reference_id):
    with connect() as conn:
        return row_dict(conn.execute("SELECT * FROM reference_images WHERE id=?", (reference_id,)).fetchone())


def delete_reference(reference_id):
    reference = get_reference(reference_id)
    if not reference:
        return False
    path = Path(reference["path"]).resolve()
    root = dataset_dir(reference["dataset_id"]).resolve()
    if root in path.parents and path.is_file():
        path.unlink()
    backup = repair_backup_path(image)
    if backup:
        backup.unlink(missing_ok=True)
    if image["source"] == "instagram":
        instagram_download_archive_path(image["dataset_id"]).unlink(missing_ok=True)
    with connect() as conn:
        conn.execute("DELETE FROM reference_images WHERE id=?", (reference_id,))
    return True


def create_completed_job(dataset_id, kind, params, message="완료"):
    job_id = uuid.uuid4().hex
    stamp = now_ts()
    with connect() as conn:
        conn.execute(
            "INSERT INTO jobs(id,dataset_id,kind,status,progress,message,params_json,created_at,started_at,finished_at) "
            "VALUES(?,?,?,'completed',1,?,?,?,?,?)",
            (job_id, dataset_id, kind, message, json.dumps(params, ensure_ascii=False), stamp, stamp, stamp),
        )
        conn.execute("UPDATE datasets SET status='ready',updated_at=? WHERE id=?", (stamp, dataset_id))
    return get_job(job_id)


def delete_images(dataset_id, statuses=None, quality_below=None, similarity_below=None, job_id=None, issues=None, repaired_only=False, delete_all=False):
    clauses = ["dataset_id=?"]
    args = [dataset_id]
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        clauses.append(f"status IN ({placeholders})")
        args.extend(statuses)
    if quality_below is not None:
        clauses.append("quality_score IS NOT NULL AND quality_score<=?")
        args.append(float(quality_below))
    if similarity_below is not None:
        clauses.append("target_similarity IS NOT NULL AND target_similarity<=?")
        args.append(float(similarity_below))
    if job_id:
        clauses.append("job_id=?")
        args.append(job_id)
    if repaired_only:
        clauses.append("repair_kind<>''")
    issue_sql = {
        "no_face": "quality_score IS NOT NULL AND face_count=0",
        "multiple_faces": "quality_score IS NOT NULL AND face_count>1",
        "small_face": "instr(reason,'얼굴이 작음')>0",
        "blurry": "instr(reason,'흐림 후보')>0",
        "low_resolution": "instr(reason,'낮은 해상도')>0",
        "analysis_failed": "instr(reason,'분석 실패:')>0",
    }
    if issues:
        unknown = set(issues) - set(issue_sql)
        if unknown:
            raise ValueError(f"Unknown analysis issue: {sorted(unknown)[0]}")
        clauses.append("(" + " OR ".join(issue_sql[issue] for issue in issues) + ")")
    if len(clauses) == 1 and not delete_all:
        raise ValueError("At least one deletion filter is required")
    with connect() as conn:
        rows = [row_dict(row) for row in conn.execute(
            "SELECT id,path,source FROM images WHERE " + " AND ".join(clauses), args
        ).fetchall()]
    root = dataset_dir(dataset_id).resolve()
    deleted_files = 0
    for row in rows:
        path = Path(row["path"]).resolve()
        if root in path.parents and path.is_file():
            path.unlink()
            deleted_files += 1
        backup = repair_backup_path({"id": row["id"], "dataset_id": dataset_id})
        if backup:
            backup.unlink(missing_ok=True)
    if any(row["source"] == "instagram" for row in rows):
        instagram_download_archive_path(dataset_id).unlink(missing_ok=True)
    if rows:
        with connect() as conn:
            conn.executemany("DELETE FROM images WHERE id=?", [(row["id"],) for row in rows])
    return {"deleted_records": len(rows), "deleted_files": deleted_files}


def delete_image(image_id):
    image = get_image(image_id)
    if not image:
        return False
    path = Path(image["path"]).resolve()
    root = dataset_dir(image["dataset_id"]).resolve()
    if root in path.parents and path.is_file():
        path.unlink()
    with connect() as conn:
        conn.execute("DELETE FROM images WHERE id=?", (image_id,))
    return True


init_db()
