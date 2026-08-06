from __future__ import annotations

from pathlib import Path
import logging
import shutil

from telethon import TelegramClient

from jobs import (
    BASE_DIR,
    clear_active_job_id,
    guess_media_ext,
    has_media,
    job_folder,
    load_job,
    now_iso,
    save_job,
)
from renderers import render_job


logger = logging.getLogger(__name__)
TELETHON_SESSION = BASE_DIR / "violet"


async def validate_queue_accessible(cfg, queue: list[dict]) -> list[str]:
    errors: list[str] = []

    async with TelegramClient(str(TELETHON_SESSION), cfg.tg_api_id, cfg.tg_api_hash) as client:
        if not await client.is_user_authorized():
            raise RuntimeError("Telethon session is not authorized.")

        for idx, item in enumerate(queue, start=1):
            try:
                chat_id = int(item["source_chat_id"])
                message_id = int(item["source_message_id"])
                msg = await client.get_messages(chat_id, ids=message_id)

                if msg is None:
                    errors.append(f"#{idx}: message no longer accessible")
                elif not has_media(msg):
                    errors.append(f"#{idx}: message exists but has no media")
            except Exception as exc:
                errors.append(f"#{idx}: {type(exc).__name__}: {exc}")

    return errors


def _cleanup_intermediate_files(template_name: str, job_id: str) -> None:
    folder = job_folder(template_name, job_id)
    shutil.rmtree(folder / "downloads", ignore_errors=True)
    shutil.rmtree(folder / "rendered" / "segments", ignore_errors=True)


async def _upload_review_copy(bot, review_chat_id: int, final_path: Path, job_id: str, part_text: str) -> tuple[int, str]:
    caption = f"Job {job_id} | Part {part_text}"

    try:
        with final_path.open("rb") as fh:
            message = await bot.send_video(
                chat_id=review_chat_id,
                video=fh,
                caption=caption,
                supports_streaming=True,
            )
        return message.id, "video"
    except Exception:
        with final_path.open("rb") as fh:
            message = await bot.send_document(
                chat_id=review_chat_id,
                document=fh,
                caption=caption,
            )
        return message.id, "document"


async def process_job(cfg, job_id: str, bot=None, notify_chat_id: int | None = None) -> None:
    template_name = cfg.active_template
    job = load_job(template_name, job_id)
    if job is None:
        raise RuntimeError(f"Job {job_id} not found")

    try:
        job["status"] = "downloading"
        job["started_at"] = now_iso()
        save_job(template_name, job)

        downloads_dir = job_folder(template_name, job_id) / "downloads"
        downloads_dir.mkdir(parents=True, exist_ok=True)

        async with TelegramClient(str(TELETHON_SESSION), cfg.tg_api_id, cfg.tg_api_hash) as client:
            if not await client.is_user_authorized():
                raise RuntimeError("Telethon session is not authorized.")

            items = job.get("items", [])
            for index, item in enumerate(items, start=1):
                chat_id = int(item["source_chat_id"])
                message_id = int(item["source_message_id"])

                item["status"] = "fetching"
                item["updated_at"] = now_iso()
                save_job(template_name, job)

                msg = await client.get_messages(chat_id, ids=message_id)
                if msg is None:
                    raise RuntimeError(f"Message {message_id} in chat {chat_id} is no longer accessible")
                if not has_media(msg):
                    raise RuntimeError(f"Message {message_id} in chat {chat_id} no longer has media")

                ext = guess_media_ext(msg)
                target_path = downloads_dir / f"{index:03d}{ext}"

                item["status"] = "downloading"
                item["updated_at"] = now_iso()
                save_job(template_name, job)

                downloaded = await client.download_media(msg, file=str(target_path))
                if not downloaded:
                    raise RuntimeError(f"Failed to download item #{index}")

                downloaded_path = Path(downloaded)
                item["status"] = "downloaded"
                item["local_path"] = str(downloaded_path)
                try:
                    item["file_size"] = downloaded_path.stat().st_size
                except OSError:
                    item["file_size"] = None

                item["updated_at"] = now_iso()
                save_job(template_name, job)

        job["status"] = "rendering"
        save_job(template_name, job)

        final_path = render_job(cfg, job_id)

        _cleanup_intermediate_files(template_name, job_id)

        job = load_job(template_name, job_id) or job
        job["rendered_path"] = str(final_path)
        save_job(template_name, job)

        review_chat_id = cfg.template.telegram.review_chat
        part_text = str(job.get("part_text") or "1")

        if bot and review_chat_id is not None:
            job["status"] = "uploading_review"
            save_job(template_name, job)

            try:
                review_message_id, upload_kind = await _upload_review_copy(
                    bot,
                    int(review_chat_id),
                    final_path,
                    job_id,
                    part_text,
                )
                job["status"] = "awaiting_review"
                job["review_chat_id"] = int(review_chat_id)
                job["review_message_id"] = review_message_id
                job["review_upload_kind"] = upload_kind
                job["review_uploaded_at"] = now_iso()
                save_job(template_name, job)
            except Exception as exc:
                job["status"] = "review_failed"
                job["review_error"] = f"{type(exc).__name__}: {exc}"
                save_job(template_name, job)
                raise
        else:
            job["status"] = "rendered"
            job["finished_at"] = now_iso()
            save_job(template_name, job)

        clear_active_job_id(template_name)

        if bot and notify_chat_id is not None:
            await bot.send_message(
                chat_id=notify_chat_id,
                text=f"Job {job_id} finished. Review copy uploaded.",
            )

    except Exception as exc:
        logger.exception("Job %s failed", job_id)

        job = load_job(template_name, job_id) or job
        job["status"] = "failed"
        job["finished_at"] = now_iso()
        job["error"] = f"{type(exc).__name__}: {exc}"
        save_job(template_name, job)

        clear_active_job_id(template_name)

        if bot and notify_chat_id is not None:
            await bot.send_message(
                chat_id=notify_chat_id,
                text=f"Job {job_id} failed: {type(exc).__name__}: {exc}",
            )
