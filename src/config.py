from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import copy
import os

import yaml
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "config.yaml"
ENV_PATH = BASE_DIR / "config" / ".env"


@dataclass
class TelegramConfig:
    input_chat: int | None
    review_chat: int | None


@dataclass
class VideoConfig:
    width: int
    height: int
    fps: int


@dataclass
class IntroPartConfig:
    enabled: bool
    start: float
    end: float
    font_size: int
    y_offset: int


@dataclass
class RenderConfig:
    intro: bool
    transition: bool
    outro: bool
    blur_background: bool
    intro_part: IntroPartConfig


@dataclass
class AssetsConfig:
    intro: Path | None
    transition: Path | None
    outro: Path | None


@dataclass
class TemplateConfig:
    name: str
    telegram: TelegramConfig
    video: VideoConfig
    render: RenderConfig
    assets: AssetsConfig


@dataclass
class AppConfig:
    active_template: str
    template: TemplateConfig
    available_templates: tuple[str, ...]
    bot_token: str
    tg_api_id: int
    tg_api_hash: str


def load_raw_config() -> dict:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"Missing config file: {CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_raw_config(raw: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, sort_keys=False, allow_unicode=True)


def _resolve_path(value) -> Path | None:
    if value in (None, ""):
        return None
    p = Path(value)
    return p if p.is_absolute() else (BASE_DIR / p)


def _bool(value, default=False) -> bool:
    if value is None:
        return default
    return bool(value)


def _int(value, default=0) -> int:
    if value in (None, ""):
        return default
    return int(value)


def _float(value, default=0.0) -> float:
    if value in (None, ""):
        return default
    return float(value)


def _set_nested(d: dict, path: list[str], value) -> None:
    cur = d
    for key in path[:-1]:
        if key not in cur or not isinstance(cur[key], dict):
            cur[key] = {}
        cur = cur[key]
    cur[path[-1]] = value


def list_templates() -> tuple[str, ...]:
    raw = load_raw_config()
    templates = raw.get("templates", {})
    if not isinstance(templates, dict):
        return ()
    return tuple(templates.keys())


def get_active_template_name(raw: dict | None = None) -> str:
    if raw is None:
        raw = load_raw_config()
    active = raw.get("active_template")
    templates = raw.get("templates", {})
    if active and active in templates:
        return active
    if isinstance(templates, dict) and templates:
        return next(iter(templates.keys()))
    raise RuntimeError("No templates found in config.yaml")


def switch_active_template(template_name: str) -> None:
    raw = load_raw_config()
    templates = raw.get("templates", {})
    if template_name not in templates:
        raise RuntimeError(f"Unknown template: {template_name}")
    raw["active_template"] = template_name
    save_raw_config(raw)


def create_template(template_name: str, clone_from: str | None = None) -> None:
    raw = load_raw_config()
    templates = raw.setdefault("templates", {})
    if template_name in templates:
        raise RuntimeError(f"Template already exists: {template_name}")

    source_name = clone_from or get_active_template_name(raw)
    if source_name not in templates:
        raise RuntimeError(f"Source template not found: {source_name}")

    templates[template_name] = copy.deepcopy(templates[source_name])
    save_raw_config(raw)


def update_template(template_name: str, path: list[str], value) -> None:
    raw = load_raw_config()
    templates = raw.setdefault("templates", {})
    if template_name not in templates:
        raise RuntimeError(f"Unknown template: {template_name}")

    _set_nested(templates[template_name], path, value)
    save_raw_config(raw)


def update_active_template(path: list[str], value) -> None:
    raw = load_raw_config()
    active = get_active_template_name(raw)
    update_template(active, path, value)


def _load_template_config(template_name: str, template_raw: dict) -> TemplateConfig:
    telegram_raw = template_raw.get("telegram", {})
    video_raw = template_raw.get("video", {})
    render_raw = template_raw.get("render", {})
    intro_part_raw = render_raw.get("intro_part", {})
    assets_raw = template_raw.get("assets", {})

    return TemplateConfig(
        name=template_name,
        telegram=TelegramConfig(
            input_chat=telegram_raw.get("input_chat"),
            review_chat=telegram_raw.get("review_chat"),
        ),
        video=VideoConfig(
            width=_int(video_raw.get("width"), 1080),
            height=_int(video_raw.get("height"), 1920),
            fps=_int(video_raw.get("fps"), 30),
        ),
        render=RenderConfig(
            intro=_bool(render_raw.get("intro"), True),
            transition=_bool(render_raw.get("transition"), True),
            outro=_bool(render_raw.get("outro"), True),
            blur_background=_bool(render_raw.get("blur_background"), False),
            intro_part=IntroPartConfig(
                enabled=_bool(intro_part_raw.get("enabled"), True),
                start=_float(intro_part_raw.get("start"), 6.26),
                end=_float(intro_part_raw.get("end"), 7.25),
                font_size=_int(intro_part_raw.get("font_size"), 96),
                y_offset=_int(intro_part_raw.get("y_offset"), -40),
            ),
        ),
        assets=AssetsConfig(
            intro=_resolve_path(assets_raw.get("intro")),
            transition=_resolve_path(assets_raw.get("transition")),
            outro=_resolve_path(assets_raw.get("outro")),
        ),
    )


def load_config() -> AppConfig:
    load_dotenv(ENV_PATH)

    raw = load_raw_config()
    active_template = get_active_template_name(raw)
    templates = raw.get("templates", {})
    template_raw = templates[active_template]

    bot_token = os.getenv("BOT_TOKEN")
    tg_api_id = os.getenv("TG_API_ID")
    tg_api_hash = os.getenv("TG_API_HASH")

    if not bot_token:
        raise RuntimeError("Missing BOT_TOKEN in config/.env")
    if not tg_api_id or not tg_api_hash:
        raise RuntimeError("Missing TG_API_ID or TG_API_HASH in config/.env")

    return AppConfig(
        active_template=active_template,
        template=_load_template_config(active_template, template_raw),
        available_templates=tuple(templates.keys()),
        bot_token=bot_token,
        tg_api_id=int(tg_api_id),
        tg_api_hash=tg_api_hash,
    )
