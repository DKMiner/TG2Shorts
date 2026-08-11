from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import shutil
import tempfile
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telethon import TelegramClient

from config import load_config
from jobs import BASE_DIR, clear_runtime_job, create_runtime_job, has_media, load_runtime_job, save_runtime_job

logger = logging.getLogger(__name__)
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
YOUTUBE_CLIENT_SECRET = BASE_DIR / "config" / "youtube_client_secret.json"
YOUTUBE_TOKEN_FILE = BASE_DIR / "data" / "youtube" / "token.json"
YOUTUBE_WORKDIR = BASE_DIR / "data" / "publish"
YOUTUBE_CATEGORY_ID = "22"
YOUTUBE_PRIVACY_STATUS = "public"


def _load_credentials(interactive: bool = False) -> Credentials:
    if not YOUTUBE_CLIENT_SECRET.exists():
        raise RuntimeError(f"Missing YouTube OAuth client secret: {YOUTUBE_CLIENT_SECRET}")
    YOUTUBE_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    creds: Credentials | None = None
    if YOUTUBE_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(YOUTUBE_TOKEN_FILE), YOUTUBE_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        YOUTUBE_TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        return creds
    if creds and creds.valid:
        return creds
    if not interactive:
        raise RuntimeError("YouTube is not authorized yet. Run: python src/youtube_auth.py")
    flow = InstalledAppFlow.from_client_secrets_file(str(YOUTUBE_CLIENT_SECRET), YOUTUBE_SCOPES)
    creds = flow.run_local_server(host="127.0.0.1", port=0, open_browser=False)
    YOUTUBE_TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds


def authorize_interactively() -> None:
    _load_credentials(interactive=True)
    print(f"Saved token to {YOUTUBE_TOKEN_FILE}")


def upload_video(video_path: Path, title: str, description: str) -> str:
    creds = _load_credentials(interactive=False)
    youtube = build("youtube", "v3", credentials=creds)
    media = MediaFileUpload(str(video_path), chunksize=8 * 1024 * 1024, resumable=True)
    body = {
        "snippet": {
            "title": title[:100].strip() or "Violet #meme",
            "description": description,
            "categoryId": YOUTUBE_CATEGORY_ID,
        },
        "status": {"privacyStatus": YOUTUBE_PRIVACY_STATUS},
    }
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk()
    return response["id"]


def _keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Edit title", callback_data=f"publish:edit_title:{job_id}"),
            InlineKeyboardButton("✏️ Edit description", callback_data=f"publish:edit_description:{job_id}"),
        ],
        [
            InlineKeyboardButton("✅ Publish", callback_data=f"publish:confirm:{job_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"publish:cancel:{job_id}"),
        ],
    ])


def _preview_text(job: dict) -> str:
    return (
        "<b>YouTube publish preview</b>\n\n"
        f"<b>Title:</b>\n<code>{job['title']}</code>\n\n"
        f"<b>Description:</b>\n<code>{job['description']}</code>\n\n"
        "Nothing has been uploaded yet."
    )


def _ensure_handlers(application) -> None:
    if application.bot_data.get("publish_handlers_installed"):
        return
    application.add_handler(
        CallbackQueryHandler(publish_callback, pattern=r"^publish:(edit_title|edit_description|confirm|cancel):"),
        group=1,
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, publish_edit_reply),
        group=1,
    )
    application.bot_data["publish_handlers_installed"] = True


async def _download_source(job: dict, work_dir: Path, cfg) -> Path:
    async with TelegramClient(str(BASE_DIR / "violet"), cfg.tg_api_id, cfg.tg_api_hash) as client:
        if not await client.is_user_authorized():
            raise RuntimeError("Telethon session is not authorized.")
        msg_obj = await client.get_messages(int(job["source_chat_id"]), ids=int(job["source_message_id"]))
        if msg_obj is None or not has_media(msg_obj):
            raise RuntimeError("The replied media is no longer accessible.")
        ext = getattr(getattr(msg_obj, "file", None), "ext", None) or ".mp4"
        if not str(ext).startswith("."):
            ext = f".{ext}"
        source_path = work_dir / f"source{ext}"
        downloaded = await client.download_media(msg_obj, file=str(source_path))
        if not downloaded:
            raise RuntimeError("Failed to download the replied media.")
        return Path(downloaded)


async def _do_publish(job: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = load_config()
    work_dir = Path(tempfile.mkdtemp(prefix="publish_", dir=str(YOUTUBE_WORKDIR)))
    try:
        job["status"] = "downloading"
        save_runtime_job(job)
        source_path = await _download_source(job, work_dir, cfg)
        job["status"] = "publishing_to_youtube"
        save_runtime_job(job)
        video_id = await asyncio.to_thread(upload_video, source_path, job["title"], job["description"])
        clear_runtime_job(job["job_id"])
        await context.bot.send_message(
            chat_id=int(job["command_chat_id"]),
            text=f"Published to YouTube successfully.\nVideo ID: <code>{video_id}</code>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as exc:
        job["status"] = "publish_failed"
        job["error"] = f"{type(exc).__name__}: {exc}"
        save_runtime_job(job)
        await context.bot.send_message(chat_id=int(job["command_chat_id"]), text=f"Publish failed: {type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


async def publish_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    data = query.data or ""
    parts = data.split(":", 2)
    if len(parts) != 3:
        return
    action, job_id = parts[1], parts[2]
    job = load_runtime_job(job_id)
    if not job or job.get("kind") != "youtube_publish":
        await query.edit_message_text("This publish request has expired.")
        return
    if action == "cancel":
        job["status"] = "cancelled"
        save_runtime_job(job)
        clear_runtime_job(job_id)
        await query.edit_message_text("Publish cancelled. Nothing was uploaded.")
        return
    if action in {"edit_title", "edit_description"}:
        job["edit_mode"] = action.removeprefix("edit_")
        save_runtime_job(job)
        field = job["edit_mode"]
        prompt = await context.bot.send_message(
            chat_id=int(job["command_chat_id"]),
            text=f"Send the new {field} as a reply to this message.",
            reply_markup=ForceReply(selective=True),
        )
        job["edit_prompt_message_id"] = prompt.message_id
        save_runtime_job(job)
        return
    if action == "confirm":
        if job.get("status") != "awaiting_confirmation":
            return
        job["status"] = "publishing_to_youtube"
        save_runtime_job(job)
        await query.edit_message_text("<b>Publishing to YouTube now...</b>", parse_mode="HTML")
        context.application.create_task(_do_publish(job, context), update=update)


async def publish_edit_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg is None or update.effective_chat is None:
        return
    pointer = BASE_DIR / "data" / "jobs" / "runtime" / "active.json"
    if not pointer.exists():
        return
    try:
        import json
        job_id = str(json.loads(pointer.read_text(encoding="utf-8"))["job_id"])
    except Exception:
        return
    job = load_runtime_job(job_id)
    if not job or job.get("kind") != "youtube_publish":
        return
    if int(job.get("command_chat_id", 0)) != int(update.effective_chat.id) or not job.get("edit_mode"):
        return
    prompt_id = job.get("edit_prompt_message_id")
    if prompt_id and (msg.reply_to_message is None or msg.reply_to_message.message_id != prompt_id):
        return
    value = (msg.text or "").strip()
    field = job.pop("edit_mode")
    if field == "title":
        job["title"] = value[:100].strip() or "Violet #meme"
    else:
        job["description"] = value
    job.pop("edit_prompt_message_id", None)
    job["status"] = "awaiting_confirmation"
    save_runtime_job(job)
    await msg.reply_text(_preview_text(job), parse_mode="HTML", reply_markup=_keyboard(job_id), disable_web_page_preview=True)


async def publish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg is None or update.effective_chat is None:
        return
    replied = msg.reply_to_message
    if replied is None or not has_media(replied):
        await msg.reply_text("Reply to a media message with /publish [title].")
        return

    _ensure_handlers(context.application)
    raw_caption = (getattr(replied, "caption", None) or "").strip()
    description = raw_caption or "Violet #meme"
    title = " ".join(context.args).strip()
    if not title:
        title = raw_caption.splitlines()[0].strip() if raw_caption else "Violet #meme"
    title = title[:100].strip() or "Violet #meme"

    job_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    create_runtime_job(
        job_id,
        "youtube_publish",
        status="awaiting_confirmation",
        command_chat_id=update.effective_chat.id,
        source_chat_id=replied.chat_id,
        source_message_id=replied.message_id,
        title=title,
        description=description,
    )
    job = load_runtime_job(job_id)
    assert job is not None
    await msg.reply_text(_preview_text(job), parse_mode="HTML", reply_markup=_keyboard(job_id), disable_web_page_preview=True)
