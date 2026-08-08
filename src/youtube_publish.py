from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
import re

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from telethon import TelegramClient
from telegram import Update
from telegram.ext import ContextTypes

from config import load_config
from jobs import BASE_DIR, has_media

logger = logging.getLogger(__name__)

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

YOUTUBE_CLIENT_SECRET = BASE_DIR / "config" / "youtube_client_secret.json"
YOUTUBE_TOKEN_FILE = BASE_DIR / "data" / "youtube" / "token.json"
YOUTUBE_WORKDIR = BASE_DIR / "data" / "publish"

YOUTUBE_CATEGORY_ID = "22"
YOUTUBE_PRIVACY_STATUS = "public"  # change to "unlisted" or "private" later if needed


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
        raise RuntimeError(
            "YouTube is not authorized yet. Run: python src/youtube_auth.py"
        )

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
            "title": title,
            "description": description,
            "categoryId": YOUTUBE_CATEGORY_ID,
        },
        "status": {
            "privacyStatus": YOUTUBE_PRIVACY_STATUS,
        },
    }

    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        _, response = request.next_chunk()

    return response["id"]


async def publish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg is None or update.effective_chat is None:
        return

    replied = msg.reply_to_message
    if replied is None or not has_media(replied):
        await msg.reply_text("Reply to a media message with /publish [title].")
        return

    cfg = load_config()

    # Title: command args if provided, otherwise caption first line, otherwise a fallback.
    title = " ".join(context.args).strip()
    raw_caption = getattr(replied, "caption", None)
    if raw_caption:
        cleaned = raw_caption.strip()
        match = re.match(r"^Job \d{8}-\d{6} \| Part (.+)", cleaned)
        if match:
            description = match.group(1).strip()
        else:
            description = description or "Violet #meme"
    else:
        description = "Violet #meme"

    if not title:
        if description:
            title = description.splitlines()[0].strip()
        else:
            title = "Violet #meme"

    title = title[:100].strip() or "Violet #meme"

    YOUTUBE_WORKDIR.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="publish_", dir=str(YOUTUBE_WORKDIR)))

    try:
        async with TelegramClient(str(BASE_DIR / "violet"), cfg.tg_api_id, cfg.tg_api_hash) as client:
            if not await client.is_user_authorized():
                raise RuntimeError("Telethon session is not authorized.")

            msg_obj = await client.get_messages(replied.chat_id, ids=replied.message_id)
            if msg_obj is None:
                raise RuntimeError("The replied message is no longer accessible.")
            if not has_media(msg_obj):
                raise RuntimeError("The replied message has no downloadable media.")

            ext = getattr(getattr(msg_obj, "file", None), "ext", None) or ".mp4"
            if not str(ext).startswith("."):
                ext = f".{ext}"

            source_path = work_dir / f"source{ext}"
            downloaded = await client.download_media(msg_obj, file=str(source_path))
            if not downloaded:
                raise RuntimeError("Failed to download the replied media.")

            video_path = Path(downloaded)

        video_id = await asyncio.to_thread(upload_video, video_path, title, description)

        await msg.reply_text(
            f"Uploaded to YouTube.\nVideo ID: {video_id}",
            disable_web_page_preview=True,
        )

    except Exception as exc:
        logger.exception("Publish failed")
        await msg.reply_text(f"Publish failed: {type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
