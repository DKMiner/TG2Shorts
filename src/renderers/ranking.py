from __future__ import annotations

import json
import random
import shutil
import subprocess
import textwrap
from pathlib import Path

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

# "center" preserves the centered placement.
# "bottom" anchors the foreground clip near the bottom edge.
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

# Static images only need a single frame and are repeated by the
# overlay filter. This keeps the final render much lighter than
# 30-fps PNG inputs.
OVERLAY_INPUT_FPS = 1


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
    return float(_ffprobe_json(path)["format"]["duration"])


def _find_font_file() -> Path:
    if not RANKING_ASSET_DIR.exists():
        raise RuntimeError(
            f"Missing ranking asset folder: {RANKING_ASSET_DIR}"
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
        candidate = (
            abs(len(line1) - len(line2)),
            line1,
            line2,
        )

        if best is None or candidate[0] < best[0]:
            best = candidate

    assert best is not None
    return best[1], best[2]


def _pick_title_font_size(title1: str, title2: str) -> int:
    longest_line = max(
        len(title1),
        len(title2),
        1,
    )

    if longest_line <= 14:
        return TITLE_FONT_SIZE

    shrink = (longest_line - 14) * 2

    return max(
        TITLE_MIN_FONT_SIZE,
        TITLE_FONT_SIZE - shrink,
    )


def _pick_number_font_size(width: int) -> int:
    return max(
        44,
        min(NUMBER_FONT_SIZE, width // 13),
    )


def _bottom_y_expression(margin: int) -> str:
    # Requested margin when it fits; otherwise reduce the margin so
    # the foreground clip remains inside the final canvas.
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
            foreground_y = (
                f"(H-h)/2+({CLIP_Y_OFFSET})"
            )

        return (
            f"[0:v]split=2[fgsrc][bgsrc];"
            f"[bgsrc]"
            f"scale={width}:{height}:"
            f"force_original_aspect_ratio=increase,"
            f"crop={width}:{height},"
            f"boxblur=20:1[bg];"
            f"[fgsrc]"
            f"scale={width}:{height}:"
            f"force_original_aspect_ratio=decrease"
            f"[fg];"
            f"[bg][fg]"
            f"overlay=(W-w)/2:{foreground_y}:"
            f"eof_action=pass:repeatlast=1,"
            f"format=yuv420p,setsar=1,fps={fps}"
            f"[vbase]"
        )

    if CLIP_POSITION == "bottom":
        pad_y = _pad_bottom_expression(
            CLIP_BOTTOM_MARGIN
        )
    else:
        pad_y = (
            f"(oh-ih)/2+({CLIP_Y_OFFSET})"
        )

    return (
        f"[0:v]"
        f"scale={width}:{height}:"
        f"force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:"
        f"(ow-iw)/2:{pad_y}:"
        f"color=black,"
        f"format=yuv420p,setsar=1,fps={fps}"
        f"[vbase]"
    )


def _normalize_segment(
    source: Path,
    dest: Path,
    *,
    cfg,
) -> None:
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
            "aresample=48000,"
            "aformat=sample_fmts=fltp:"
            "channel_layouts=stereo",
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


def _cluster_is_emoji(cluster: str) -> bool:
    return bool(iter_emoji(cluster))


def _measure_cluster(
    draw: ImageDraw.ImageDraw,
    cluster: str,
    font_size: int,
) -> tuple[int, int]:
    if _cluster_is_emoji(cluster):
        height = max(
            1,
            int(font_size * EMOJI_HEIGHT_SCALE),
        )
        return (
            height
            + int(font_size * EMOJI_GAP_SCALE * 2),
            height,
        )

    font = _load_font(font_size)
    bbox = draw.textbbox(
        (0, 0),
        cluster,
        font=font,
        stroke_width=0,
    )

    width = int(
        round(
            draw.textlength(
                cluster,
                font=font,
            )
        )
    )

    height = max(
        1,
        bbox[3] - bbox[1],
    )

    return width, height


def _draw_mixed_text(
    image: Image.Image,
    text: str,
    *,
    x: int,
    y: int,
    font_size: int,
    fill: str,
    stroke_width: int,
    center_x: bool,
    emoji_assets: dict[str, Path],
) -> tuple[int, int]:
    draw = ImageDraw.Draw(image)
    font = _load_font(font_size)

    import regex

    clusters = regex.findall(
        r"\X",
        text,
    )

    measurements = [
        _measure_cluster(
            draw,
            cluster,
            font_size,
        )
        for cluster in clusters
    ]

    total_width = sum(
        width
        for width, _ in measurements
    )

    max_height = max(
        (
            height
            for _, height in measurements
        ),
        default=font_size,
    )

    cursor_x = (
        int(
            (image.width - total_width)
            / 2
        )
        if center_x
        else x
    )

    for cluster, (advance, _) in zip(
        clusters,
        measurements,
    ):
        if _cluster_is_emoji(cluster):
            emoji_path = emoji_assets.get(cluster)

            if emoji_path is None:
                raise RuntimeError(
                    f"Missing cached Twemoji asset "
                    f"for {cluster!r}"
                )

            emoji_height = max(
                1,
                int(
                    font_size
                    * EMOJI_HEIGHT_SCALE
                ),
            )

            emoji = resize_emoji(
                emoji_path,
                emoji_height,
            )

            gap = int(
                font_size * EMOJI_GAP_SCALE
            )

            cursor_x += gap

            emoji_y = (
                y
                + int(
                    (font_size - emoji.height)
                    / 2
                )
            )

            image.alpha_composite(
                emoji,
                (cursor_x, emoji_y),
            )

            cursor_x += (
                emoji.width + gap
            )
            continue

        draw.text(
            (cursor_x, y),
            cluster,
            font=font,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill="black",
        )

        cursor_x += advance

    return total_width, max_height


def _make_text_image(
    *,
    text: str,
    font_size: int,
    fill: str,
    stroke_width: int,
    emoji_assets: dict[str, Path],
    padding: int = 12,
) -> Image.Image:
    probe = Image.new(
        "RGBA",
        (4000, 400),
        (0, 0, 0, 0),
    )

    dummy_draw = ImageDraw.Draw(probe)

    import regex

    clusters = regex.findall(
        r"\X",
        text,
    )

    total_width = 0
    max_height = font_size

    for cluster in clusters:
        cluster_width, cluster_height = (
            _measure_cluster(
                dummy_draw,
                cluster,
                font_size,
            )
        )

        total_width += cluster_width
        max_height = max(
            max_height,
            cluster_height,
        )

    image = Image.new(
        "RGBA",
        (
            max(1, total_width + padding * 2),
            max_height + padding * 2,
        ),
        (0, 0, 0, 0),
    )

    _draw_mixed_text(
        image,
        text,
        x=padding,
        y=padding,
        font_size=font_size,
        fill=fill,
        stroke_width=stroke_width,
        center_x=False,
        emoji_assets=emoji_assets,
    )

    return image


def _write_static_overlay(
    *,
    cfg,
    title1: str,
    title2: str,
    number_labels: list[str],
    emoji_assets: dict[str, Path],
    output_path: Path,
) -> None:
    width = cfg.template.video.width

    title_size = _pick_title_font_size(
        title1,
        title2,
    )

    number_size = _pick_number_font_size(
        width
    )

    second_title_y = (
        TITLE_TOP_Y
        + title_size
        + TITLE_LINE_SPACING
    )

    last_number_y = (
        NUMBER_START_Y
        + (len(number_labels) - 1)
        * NUMBER_GAP
    )

    bottom = (
        max(
            (
                second_title_y
                + title_size
                if title2.strip()
                else TITLE_TOP_Y
                + title_size
            ),
            last_number_y
            + number_size,
        )
        + 24
    )

    image = Image.new(
        "RGBA",
        (width, bottom),
        (0, 0, 0, 0),
    )

    _draw_mixed_text(
        image,
        title1,
        x=0,
        y=TITLE_TOP_Y,
        font_size=title_size,
        fill="white",
        stroke_width=TITLE_BORDER,
        center_x=True,
        emoji_assets=emoji_assets,
    )

    if title2.strip():
        _draw_mixed_text(
            image,
            title2,
            x=0,
            y=second_title_y,
            font_size=title_size,
            fill="white",
            stroke_width=TITLE_BORDER,
            center_x=True,
            emoji_assets=emoji_assets,
        )

    for position, label in enumerate(
        number_labels,
        start=1,
    ):
        color = NUMBER_COLORS[
            (position - 1)
            % len(NUMBER_COLORS)
        ]

        _draw_mixed_text(
            image,
            label,
            x=NUMBER_LEFT_X,
            y=(
                NUMBER_START_Y
                + (position - 1)
                * NUMBER_GAP
            ),
            font_size=number_size,
            fill=color,
            stroke_width=NUMBER_BORDER,
            center_x=False,
            emoji_assets=emoji_assets,
        )

    image.save(
        output_path,
        "PNG",
    )


def _write_caption_overlay(
    *,
    caption: str,
    number: int,
    emoji_assets: dict[str, Path],
    output_path: Path,
) -> None:
    color = NUMBER_COLORS[
        (number - 1)
        % len(NUMBER_COLORS)
    ]

    image = _make_text_image(
        text=caption,
        font_size=CAPTION_FONT_SIZE,
        fill=color,
        stroke_width=NUMBER_BORDER,
        emoji_assets=emoji_assets,
    )

    image.save(
        output_path,
        "PNG",
    )


def _merge_normalized_segments(
    segments: list[Path],
    output_path: Path,
) -> None:
    """
    Concatenate normalized segments with the concat FILTER rather than
    the concat demuxer / stream-copy path.

    Every segment starts at PTS 0, and the concat filter creates one
    continuous video/audio timeline. The Python timing cursor below is
    based on the same segment durations, so overlay timestamps line up.
    """
    if not segments:
        raise RuntimeError(
            "No normalized segments to merge"
        )

    inputs: list[str] = []
    filter_parts: list[str] = []

    for index, segment in enumerate(
        segments
    ):
        inputs += [
            "-i",
            str(segment),
        ]

        filter_parts.append(
            f"[{index}:v]"
            f"setpts=PTS-STARTPTS"
            f"[v{index}]"
        )

        filter_parts.append(
            f"[{index}:a]"
            f"asetpts=PTS-STARTPTS"
            f"[a{index}]"
        )

    concat_inputs = "".join(
        f"[v{i}][a{i}]"
        for i in range(len(segments))
    )

    filter_parts.append(
        f"{concat_inputs}"
        f"concat=n={len(segments)}:v=1:a=1"
        f"[vout][aout]"
    )

    _run(
        [
            "ffmpeg",
            "-y",
            *inputs,
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            "[vout]",
            "-map",
            "[aout]",
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
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )


def _build_overlay_command(
    *,
    merged_base: Path,
    overlay_entries: list[tuple[Path, float, float]],
    total_duration: float,
    output_path: Path,
) -> list[str]:
    """
    Create one final FFmpeg command for the static overlays.

    Overlay visibility is controlled only by enable() intervals.
    Since merged_base has one continuous timestamp timeline, transitions
    are ordinary frames and the overlays remain visible through them.
    """
    inputs: list[str] = []
    filters: list[str] = []
    current = "0:v"

    for index, (
        image_path,
        start,
        end,
    ) in enumerate(
        overlay_entries,
        start=1,
    ):
        inputs += [
            "-loop",
            "1",
            "-framerate",
            str(OVERLAY_INPUT_FPS),
            "-t",
            f"{total_duration:.3f}",
            "-i",
            str(image_path),
        ]

        next_label = f"ov{index}"

        filters.append(
            f"[{current}]"
            f"[{index}:v]"
            f"overlay=0:0:"
            f"eof_action=repeat:"
            f"repeatlast=1:"
            f"enable='between(t,{start:.3f},{end:.3f})'"
            f"[{next_label}]"
        )

        current = next_label

    filters.append(
        f"[{current}]format=yuv420p[v]"
    )

    return [
        "ffmpeg",
        "-y",
        "-i",
        str(merged_base),
        *inputs,
        "-filter_complex",
        ";".join(filters),
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
        str(output_path),
    ]


def _make_assigned_numbers(
    count: int,
    seed_text: str,
) -> list[int]:
    if count <= 1:
        return [1]

    rng = random.Random(seed_text)

    numbers = list(
        range(2, count + 1)
    )

    rng.shuffle(numbers)
    numbers.append(1)

    return numbers


def _short_caption(
    text: str,
    limit: int = 28,
) -> str:
    text = " ".join(
        (text or "").split()
    )

    if not text:
        return ""

    return textwrap.shorten(
        text,
        width=limit,
        placeholder="…",
    )


def render_job(
    cfg,
    job_id: str,
) -> Path:
    template_name = cfg.active_template

    job = load_job(
        template_name,
        job_id,
    )

    if job is None:
        raise RuntimeError(
            f"Job {job_id} not found"
        )

    rendered_dir = (
        job_folder(
            template_name,
            job_id,
        )
        / "rendered"
    )

    segments_dir = (
        rendered_dir
        / "segments"
    )

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
    save_job(
        template_name,
        job,
    )

    make_text = str(
        job.get("make_text")
        or "Ranking funny viral videos"
    )

    items = job.get(
        "items",
        [],
    )

    if not items:
        raise RuntimeError(
            "Ranking job has no items"
        )

    # --------------------------------------------------------
    # Build exact rendering sequence.
    # --------------------------------------------------------

    sequence: list[
        tuple[Path, str]
    ] = []

    if cfg.template.render.intro:
        if cfg.template.assets.intro is None:
            raise RuntimeError(
                "Intro is enabled but intro asset is missing"
            )

        sequence.append(
            (
                cfg.template.assets.intro,
                "intro",
            )
        )

    for index, item in enumerate(
        items,
        start=1,
    ):
        local_path = item.get(
            "local_path"
        )

        if not local_path:
            raise RuntimeError(
                f"Ranking item #{index} "
                "has no local_path"
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
                    "Transition is enabled but transition "
                    "asset is missing"
                )

            sequence.append(
                (
                    cfg.template.assets.transition,
                    f"transition_{index:03d}",
                )
            )

    if cfg.template.render.outro:
        if cfg.template.assets.outro is None:
            raise RuntimeError(
                "Outro is enabled but outro asset is missing"
            )

        sequence.append(
            (
                cfg.template.assets.outro,
                "outro",
            )
        )

    # --------------------------------------------------------
    # Normalize every segment independently.
    # --------------------------------------------------------

    segment_paths: list[Path] = []

    for index, (
        source,
        label,
    ) in enumerate(
        sequence,
        start=1,
    ):
        destination = (
            segments_dir
            / f"{index:03d}_{label}.mp4"
        )

        _normalize_segment(
            source,
            destination,
            cfg=cfg,
        )

        segment_paths.append(
            destination
        )

    # --------------------------------------------------------
    # Determine timings from normalized segment durations.
    # These timings match the continuous concat-filter timeline.
    # --------------------------------------------------------

    durations = [
        _get_duration(path)
        for path in segment_paths
    ]

    clip_starts: list[float] = []
    outro_start: float | None = None

    cursor = 0.0

    for (
        (_, label),
        duration,
    ) in zip(
        sequence,
        durations,
    ):
        if label.startswith("clip_"):
            clip_starts.append(
                cursor
            )

        if label == "outro":
            outro_start = cursor

        cursor += duration

    if not clip_starts:
        raise RuntimeError(
            "Ranking job contains no clips"
        )

    total_duration = cursor

    # Title/numbers/captions start at clip 1 and remain visible through
    # all later clips and transitions until the outro starts.
    overlay_start = clip_starts[0]
    overlay_end = (
        outro_start
        if outro_start is not None
        else total_duration
    )

    # --------------------------------------------------------
    # Ranking order.
    # --------------------------------------------------------

    assigned_numbers = (
        _make_assigned_numbers(
            len(clip_starts),
            seed_text=job_id,
        )
    )

    number_labels = [
        MEDAL_LABELS.get(
            number,
            f"{number}.",
        )
        for number in range(
            1,
            len(assigned_numbers) + 1,
        )
    ]

    # --------------------------------------------------------
    # Title.
    # --------------------------------------------------------

    title1, title2 = (
        _wrap_title_to_two_lines(
            make_text
        )
    )

    # --------------------------------------------------------
    # Captions and cached Twemoji assets.
    # --------------------------------------------------------

    normalized_captions: list[
        str | None
    ] = []

    for item in items:
        caption = str(
            item.get("caption")
            or ""
        ).strip()

        normalized_captions.append(
            _short_caption(caption)
            if caption
            else None
        )

    emoji_texts = [
        title1,
        title2,
        *number_labels,
        *[
            caption
            for caption in normalized_captions
            if caption
        ],
    ]

    emoji_assets = ensure_text_emoji_assets(
        BASE_DIR,
        emoji_texts,
    )

    # --------------------------------------------------------
    # One static title + numbers overlay.
    # --------------------------------------------------------

    static_overlay = (
        rendered_dir
        / "ranking_static.png"
    )

    _write_static_overlay(
        cfg=cfg,
        title1=title1,
        title2=title2,
        number_labels=number_labels,
        emoji_assets=emoji_assets,
        output_path=static_overlay,
    )

    overlay_entries: list[
        tuple[Path, float, float]
    ] = [
        (
            static_overlay,
            overlay_start,
            overlay_end,
        )
    ]

    # --------------------------------------------------------
    # One caption image per ranking item.
    # Caption N starts with clip N and remains through all later
    # clips/transitions until the outro begins.
    # --------------------------------------------------------

    caption_paths: list[Path] = []

    for index, (
        caption,
        number,
        clip_start,
    ) in enumerate(
        zip(
            normalized_captions,
            assigned_numbers,
            clip_starts,
        ),
        start=1,
    ):
        if not caption:
            continue

        caption_path = (
            rendered_dir
            / f"caption_{index:03d}.png"
        )

        _write_caption_overlay(
            caption=caption,
            number=number,
            emoji_assets=emoji_assets,
            output_path=caption_path,
        )

        caption_paths.append(
            caption_path
        )

        overlay_entries.append(
            (
                caption_path,
                clip_start,
                overlay_end,
            )
        )

    # --------------------------------------------------------
    # Merge normalized clips with the concat FILTER.
    # --------------------------------------------------------

    merged_base = (
        rendered_dir
        / "merged_base.mp4"
    )

    _merge_normalized_segments(
        segment_paths,
        merged_base,
    )

    # --------------------------------------------------------
    # Apply overlays using the continuous timeline.
    # --------------------------------------------------------

    final_path = (
        rendered_dir
        / "final.mp4"
    )

    final_command = _build_overlay_command(
        merged_base=merged_base,
        overlay_entries=overlay_entries,
        total_duration=total_duration,
        output_path=final_path,
    )

    try:
        _run(final_command)
    finally:
        static_overlay.unlink(
            missing_ok=True
        )

        for caption_path in caption_paths:
            caption_path.unlink(
                missing_ok=True
            )

    shutil.rmtree(
        segments_dir,
        ignore_errors=True,
    )

    merged_base.unlink(
        missing_ok=True
    )

    job = (
        load_job(
            template_name,
            job_id,
        )
        or job
    )

    job["status"] = "rendered"
    job["render_started_at"] = (
        job.get(
            "render_started_at"
        )
        or now_iso()
    )
    job["rendered_at"] = now_iso()
    job["rendered_path"] = str(
        final_path
    )

    save_job(
        template_name,
        job,
    )

    return final_path
