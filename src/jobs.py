from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from pathlib import Path
import json
import shutil

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
QUEUES_DIR = DATA_DIR / "queues"
JOBS_DIR = DATA_DIR / "jobs"
RUNTIME_JOBS_DIR = JOBS_DIR / "runtime"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def queue_file(template_name: str) -> Path:
    return QUEUES_DIR / f"{template_name}.json"


def jobs_root(template_name: str) -> Path:
    return JOBS_DIR / template_name


def active_pointer_file(template_name: str) -> Path:
    return jobs_root(template_name) / "active.json"


def runtime_active_pointer_file() -> Path:
    return RUNTIME_JOBS_DIR / "active.json"


def job_folder(template_name: str, job_id: str) -> Path:
    return jobs_root(template_name) / job_id


def runtime_job_folder(job_id: str) -> Path:
    return RUNTIME_JOBS_DIR / job_id


def job_manifest_path(template_name: str, job_id: str) -> Path:
    return job_folder(template_name, job_id) / "job.json"


def runtime_job_manifest_path(job_id: str) -> Path:
    return runtime_job_folder(job_id) / "job.json"


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_queue(template_name: str) -> list[dict]:
    data = load_json(queue_file(template_name), [])
    return data if isinstance(data, list) else []


def save_queue(template_name: str, queue: list[dict]) -> None:
    save_json(queue_file(template_name), queue)


def clear_queue(template_name: str) -> None:
    save_queue(template_name, [])


def load_active_job_id(template_name: str) -> str | None:
    data = load_json(active_pointer_file(template_name), None)
    if isinstance(data, dict):
        job_id = data.get("job_id")
        if job_id:
            return str(job_id)

    # Runtime jobs (Whisper / YouTube publish) use the same status surface.
    runtime = load_json(runtime_active_pointer_file(), None)
    if isinstance(runtime, dict):
        job_id = runtime.get("job_id")
        if job_id:
            return str(job_id)
    return None


def set_active_job_id(template_name: str, job_id: str) -> None:
    save_json(active_pointer_file(template_name), {"job_id": job_id, "set_at": now_iso()})


def clear_active_job_id(template_name: str) -> None:
    p = active_pointer_file(template_name)
    if p.exists():
        p.unlink()


def create_runtime_job(job_id: str, kind: str, **fields) -> dict:
    job = {
        "job_id": job_id,
        "template_name": "runtime",
        "kind": kind,
        "status": "queued",
        "created_at": now_iso(),
        **fields,
    }
    save_runtime_job(job)
    save_json(runtime_active_pointer_file(), {"job_id": job_id, "set_at": now_iso()})
    return job


def load_runtime_job(job_id: str) -> dict | None:
    data = load_json(runtime_job_manifest_path(job_id), None)
    return data if isinstance(data, dict) else None


def save_runtime_job(job: dict) -> None:
    save_json(runtime_job_manifest_path(str(job["job_id"])), job)


def clear_runtime_job(job_id: str) -> None:
    pointer = runtime_active_pointer_file()
    active = load_json(pointer, None)
    if isinstance(active, dict) and str(active.get("job_id")) == str(job_id):
        pointer.unlink(missing_ok=True)
    shutil.rmtree(runtime_job_folder(str(job_id)), ignore_errors=True)


def load_job(template_name: str, job_id: str) -> dict | None:
    data = load_json(job_manifest_path(template_name, job_id), None)
    if isinstance(data, dict):
        return data
    return load_runtime_job(job_id)


def save_job(template_name: str, job: dict) -> None:
    job_id = str(job["job_id"])
    save_json(job_manifest_path(template_name, job_id), job)


def create_job(
    template_name: str,
    job_id: str,
    created_by: int | None,
    queue_snapshot: list[dict],
    make_text: str,
) -> dict:
    folder = job_folder(template_name, job_id)
    (folder / "downloads").mkdir(parents=True, exist_ok=True)
    (folder / "rendered" / "segments").mkdir(parents=True, exist_ok=True)

    job = {
        "template_name": template_name,
        "job_id": job_id,
        "status": "queued",
        "created_at": now_iso(),
        "created_by": created_by,
        "started_at": None,
        "finished_at": None,
        "error": None,
        "make_text": make_text,
        "rendered_path": None,
        "items": queue_snapshot,
    }
    save_job(template_name, job)
    return job


def cleanup_job(template_name: str, job_id: str) -> None:
    shutil.rmtree(job_folder(template_name, job_id), ignore_errors=True)


def has_media(msg) -> bool:
    return bool(
        msg.video
        or msg.document
        or msg.photo
        or msg.audio
        or msg.animation
        or msg.voice
    )


def guess_media_ext(msg) -> str:
    file_obj = getattr(msg, "file", None)
    ext = getattr(file_obj, "ext", None) if file_obj else None
    if ext:
        return ext if str(ext).startswith(".") else f".{ext}"
    return ".bin"


def source_link(chat_id: int, message_id: int) -> str | None:
    chat_id_str = str(chat_id)
    if chat_id_str.startswith("-100"):
        return f"https://t.me/c/{chat_id_str[4:]}/{message_id}"
    return None


def item_label(item: dict) -> str:
    media_type = str(item.get("media_type", "media")).title()
    source_msg = item.get("source_message_id", "?")
    status = item.get("status", "queued")
    return f"{escape(media_type)} — msg {escape(str(source_msg))} — {escape(status)}"
