from __future__ import annotations

from pathlib import Path
import json
import shutil
import subprocess

from jobs import job_folder, load_job, now_iso, save_job


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


def _escape_drawtext(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
        .replace(",", "\\,")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def _build_filter(cfg, *, intro_segment: bool, part_text: str | None) -> str:
    width = cfg.template.video.width
    height = cfg.template.video.height
    fps = cfg.template.video.fps
    blur = cfg.template.render.blur_background

    if blur:
        base = (
            f"[0:v]split=2[fgsrc][bgsrc];"
            f"[bgsrc]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=20:1[bg];"
            f"[fgsrc]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1,format=yuv420p,setsar=1,fps={fps}[vbase]"
        )
    else:
        base = (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"format=yuv420p,setsar=1,fps={fps}[vbase]"
        )

    if intro_segment and part_text and cfg.template.render.intro_part.enabled:
        txt = _escape_drawtext(part_text)
        start = cfg.template.render.intro_part.start
        end = cfg.template.render.intro_part.end
        font_size = cfg.template.render.intro_part.font_size
        y_offset = cfg.template.render.intro_part.y_offset

        return (
            f"{base};"
            f"[vbase]drawtext=text='{txt}':fontcolor=white:fontsize={font_size}:"
            f"borderw=6:bordercolor=black:"
            f"x=(w-text_w)/2:y=(h-text_h)/2+{y_offset}:"
            f"enable='between(t,{start},{end})'[v]"
        )

    return f"{base};[vbase]null[v]"


def _normalize_clip(
    source: Path,
    dest: Path,
    *,
    cfg,
    intro_segment: bool = False,
    part_text: str | None = None,
) -> None:
    if not source.exists():
        raise RuntimeError(f"Source clip missing: {source}")

    source_has_audio = _has_audio(source)
    filter_complex = _build_filter(cfg, intro_segment=intro_segment, part_text=part_text)

    cmd = ["ffmpeg", "-y", "-i", str(source)]

    if not source_has_audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]

    cmd += ["-filter_complex", filter_complex, "-map", "[v]"]

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

    items = job.get("items", [])
    part_text = str(job.get("part_text") or "1")

    sequence: list[tuple[Path, str, bool]] = []

    if cfg.template.render.intro:
        if cfg.template.assets.intro is None:
            raise RuntimeError("Intro is enabled but assets.intro is missing")
        sequence.append((cfg.template.assets.intro, "intro", True))

    for index, item in enumerate(items):
        local_path = item.get("local_path")
        if not local_path:
            raise RuntimeError(f"Job item #{index + 1} has no local_path")

        sequence.append((Path(local_path), "clip", False))

        if cfg.template.render.transition:
            if cfg.template.assets.transition is None:
                raise RuntimeError("Transition is enabled but assets.transition is missing")
            sequence.append((cfg.template.assets.transition, "transition", False))

    if cfg.template.render.outro:
        if cfg.template.assets.outro is None:
            raise RuntimeError("Outro is enabled but assets.outro is missing")
        sequence.append((cfg.template.assets.outro, "outro", False))

    if not sequence:
        raise RuntimeError("Nothing to render")

    segment_paths: list[Path] = []
    for index, (source, label, intro_segment) in enumerate(sequence, start=1):
        segment_path = segments_dir / f"{index:03d}_{label}.mp4"
        _normalize_clip(
            source,
            segment_path,
            cfg=cfg,
            intro_segment=intro_segment,
            part_text=part_text if intro_segment else None,
        )
        segment_paths.append(segment_path)

    final_path = rendered_dir / "final.mp4"

    if len(segment_paths) == 1:
        shutil.copy2(segment_paths[0], final_path)
    else:
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
                str(final_path),
            ]
        )

        concat_list.unlink(missing_ok=True)

    job = load_job(template_name, job_id) or job
    job["status"] = "rendered"
    job["render_started_at"] = job.get("render_started_at") or now_iso()
    job["rendered_at"] = now_iso()
    job["rendered_path"] = str(final_path)
    save_job(template_name, job)

    return final_path
