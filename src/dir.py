from pathlib import Path


def ensure_dirs(base_dir: Path) -> dict[str, Path]:
    data_dir = base_dir / "data"
    queues_dir = data_dir / "queues"
    jobs_dir = data_dir / "jobs"
    whisper_dir = data_dir / "whisper"
    logs_dir = base_dir / "logs"

    paths = {
        "data": data_dir,
        "queues": queues_dir,
        "jobs": jobs_dir,
        "whisper": whisper_dir,
        "logs": logs_dir,
    }

    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)

    return paths
