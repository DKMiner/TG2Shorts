from __future__ import annotations

from pathlib import Path
import json
import random
import shutil
import subprocess
import textwrap

from jobs import BASE_DIR, job_folder, load_job, now_iso, save_job


RANKING_ASSET_DIR = BASE_DIR / "assets" / "ranking"

TITLE_FONT_SIZE = 90
TITLE_MIN_FONT_SIZE = 34
TITLE_TOP_Y = 40
TITLE_LINE_SPACING = 5

NUMBER_FONT_SIZE = 74
NUMBER_LEFT_X = 60
NUMBER_START_Y = 300
NUMBER_GAP = 120

CAPTION_FONT_SIZE = 74
CAPTION_LEFT_X = 150
CAPTION_OFFSET_Y = 5

NUMBER_BORDER = 5
TITLE_BORDER = 5

NUMBER_COLORS = [
    "white",
    "yellow",
    "cyan",
    "magenta",
    "lime",
    "orange",
    "red",
]


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, text=True, capture_output=True)


def _ffprobe_json(path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = subprocess.run(cmd, check=True, text=True, capture_output=True)
    return json.loads(result.stdout)


def _has_audio(path: Path) -> bool:
    probe = _ffprobe_json(path)
    return any(stream.get("codec_type") == "audio" for stream in probe.get("streams", []))


def _get_duration(path: Path) -> float:
    probe = _ffprobe_json(path)
    return float(probe["format"]["duration"])


def _escape_drawtext(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
        .replace(",", "\\,")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("\n", "\\n")
    )


def _escape_path(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace(":", "\\:")


def _find_font_file() -> Path:
    if not RANKING_ASSET_DIR.exists():
        raise RuntimeError(f"Missing asset folder: {RANKING_ASSET_DIR}")

    fonts = sorted(
        [p for p in RANKING_ASSET_DIR.iterdir() if p.suffix.lower() in {".ttf", ".otf"}]
    )
    if not fonts:
        raise RuntimeError(f"No .ttf or .otf font file found in {RANKING_ASSET_DIR}")

    return fonts[0]


def _wrap_title_to_two_lines(title: str) -> tuple[str, str]:
    words = [w for w in title.split() if w]
    if not words:
        return "Ranking", "funny viral videos"
    if len(words) == 1:
        return words[0], ""

    best: tuple[int, str, str] | None = None

    for split in range(1, len(words)):
        line1 = " ".join(words[:split])
        line2 = " ".join(words[split:])
        score = abs(len(line1) - len(line2))
        candidate = (score, line1, line2)
        if best is None or candidate[0] < best[0]:
            best = candidate

    assert best is not None
    return best[1], best[2]


def _pick_title_font_size(title1: str, title2: str, width: int) -> int:
    longest_line = max(len(title1), len(title2), 1)
    if longest_line <= 14:
        return TITLE_FONT_SIZE

    shrink = (longest_line - 14) * 2
    return max(TITLE_MIN_FONT_SIZE, TITLE_FONT_SIZE - shrink)


def _pick_number_font_size(width: int) -> int:
    return max(44, min(84, width // 13))


def _build_base_filter(cfg) -> str:
    width = cfg.template.video.width
    height = cfg.template.video.height
    fps = cfg.template.video.fps
    blur = cfg.template.render.blur_background

    if blur:
        return (
            f"[0:v]split=2[fgsrc][bgsrc];"
            f"[bgsrc]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=20:1[bg];"
            f"[fgsrc]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1,format=yuv420p,setsar=1,fps={fps}[vbase]"
        )

    return (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"format=yuv420p,setsar=1,fps={fps}[vbase]"
    )


def _normalize_segment(source: Path, dest: Path, *, cfg) -> None:
    if not source.exists():
        raise RuntimeError(f"Source clip missing: {source}")

    source_has_audio = _has_audio(source)
    filter_complex = _build_base_filter(cfg)

    cmd = ["ffmpeg", "-y", "-i", str(source)]

    if not source_has_audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]

    cmd += ["-filter_complex", filter_complex, "-map", "[vbase]"]

    if source_has_audio:
        cmd += [
            "-map",
            "0:a:0?",
            "-af",
            "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo",
        ]
    else:
        cmd += ["-map", "1:a"]

    cmd += [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-shortest",
        "-movflags",
        "+faststart",
        str(dest),
    ]

    _run(cmd)


def _build_overlay_filter(
    cfg,
    *,
    title1_file: Path,
    title2_file: Path | None,
    title_start: float,
    title_end: float,
    clip_starts: list[float],
    assigned_numbers: list[int],
    caption_files: list[Path | None],
) -> str:
    font_path = _find_font_file()
    fontfile = _escape_path(font_path)
    title1_escaped = _escape_path(title1_file)
    title2_escaped = _escape_path(title2_file) if title2_file else ""

    width = cfg.template.video.width
    height = cfg.template.video.height

    title1_text = title1_file.read_text(encoding="utf-8")
    title2_text = title2_file.read_text(encoding="utf-8") if title2_file else ""
    title_size = _pick_title_font_size(title1_text, title2_text, width)
    number_size = _pick_number_font_size(width)

    title1_y = TITLE_TOP_Y
    title2_y = TITLE_TOP_Y + title_size + TITLE_LINE_SPACING

    left_x = NUMBER_LEFT_X
    number_start_y = NUMBER_START_Y
    number_gap = NUMBER_GAP

    number_palette = NUMBER_COLORS

    parts: list[str] = []

    parts.append(
        f"[0:v]drawtext=fontfile='{fontfile}':textfile='{title1_escaped}':reload=0:"
        f"fontcolor=white:fontsize={title_size}:"
        f"borderw={TITLE_BORDER}:bordercolor=black:"
        f"x=(w-text_w)/2:y={title1_y}:"
        f"enable='between(t,{title_start:.3f},{title_end:.3f})'[v0]"
    )

    current = "v0"

    if title2_file and title2_text.strip():
        parts.append(
            f"[{current}]drawtext=fontfile='{fontfile}':textfile='{title2_escaped}':reload=0:"
            f"fontcolor=white:fontsize={title_size}:"
            f"borderw={TITLE_BORDER}:bordercolor=black:"
            f"x=(w-text_w)/2:y={title2_y}:"
            f"enable='between(t,{title_start:.3f},{title_end:.3f})'[v1]"
        )
        current = "v1"

    for i in range(1, len(assigned_numbers) + 1):
        color = number_palette[(i - 1) % len(number_palette)]
        next_label = f"v_num_{i}"
        y = number_start_y + (i - 1) * number_gap

        parts.append(
            f"[{current}]drawtext=fontfile='{fontfile}':text='{i}.':"
            f"fontcolor={color}:fontsize={number_size}:"
            f"borderw={NUMBER_BORDER}:bordercolor=black:"
            f"x={left_x}:y={y}:"
            f"enable='between(t,{title_start:.3f},{title_end:.3f})'[{next_label}]"
        )
        current = next_label

    for idx, (start_time, number, caption_file) in enumerate(
        zip(clip_starts, assigned_numbers, caption_files),
        start=1,
    ):
        if caption_file is None:
            continue

        color = number_palette[(number - 1) % len(number_palette)]
        next_label = f"v_cap_{idx}"
        y = number_start_y + (number - 1) * number_gap + CAPTION_OFFSET_Y
        caption_escaped = _escape_path(caption_file)

        parts.append(
            f"[{current}]drawtext=fontfile='{fontfile}':textfile='{caption_escaped}':reload=0:"
            f"fontcolor={color}:fontsize={CAPTION_FONT_SIZE}:"
            f"borderw={NUMBER_BORDER}:bordercolor=black:"
            f"x={CAPTION_LEFT_X}:y={y}:"
            f"enable='between(t,{start_time:.3f},{title_end:.3f})'[{next_label}]"
        )
        current = next_label

    parts.append(f"[{current}]null[v]")
    return ";".join(parts)


def _make_assigned_numbers(count: int, seed_text: str) -> list[int]:
    if count <= 1:
        return [1]

    rng = random.Random(seed_text)
    nums = list(range(2, count + 1))
    rng.shuffle(nums)
    nums.append(1)
    return nums


def _short_caption(text: str, limit: int = 28) -> str:
    text = " ".join((text or "").split())
    if not text:
        return ""
    return textwrap.shorten(text, width=limit, placeholder="…")


def render_job(cfg, job_id: str) -> Path:
    template_name = cfg.active_template
    job = load_job(template_name, job_id)
    if job is None:
        raise RuntimeError(f"Job {job_id} not found")

    rendered_dir = job_folder(template_name, job_id) / "rendered"
    segments_dir = rendered_dir / "segments"
    rendered_dir.mkdir(parents=True, exist_ok=True)
    segments_dir.mkdir(parents=True, exist_ok=True)

    job["status"] = "rendering"
    job["render_started_at"] = now_iso()
    save_job(template_name, job)

    make_text = str(job.get("make_text") or "Ranking funny viral videos")
    items = job.get("items", [])
    if not items:
        raise RuntimeError("Ranking job has no items")

    sequence: list[tuple[Path, str]] = []

    if cfg.template.render.intro:
        if cfg.template.assets.intro is None:
            raise RuntimeError("Intro is enabled but assets.intro is missing")
        sequence.append((cfg.template.assets.intro, "intro"))

    for index, item in enumerate(items, start=1):
        local_path = item.get("local_path")
        if not local_path:
            raise RuntimeError(f"Job item #{index} has no local_path")
        sequence.append((Path(local_path), f"clip_{index:03d}"))

        if cfg.template.render.transition:
            if cfg.template.assets.transition is None:
                raise RuntimeError("Transition is enabled but assets.transition is missing")
            sequence.append((cfg.template.assets.transition, f"trans_{index:03d}"))

    if cfg.template.render.outro:
        if cfg.template.assets.outro is None:
            raise RuntimeError("Outro is enabled but assets.outro is missing")
        sequence.append((cfg.template.assets.outro, "outro"))

    if not sequence:
        raise RuntimeError("Nothing to render")

    segment_paths: list[Path] = []
    for index, (source, label) in enumerate(sequence, start=1):
        dest = segments_dir / f"{index:03d}_{label}.mp4"
        _normalize_segment(source, dest, cfg=cfg)
        segment_paths.append(dest)

    durations = [_get_duration(p) for p in segment_paths]

    clip_starts: list[float] = []
    outro_start: float | None = None

    cursor = 0.0
    for (_, label), duration in zip(sequence, durations):
        if label.startswith("clip_"):
            clip_starts.append(cursor)
        if label == "outro":
            outro_start = cursor
        cursor += duration

    if not clip_starts:
        raise RuntimeError("Ranking job has no clips")

    title_end = outro_start if outro_start is not None else cursor

    assigned_numbers = _make_assigned_numbers(len(clip_starts), seed_text=job_id)

    caption_files: list[Path | None] = []
    for idx, item in enumerate(items, start=1):
        caption = str(item.get("caption") or "").strip()
        if caption:
            caption_file = rendered_dir / f"caption_{idx:03d}.txt"
            caption_file.write_text(_short_caption(caption), encoding="utf-8")
            caption_files.append(caption_file)
        else:
            caption_files.append(None)

    merged_base = rendered_dir / "merged_base.mp4"
    concat_list = rendered_dir / "concat.txt"
    with concat_list.open("w", encoding="utf-8") as f:
        for seg in segment_paths:
            f.write(f"file '{seg}'\n")

    _run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-c",
            "copy",
            str(merged_base),
        ]
    )

    title1, title2 = _wrap_title_to_two_lines(make_text)
    title1_file = rendered_dir / "ranking_title_1.txt"
    title1_file.write_text(title1, encoding="utf-8")

    title2_file: Path | None = None
    if title2.strip():
        title2_file = rendered_dir / "ranking_title_2.txt"
        title2_file.write_text(title2, encoding="utf-8")

    overlay_filter = _build_overlay_filter(
        cfg,
        title1_file=title1_file,
        title2_file=title2_file,
        title_start=clip_starts[0],
        title_end=title_end,
        clip_starts=clip_starts,
        assigned_numbers=assigned_numbers,
        caption_files=caption_files,
    )

    final_path = rendered_dir / "final.mp4"
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(merged_base),
            "-filter_complex",
            overlay_filter,
            "-map",
            "[v]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(final_path),
        ]
    )

    concat_list.unlink(missing_ok=True)
    title1_file.unlink(missing_ok=True)
    if title2_file is not None:
        title2_file.unlink(missing_ok=True)
    merged_base.unlink(missing_ok=True)

    for cap in caption_files:
        if cap is not None:
            cap.unlink(missing_ok=True)

    shutil.rmtree(segments_dir, ignore_errors=True)

    job = load_job(template_name, job_id) or job
    job["status"] = "rendered"
    job["render_started_at"] = job.get("render_started_at") or now_iso()
    job["rendered_at"] = now_iso()
    job["rendered_path"] = str(final_path)
    save_job(template_name, job)

    return final_path
