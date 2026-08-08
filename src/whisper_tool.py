from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient
from telegram import Update
from telegram.ext import ContextTypes

from config import load_config
from jobs import BASE_DIR, has_media

logger = logging.getLogger(__name__)

WHISPER_SESSION = BASE_DIR / "violet"
WHISPER_ASSET_DIR = BASE_DIR / "assets" / "whisper"
WHISPER_WORKDIR = BASE_DIR / "data" / "whisper"

WHISPER_LANGUAGE = "en"
WHISPER_COMPUTE_TYPE = "int8"
WHISPER_MODEL = "base.en"
WHISPER_DEVICE = "cpu"
WHISPER_BATCH_SIZE = 1
WHISPER_OUTPUT_FORMAT = "srt"
WHISPER_MAX_LINE_WIDTH = 15
WHISPER_MAX_LINE_COUNT = 1
WHISPER_HIGHLIGHT_WORDS = True

WHISPER_FALLBACK_WIDTH = 1080
WHISPER_FALLBACK_HEIGHT = 1920
WHISPER_FALLBACK_FPS = 30

WHISPER_FONT_SIZE = 15
WHISPER_MARGIN_V = 50
WHISPER_OUTLINE = 1
WHISPER_ALIGNMENT = 2  # bottom-center


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, text=True, capture_output=True)


def _ffmpeg_escape_path(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace(":", "\\:")


def _find_font_file() -> Path:
    if not WHISPER_ASSET_DIR.exists():
        raise RuntimeError(f"Missing asset folder: {WHISPER_ASSET_DIR}")

    fonts = sorted(p for p in WHISPER_ASSET_DIR.iterdir() if p.suffix.lower() in {".ttf", ".otf"})
    if not fonts:
        raise RuntimeError(f"No .ttf or .otf font file found in {WHISPER_ASSET_DIR}")

    return fonts[0]


def _sanitize_color(color: str | None) -> str:
    if not color:
        return "yellow"

    color = color.strip().lower()
    if re.fullmatch(r"#[0-9a-f]{6}", color):
        return color
    if re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", color):
        return color
    return "yellow"


def _ffprobe_json(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    import json

    return json.loads(result.stdout)


def _has_video_stream(path: Path) -> bool:
    probe = _ffprobe_json(path)
    return any(stream.get("codec_type") == "video" for stream in probe.get("streams", []))


def _has_audio_stream(path: Path) -> bool:
    probe = _ffprobe_json(path)
    return any(stream.get("codec_type") == "audio" for stream in probe.get("streams", []))


def _get_duration(path: Path) -> float:
    probe = _ffprobe_json(path)
    return float(probe["format"]["duration"])


def _find_srt_file(output_dir: Path) -> Path:
    candidates = sorted(output_dir.rglob("*.srt"))
    if not candidates:
        raise RuntimeError("WhisperX did not produce an SRT file.")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _colorize_srt(srt_text: str, color: str) -> str:
    return srt_text.replace("<u>", f'<font color="{color}">').replace("</u>", "</font>")


def _run_whisperx(source_path: Path, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "whisperx",
        str(source_path),

        "--language",
        WHISPER_LANGUAGE,

        "--device",
        WHISPER_DEVICE,

        "--compute_type",
        WHISPER_COMPUTE_TYPE,

        "--model",
        WHISPER_MODEL,

        "--batch_size",
        str(WHISPER_BATCH_SIZE),

        "--output_format",
        WHISPER_OUTPUT_FORMAT,

        "--highlight_words",
        str(WHISPER_HIGHLIGHT_WORDS),

        "--max_line_width",
        str(WHISPER_MAX_LINE_WIDTH),

        "--max_line_count",
        str(WHISPER_MAX_LINE_COUNT),

        "--output_dir",
        str(output_dir),
    ]

    _run(cmd)
    return _find_srt_file(output_dir)


def _make_black_base(source_path: Path, base_path: Path) -> Path:
    duration = _get_duration(source_path)

    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={WHISPER_FALLBACK_WIDTH}x{WHISPER_FALLBACK_HEIGHT}:r={WHISPER_FALLBACK_FPS}",
        "-i",
        str(source_path),
        "-t",
        str(duration),
        "-shortest",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(base_path),
    ]
    _run(cmd)
    return base_path


def _burn_subtitles(input_video: Path, srt_path: Path, output_video: Path) -> None:
    font_path = _find_font_file()
    font_dir = font_path.parent
    font_name = font_path.stem.replace("'", "")

    style = (
        f"FontName={font_name},"
        f"FontSize={WHISPER_FONT_SIZE},"
        f"Outline={WHISPER_OUTLINE},"
        f"Shadow=0,"
        f"Alignment={WHISPER_ALIGNMENT},"
        f"MarginV={WHISPER_MARGIN_V}"
    )

    srt_escaped = _ffmpeg_escape_path(srt_path)
    fontdir_escaped = _ffmpeg_escape_path(font_dir)

    if _has_audio_stream(input_video):
        map_args = ["-map", "0:v:0", "-map", "0:a?"]
        audio_args = ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
    else:
        map_args = ["-map", "0:v:0"]
        audio_args = []

    vf = (
        f"subtitles='{srt_escaped}':"
        f"fontsdir='{fontdir_escaped}':"
        f"force_style='{style}'"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_video),
        "-vf",
        vf,
        *map_args,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        *audio_args,
        "-movflags",
        "+faststart",
        str(output_video),
    ]
    _run(cmd)


def _whisper_pipeline_sync(source_path: Path, color: str, work_dir: Path) -> Path:
    transcribe_dir = work_dir / "whisperx"
    transcribe_dir.mkdir(parents=True, exist_ok=True)

    srt_path = _run_whisperx(source_path, transcribe_dir)
    raw_srt = srt_path.read_text(encoding="utf-8")
    colored_srt = work_dir / "colored.srt"
    colored_srt.write_text(_colorize_srt(raw_srt, color), encoding="utf-8")

    if _has_video_stream(source_path):
        base_video = source_path
    else:
        base_video = _make_black_base(source_path, work_dir / "black_base.mp4")

    final_video = work_dir / "final.mp4"
    _burn_subtitles(base_video, colored_srt, final_video)
    return final_video


async def _download_replied_media(chat_id: int, message_id: int, dest: Path, cfg) -> Path:
    async with TelegramClient(str(WHISPER_SESSION), cfg.tg_api_id, cfg.tg_api_hash) as client:
        if not await client.is_user_authorized():
            raise RuntimeError("Telethon session is not authorized.")

        msg = await client.get_messages(chat_id, ids=message_id)
        if msg is None:
            raise RuntimeError("The replied message is no longer accessible.")
        if not has_media(msg):
            raise RuntimeError("The replied message has no downloadable media.")

        downloaded = await client.download_media(msg, file=str(dest))
        if not downloaded:
            raise RuntimeError("Failed to download the replied media.")

        return Path(downloaded)


async def process_whisper_job(
    *,
    cfg,
    bot,
    command_chat_id: int,
    review_chat_id: int,
    source_chat_id: int,
    source_message_id: int,
    color: str,
) -> None:
    job_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    work_dir = WHISPER_WORKDIR / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        source_ext = ".bin"
        source_path = work_dir / f"source{source_ext}"

        # Download with Telethon first.
        async with TelegramClient(str(WHISPER_SESSION), cfg.tg_api_id, cfg.tg_api_hash) as client:
            if not await client.is_user_authorized():
                raise RuntimeError("Telethon session is not authorized.")

            msg = await client.get_messages(source_chat_id, ids=source_message_id)
            if msg is None:
                raise RuntimeError("The replied message is no longer accessible.")
            if not has_media(msg):
                raise RuntimeError("The replied message has no media.")

            # Try to preserve the source extension if Telethon knows it.
            ext = getattr(getattr(msg, "file", None), "ext", None) or ".bin"
            if not str(ext).startswith("."):
                ext = f".{ext}"
            source_path = work_dir / f"source{ext}"

            downloaded = await client.download_media(msg, file=str(source_path))
            if not downloaded:
                raise RuntimeError("Failed to download the replied media.")
            source_path = Path(downloaded)

        final_video = await asyncio.to_thread(_whisper_pipeline_sync, source_path, color, work_dir)

        if review_chat_id is None:
            review_chat_id = command_chat_id

        try:
            with final_video.open("rb") as fh:
                await bot.send_video(
                    chat_id=review_chat_id,
                    video=fh,
                    supports_streaming=True,
                    caption=f"Whisper subtitle pass ({color})",
                )
        except Exception:
            with final_video.open("rb") as fh:
                await bot.send_document(
                    chat_id=review_chat_id,
                    document=fh,
                    caption=f"Whisper subtitle pass ({color})",
                )

        await bot.send_message(
            chat_id=command_chat_id,
            text="Whisper pass finished and sent to the review chat.",
        )

    except Exception as exc:
        logger.exception("Whisper job failed")
        await bot.send_message(
            chat_id=command_chat_id,
            text=f"Whisper failed: {type(exc).__name__}: {exc}",
        )
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


async def whisper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg is None or update.effective_chat is None:
        return

    replied = msg.reply_to_message
    if replied is None or not has_media(replied):
        await msg.reply_text("Reply to a media message with /whisper [color].")
        return

    cfg = load_config()

    color = _sanitize_color(context.args[0] if context.args else "yellow")
    review_chat_id = cfg.template.telegram.review_chat or update.effective_chat.id

    await msg.reply_text(f"Whisper job started with color: {color}")

    context.application.create_task(
        process_whisper_job(
            cfg=cfg,
            bot=context.bot,
            command_chat_id=update.effective_chat.id,
            review_chat_id=review_chat_id,
            source_chat_id=replied.chat_id,
            source_message_id=replied.message_id,
            color=color,
        )
    )
