from __future__ import annotations

from pathlib import Path
import json
import random
import shutil
import subprocess
import textwrap

from PIL import Image, ImageDraw, ImageFont

from jobs import BASE_DIR, job_folder, load_job, now_iso, save_job
from .twemoji import ensure_text_emoji_assets, iter_emoji, resize_emoji


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
CAPTION_OFFSET_Y = 0

NUMBER_BORDER = 5
TITLE_BORDER = 5

# "center" preserves the old centered placement.
# "bottom" anchors the clip to the bottom with CLIP_BOTTOM_MARGIN
# when there is enough room. If the clip is too tall, the margin is
# automatically reduced so the clip still fits inside the canvas.
CLIP_POSITION = "center"
CLIP_Y_OFFSET = 0
CLIP_BOTTOM_MARGIN = 120

NUMBER_COLORS = [
    "white",
    "yellow",
    "cyan",
    "magenta",
    "lime",
    "orange",
    "red",
]

MEDAL_LABELS = {
    1: "🥇.",
    2: "🥈.",
    3: "🥉.",
}

EMOJI_GAP_SCALE = 0.05
EMOJI_HEIGHT_SCALE = 1.0


def _run(cmd: list[str]) -> None:
    result = subprocess.run(
        cmd,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            cmd,
            output=result.stdout,
            stderr=result.stderr,
        )


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
    return json.loads(result.stdout)


def _has_audio(path: Path) -> bool:
    probe = _ffprobe_json(path)
    return any(
        stream.get("codec_type") == "audio"
        for stream in probe.get("streams", [])
    )


def _get_duration(path: Path) -> float:
    probe = _ffprobe_json(path)
    return float(probe["format"]["duration"])


def _find_font_file() -> Path:
    if not RANKING_ASSET_DIR.exists():
        raise RuntimeError(
            f"Missing asset folder: {RANKING_ASSET_DIR}"
        )

    fonts = sorted(
        p
        for p in RANKING_ASSET_DIR.iterdir()
        if p.suffix.lower() in {".ttf", ".otf"}
    )

    if not fonts:
        raise RuntimeError(
            f"No .ttf or .otf font file found in {RANKING_ASSET_DIR}"
        )

    return fonts[0]


def _load_font(size: int):
    return ImageFont.truetype(
        str(_find_font_file()),
        size=max(1, int(size)),
    )


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


def _pick_title_font_size(title1: str, title2: str) -> int:
    longest_line = max(len(title1), len(title2), 1)
    if longest_line <= 14:
        return TITLE_FONT_SIZE

    shrink = (longest_line - 14) * 2
    return max(
        TITLE_MIN_FONT_SIZE,
        TITLE_FONT_SIZE - shrink,
    )


def _pick_number_font_size(width: int) -> int:
    return max(44, min(NUMBER_FONT_SIZE, width // 13))


def _bottom_y_expression(margin: int) -> str:
    """Clamp the requested bottom margin to the space actually available."""
    return (
        f"'if(gt(H-h-{margin},0),"
        f"H-h-{margin},0)'"
    )


def _pad_bottom_expression(margin: int) -> str:
    return (
        f"'if(gt(oh-ih-{margin},0),"
        f"oh-ih-{margin},0)'"
    )


def _build_base_filter(cfg) -> str:
    width = cfg.template.video.width
    height = cfg.template.video.height
    fps = cfg.template.video.fps
    blur = cfg.template.render.blur_background

    if CLIP_POSITION not in {"center", "bottom"}:
        raise RuntimeError(
            f"Invalid CLIP_POSITION: {CLIP_POSITION!r}"
        )

    if blur:
        if CLIP_POSITION == "bottom":
            foreground_y = _bottom_y_expression(
                CLIP_BOTTOM_MARGIN
            )
        else:
            foreground_y = f"(H-h)/2+({CLIP_Y_OFFSET})"

        return (
            f"[0:v]split=2[fgsrc][bgsrc];"
            f"[bgsrc]scale={width}:{height}:"
            f"force_original_aspect_ratio=increase,"
            f"crop={width}:{height},"
            f"boxblur=20:1[bg];"
            f"[fgsrc]scale={width}:{height}:"
            f"force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:{foreground_y}:"
            f"eof_action=pass:repeatlast=1,"
            f"format=yuv420p,setsar=1,fps={fps}[vbase]"
        )

    if CLIP_POSITION == "bottom":
        pad_y = _pad_bottom_expression(
            CLIP_BOTTOM_MARGIN
        )
    else:
        pad_y = f"(oh-ih)/2+({CLIP_Y_OFFSET})"

    return (
        f"[0:v]scale={width}:{height}:"
        f"force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:{pad_y}:"
        f"color=black,format=yuv420p,setsar=1,fps={fps}[vbase]"
    )


def _normalize_segment(source: Path, dest: Path, *, cfg) -> None:
    if not source.exists():
        raise RuntimeError(
            f"Source clip missing: {source}"
        )

    source_has_audio = _has_audio(source)
    filter_complex = _build_base_filter(cfg)

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
    ]

    if not source_has_audio:
        cmd += [
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
        ]

    cmd += [
        "-filter_complex",
        filter_complex,
        "-map",
        "[vbase]",
    ]

    if source_has_audio:
        cmd += [
            "-map",
            "0:a:0?",
            "-af",
            "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo",
        ]
    else:
        cmd += [
            "-map",
            "1:a",
        ]

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


def _is_emoji_cluster(cluster: str) -> bool:
    return bool(iter_emoji(cluster))


def _measure_cluster(draw, cluster: str, font_size: int) -> int:
    if _is_emoji_cluster(cluster):
        emoji_height = max(
            1,
            int(font_size * EMOJI_HEIGHT_SCALE),
        )
        return emoji_height + int(
            font_size * EMOJI_GAP_SCALE * 2
        )

    font = _load_font(font_size)
    return int(
        round(
            draw.textlength(
                cluster,
                font=font,
            )
        )
    )


def _draw_mixed_text(
    image: Image.Image,
    text: str,
    *,
    x: int,
    y: int,
    font_size: int,
    fill: str,
    stroke_width: int,
    stroke_fill: str = "black",
    center_x: bool = False,
    emoji_assets: dict[str, Path],
) -> None:
    draw = ImageDraw.Draw(image)
    font = _load_font(font_size)

    import regex
    clusters = regex.findall(r"\X", text)

    total_width = sum(
        _measure_cluster(
            draw,
            cluster,
            font_size,
        )
        for cluster in clusters
    )

    cursor_x = (
        int((image.width - total_width) / 2)
        if center_x
        else x
    )

    for cluster in clusters:
        if _is_emoji_cluster(cluster):
            emoji_path = emoji_assets.get(cluster)
            if emoji_path is None:
                raise RuntimeError(
                    f"Missing cached Twemoji asset for {cluster!r}"
                )

            emoji_height = max(
                1,
                int(font_size * EMOJI_HEIGHT_SCALE),
            )
            emoji = resize_emoji(
                emoji_path,
                emoji_height,
            )

            gap = int(
                font_size * EMOJI_GAP_SCALE
            )
            cursor_x += gap

            emoji_y = y + int(
                (font_size - emoji.height) / 2
            )
            image.alpha_composite(
                emoji,
                (cursor_x, emoji_y),
            )
            cursor_x += emoji.width + gap
            continue

        draw.text(
            (cursor_x, y),
            cluster,
            font=font,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )
        cursor_x += int(
            round(
                draw.textlength(
                    cluster,
                    font=font,
                )
            )
        )


def _write_text_overlay(
    *,
    output_path: Path,
    width: int,
    height: int,
    text: str,
    x: int,
    y: int,
    font_size: int,
    fill: str,
    stroke_width: int,
    center_x: bool,
    emoji_assets: dict[str, Path],
) -> None:
    image = Image.new(
        "RGBA",
        (width, height),
        (0, 0, 0, 0),
    )

    _draw_mixed_text(
        image,
        text,
        x=x,
        y=y,
        font_size=font_size,
        fill=fill,
        stroke_width=stroke_width,
        stroke_fill="black",
        center_x=center_x,
        emoji_assets=emoji_assets,
    )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    image.save(output_path, "PNG")


def _make_assigned_numbers(
    count: int,
    seed_text: str,
) -> list[int]:
    if count <= 1:
        return [1]

    rng = random.Random(seed_text)
    nums = list(range(2, count + 1))
    rng.shuffle(nums)
    nums.append(1)
    return nums


def _short_caption(
    text: str,
    limit: int = 28,
) -> str:
    text = " ".join((text or "").split())
    if not text:
        return ""
    return textwrap.shorten(
        text,
        width=limit,
        placeholder="…",
    )


def _build_overlay_inputs(
    *,
    overlays: list[tuple[Path, float, float]],
    total_duration: float,
    fps: int,
) -> tuple[list[str], str]:
    """Build finite PNG inputs and a single continuous overlay chain."""
    inputs: list[str] = []
    filters: list[str] = []
    current = "0:v"

    for index, (path, start, end) in enumerate(
        overlays,
        start=1,
    ):
        inputs += [
            "-loop",
            "1",
            "-framerate",
            str(fps),
            "-t",
            f"{total_duration:.3f}",
            "-i",
            str(path),
        ]

        next_label = f"ov{index}"
        filters.append(
            f"[{current}][{index}:v]overlay=0:0:"
            f"eof_action=repeat:repeatlast=1:"
            f"enable='between(t,{start:.3f},{end:.3f})'"
            f"[{next_label}]"
        )
        current = next_label

    filters.append(
        f"[{current}]format=yuv420p[v]"
    )

    return inputs, ";".join(filters)


def render_job(cfg, job_id: str) -> Path:
    template_name = cfg.active_template
    job = load_job(template_name, job_id)
    if job is None:
        raise RuntimeError(
            f"Job {job_id} not found"
        )

    rendered_dir = (
        job_folder(template_name, job_id)
        / "rendered"
    )
    segments_dir = rendered_dir / "segments"

    rendered_dir.mkdir(
        parents=True,
        exist_ok=True,
    )
    segments_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    job["status"] = "rendering"
    job["render_started_at"] = now_iso()
    save_job(template_name, job)

    make_text = str(
        job.get("make_text")
        or "Ranking funny viral videos"
    )
    items = job.get("items", [])
    if not items:
        raise RuntimeError(
            "Ranking job has no items"
        )

    sequence: list[tuple[Path, str]] = []

    if cfg.template.render.intro:
        if cfg.template.assets.intro is None:
            raise RuntimeError(
                "Intro is enabled but assets.intro is missing"
            )
        sequence.append(
            (cfg.template.assets.intro, "intro")
        )

    for index, item in enumerate(
        items,
        start=1,
    ):
        local_path = item.get("local_path")
        if not local_path:
            raise RuntimeError(
                f"Job item #{index} has no local_path"
            )

        sequence.append(
            (
                Path(local_path),
                f"clip_{index:03d}",
            )
        )

        if cfg.template.render.transition:
            if cfg.template.assets.transition is None:
                raise RuntimeError(
                    "Transition is enabled but assets.transition is missing"
                )
            sequence.append(
                (
                    cfg.template.assets.transition,
                    f"trans_{index:03d}",
                )
            )

    if cfg.template.render.outro:
        if cfg.template.assets.outro is None:
            raise RuntimeError(
                "Outro is enabled but assets.outro is missing"
            )
        sequence.append(
            (cfg.template.assets.outro, "outro")
        )

    if not sequence:
        raise RuntimeError("Nothing to render")

    segment_paths: list[Path] = []

    for index, (source, label) in enumerate(
        sequence,
        start=1,
    ):
        dest = (
            segments_dir
            / f"{index:03d}_{label}.mp4"
        )
        _normalize_segment(
            source,
            dest,
            cfg=cfg,
        )
        segment_paths.append(dest)

    durations = [
        _get_duration(path)
        for path in segment_paths
    ]

    clip_starts: list[float] = []
    outro_start: float | None = None
    cursor = 0.0

    for (_, label), duration in zip(
        sequence,
        durations,
    ):
        if label.startswith("clip_"):
            clip_starts.append(cursor)
        if label == "outro":
            outro_start = cursor
        cursor += duration

    if not clip_starts:
        raise RuntimeError("Ranking job has no clips")

    total_duration = cursor
    title_start = clip_starts[0]
    title_end = (
        outro_start
        if outro_start is not None
        else total_duration
    )

    assigned_numbers = _make_assigned_numbers(
        len(clip_starts),
        seed_text=job_id,
    )

    title1, title2 = _wrap_title_to_two_lines(
        make_text
    )
    title_size = _pick_title_font_size(
        title1,
        title2,
    )
    number_size = _pick_number_font_size(
        cfg.template.video.width
    )

    captions = [
        _short_caption(
            str(item.get("caption") or "").strip()
        )
        for item in items
    ]

    emoji_texts = [
        title1,
        title2,
        *captions,
        *(
            MEDAL_LABELS.get(
                number,
                f"{number}.",
            )
            for number in range(
                1,
                len(assigned_numbers) + 1,
            )
        ),
    ]

    emoji_assets = ensure_text_emoji_assets(
        BASE_DIR,
        emoji_texts,
    )

    overlays: list[tuple[Path, float, float]] = []
    width = cfg.template.video.width
    height = cfg.template.video.height

    title1_png = rendered_dir / "title_1.png"
    _write_text_overlay(
        output_path=title1_png,
        width=width,
        height=height,
        text=title1,
        x=0,
        y=TITLE_TOP_Y,
        font_size=title_size,
        fill="white",
        stroke_width=TITLE_BORDER,
        center_x=True,
        emoji_assets=emoji_assets,
    )
    overlays.append(
        (title1_png, title_start, title_end)
    )

    if title2.strip():
        title2_png = rendered_dir / "title_2.png"
        _write_text_overlay(
            output_path=title2_png,
            width=width,
            height=height,
            text=title2,
            x=0,
            y=(
                TITLE_TOP_Y
                + title_size
                + TITLE_LINE_SPACING
            ),
            font_size=title_size,
            fill="white",
            stroke_width=TITLE_BORDER,
            center_x=True,
            emoji_assets=emoji_assets,
        )
        overlays.append(
            (title2_png, title_start, title_end)
        )

    for rank in range(
        1,
        len(assigned_numbers) + 1,
    ):
        label = MEDAL_LABELS.get(
            rank,
            f"{rank}.",
        )
        number_png = (
            rendered_dir
            / f"number_{rank:03d}.png"
        )

        _write_text_overlay(
            output_path=number_png,
            width=width,
            height=height,
            text=label,
            x=NUMBER_LEFT_X,
            y=(
                NUMBER_START_Y
                + (rank - 1) * NUMBER_GAP
            ),
            font_size=number_size,
            fill=NUMBER_COLORS[
                (rank - 1)
                % len(NUMBER_COLORS)
            ],
            stroke_width=NUMBER_BORDER,
            center_x=False,
            emoji_assets=emoji_assets,
        )
        overlays.append(
            (number_png, title_start, title_end)
        )

    for index, (item, rank, start_time) in enumerate(
        zip(items, assigned_numbers, clip_starts),
        start=1,
    ):
        caption = captions[index - 1]
        if not caption:
            continue

        caption_png = (
            rendered_dir
            / f"caption_{index:03d}.png"
        )

        _write_text_overlay(
            output_path=caption_png,
            width=width,
            height=height,
            text=caption,
            x=CAPTION_LEFT_X,
            y=(
                NUMBER_START_Y
                + (rank - 1) * NUMBER_GAP
                + CAPTION_OFFSET_Y
            ),
            font_size=CAPTION_FONT_SIZE,
            fill=NUMBER_COLORS[
                (rank - 1)
                % len(NUMBER_COLORS)
            ],
            stroke_width=NUMBER_BORDER,
            center_x=False,
            emoji_assets=emoji_assets,
        )
        overlays.append(
            (caption_png, start_time, title_end)
        )

    merged_base = rendered_dir / "merged_base.mp4"
    concat_list = rendered_dir / "concat.txt"

    with concat_list.open("w", encoding="utf-8") as file:
        for segment in segment_paths:
            escaped = (
                str(segment)
                .replace("\\", "\\\\")
                .replace("'", "'\\''")
            )
            file.write(f"file '{escaped}'\n")

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

    overlay_inputs, overlay_filter = _build_overlay_inputs(
        overlays=overlays,
        total_duration=total_duration,
        fps=cfg.template.video.fps,
    )

    final_path = rendered_dir / "final.mp4"

    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(merged_base),
            *overlay_inputs,
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
            "-t",
            f"{total_duration:.3f}",
            "-movflags",
            "+faststart",
            str(final_path),
        ]
    )

    concat_list.unlink(missing_ok=True)
    merged_base.unlink(missing_ok=True)

    for path in rendered_dir.glob("*.png"):
        path.unlink(missing_ok=True)
    for path in rendered_dir.glob("*.txt"):
        path.unlink(missing_ok=True)

    shutil.rmtree(
        segments_dir,
        ignore_errors=True,
    )

    job = load_job(
        template_name,
        job_id,
    ) or job
    job["status"] = "rendered"
    job["rendered_at"] = now_iso()
    job["rendered_path"] = str(final_path)
    save_job(template_name, job)

    return final_path
