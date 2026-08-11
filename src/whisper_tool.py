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
from jobs import BASE_DIR, clear_runtime_job, create_runtime_job, has_media, load_runtime_job, save_runtime_job

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
WHISPER_ALIGNMENT = 2


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
    result = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], check=True, text=True, capture_output=True)
    import json
    return json.loads(result.stdout)


def _has_video_stream(path: Path) -> bool:
    return any(stream.get("codec_type") == "video" for stream in _ffprobe_json(path).get("streams", []))


def _has_audio_stream(path: Path) -> bool:
    return any(stream.get("codec_type") == "audio" for stream in _ffprobe_json(path).get("streams", []))


def _get_duration(path: Path) -> float:
    return float(_ffprobe_json(path)["format"]["duration"])


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
        "whisperx", str(source_path),
        "--language", WHISPER_LANGUAGE,
        "--device", WHISPER_DEVICE,
        "--compute_type", WHISPER_COMPUTE_TYPE,
        "--model", WHISPER_MODEL,
        "--batch_size", str(WHISPER_BATCH_SIZE),
        "--output_format", WHISPER_OUTPUT_FORMAT,
        "--highlight_words", str(WHISPER_HIGHLIGHT_WORDS),
        "--max_line_width", str(WHISPER_MAX_LINE_WIDTH),
        "--max_line_count", str(WHISPER_MAX_LINE_COUNT),
        "--output_dir", str(output_dir),
    ]
    _run(cmd)
    return _find_srt_file(output_dir)


def _make_black_base(source_path: Path, base_path: Path) -> Path:
    duration = _get_duration(source_path)
    _run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"color=c=black:s={WHISPER_FALLBACK_WIDTH}x{WHISPER_FALLBACK_HEIGHT}:r={WHISPER_FALLBACK_FPS}",
        "-i", str(source_path), "-t", str(duration), "-shortest",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(base_path),
    ])
    return base_path


def _burn_subtitles(input_video: Path, srt_path: Path, output_video: Path) -> None:
    font_path = _find_font_file()
    style = f"FontName={font_path.stem.replace(chr(39), '')},FontSize={WHISPER_FONT_SIZE},Outline={WHISPER_OUTLINE},Shadow=0,Alignment={WHISPER_ALIGNMENT},MarginV={WHISPER_MARGIN_V}"
    vf = f"subtitles='{_ffmpeg_escape_path(srt_path)}':fontsdir='{_ffmpeg_escape_path(font_path.parent)}':force_style='{style}'"
    if _has_audio_stream(input_video):
        maps = ["-map", "0:v:0", "-map", "0:a?"]
        audio = ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
    else:
        maps, audio = ["-map", "0:v:0"], []
    _run(["ffmpeg", "-y", "-i", str(input_video), "-vf", vf, *maps, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", *audio, "-movflags", "+faststart", str(output_video)])


def _whisper_pipeline_sync(source_path: Path, color: str, work_dir: Path) -> Path:
    transcribe_dir = work_dir / "whisperx"
    srt_path = _run_whisperx(source_path, transcribe_dir)
    colored_srt = work_dir / "colored.srt"
    colored_srt.write_text(_colorize_srt(srt_path.read_text(encoding="utf-8"), color), encoding="utf-8")
    base_video = source_path if _has_video_stream(source_path) else _make_black_base(source_path, work_dir / "black_base.mp4")
    final_video = work_dir / "final.mp4"
    _burn_subtitles(base_video, colored_srt, final_video)
    return final_video


async def process_whisper_job(*, cfg, bot, command_chat_id: int, review_chat_id: int, source_chat_id: int, source_message_id: int, color: str, job_id: str) -> None:
    work_dir = WHISPER_WORKDIR / job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    job = load_runtime_job(job_id)
    if job is None:
        raise RuntimeError(f"Whisper job {job_id} not found")
    try:
        job["status"] = "downloading"
        save_runtime_job(job)
        async with TelegramClient(str(WHISPER_SESSION), cfg.tg_api_id, cfg.tg_api_hash) as client:
            if not await client.is_user_authorized():
                raise RuntimeError("Telethon session is not authorized.")
            msg = await client.get_messages(source_chat_id, ids=source_message_id)
            if msg is None or not has_media(msg):
                raise RuntimeError("The replied message is no longer accessible.")
            ext = getattr(getattr(msg, "file", None), "ext", None) or ".bin"
            if not str(ext).startswith("."):
                ext = f".{ext}"
            source_path = work_dir / f"source{ext}"
            downloaded = await client.download_media(msg, file=str(source_path))
            if not downloaded:
                raise RuntimeError("Failed to download the replied media.")
            source_path = Path(downloaded)

        job["status"] = "transcribing"
        save_runtime_job(job)
        final_video = await asyncio.to_thread(_whisper_pipeline_sync, source_path, color, work_dir)
        job["status"] = "uploading_review"
        save_runtime_job(job)
        target_chat = review_chat_id or command_chat_id
        try:
            with final_video.open("rb") as fh:
                await bot.send_video(chat_id=target_chat, video=fh, supports_streaming=True, caption=f"Whisper subtitle pass ({color})")
        except Exception:
            with final_video.open("rb") as fh:
                await bot.send_document(chat_id=target_chat, document=fh, caption=f"Whisper subtitle pass ({color})")
        clear_runtime_job(job_id)
        await bot.send_message(chat_id=command_chat_id, text="Whisper pass finished and sent to the review chat.")
    except Exception as exc:
        logger.exception("Whisper job failed")
        job["status"] = "failed"
        job["error"] = f"{type(exc).__name__}: {exc}"
        save_runtime_job(job)
        await bot.send_message(chat_id=command_chat_id, text=f"Whisper failed: {type(exc).__name__}: {exc}")
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
    job_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    create_runtime_job(
        job_id,
        "whisper",
        status="queued",
        command_chat_id=update.effective_chat.id,
        source_chat_id=replied.chat_id,
        source_message_id=replied.message_id,
        color=color,
    )
    await msg.reply_text(f"Whisper job <code>{job_id}</code> started with color: {color}", parse_mode="HTML")
    context.application.create_task(process_whisper_job(
        cfg=cfg, bot=context.bot, command_chat_id=update.effective_chat.id,
        review_chat_id=review_chat_id, source_chat_id=replied.chat_id,
        source_message_id=replied.message_id, color=color, job_id=job_id,
    ), update=update)
