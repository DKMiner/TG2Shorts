from __future__ import annotations

from pathlib import Path
import json
import shutil
import subprocess

from jobs import BASE_DIR, job_folder, load_job, now_iso, save_job


RANKING_ASSET_DIR = BASE_DIR / "assets" / "ranking"


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


def _wrap_title_to_two_lines(title: str) -> str:
    """
    Try to fit the title into two lines by choosing the most balanced split.
    Falls back to the original text if there is only one word.
    """
    words = [w for w in title.split() if w]
    if not words:
        return "Ranking funny viral videos"
    if len(words) == 1:
        return words[0]

    best: tuple[int, str, str] | None = None

    for split in range(1, len(words)):
        line1 = " ".join(words[:split])
        line2 = " ".join(words[split:])
        score = abs(len(line1) - len(line2))
        candidate = (score, line1, line2)
        if best is None or candidate[0] < best[0]:
            best = candidate

    assert best is not None
    return f"{best[1]}\n{best[2]}"


def _pick_title_font_size(width: int, title: str) -> int:
    longest = max((len(line) for line in title.splitlines()), default=10)
    # Smaller when the title is longer; still readable on 1080p.
    size = int(width / max(12, longest * 0.78))
    return max(34, min(68, size))


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
    titlefile: Path,
    title_start: float,
    title_end: float,
    clip_starts: list[float],
) -> str:
    font_path = _find_font_file()
    fontfile = _escape_path(font_path)
    titlefile_escaped = _escape_path(titlefile)

    width = cfg.template.video.width
    height = cfg.template.video.height

    title_size = _pick_title_font_size(width, titlefile.read_text(encoding="utf-8"))
    number_size = _pick_number_font_size(width)

    # Lower than before, so the title has breathing room above the numbers.
    title_y = max(30, height // 28)
    left_x = max(50, width // 25)
    number_start_y = max(250, height // 6)
    number_gap = max(64, height // 10)

    number_palette = [
        "white",
        "yellow",
        "cyan",
        "magenta",
        "lime",
        "orange",
        "red",
    ]

    parts: list[str] = []

    parts.append(
        f"[0:v]drawtext=fontfile='{fontfile}':textfile='{titlefile_escaped}':reload=0:"
        f"fontcolor=white:fontsize={title_size}:"
        f"box=1:boxcolor=black@0.45:boxborderw=18:line_spacing=8:"
        f"x=(w-text_w)/2:y={title_y}:"
        f"enable='between(t,{title_start:.3f},{title_end:.3f})'[v0]"
    )

    current = "v0"
    for i, start_time in enumerate(clip_starts, start=1):
        color = number_palette[(i - 1) % len(number_palette)]
        next_label = f"v{i}"
        y = number_start_y + (i - 1) * number_gap

        parts.append(
            f"[{current}]drawtext=fontfile='{fontfile}':text='{i}.':"
            f"fontcolor={color}:fontsize={number_size}:"
            f"borderw=5:bordercolor=black:"
            f"x={left_x}:y={y}:"
            f"enable='between(t,{start_time:.3f},{title_end:.3f})'[{next_label}]"
        )
        current = next_label

    parts.append(f"[{current}]null[v]")
    return ";".join(parts)


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

    mark_text = str(job.get("mark_text") or job.get("part_text") or "Ranking funny viral videos")
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

    titlefile = rendered_dir / "ranking_title.txt"
    titlefile.write_text(_wrap_title_to_two_lines(mark_text), encoding="utf-8")

    overlay_filter = _build_overlay_filter(
        cfg,
        titlefile=titlefile,
        title_start=clip_starts[0],
        title_end=title_end,
        clip_starts=clip_starts,
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
    titlefile.unlink(missing_ok=True)
    merged_base.unlink(missing_ok=True)
    shutil.rmtree(segments_dir, ignore_errors=True)

    job = load_job(template_name, job_id) or job
    job["status"] = "rendered"
    job["render_started_at"] = job.get("render_started_at") or now_iso()
    job["rendered_at"] = now_iso()
    job["rendered_path"] = str(final_path)
    save_job(template_name, job)

    return final_path
