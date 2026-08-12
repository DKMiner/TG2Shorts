from __future__ import annotations

from pathlib import Path
import re
from urllib.request import Request, urlopen

from PIL import Image


TWEMOJI_VERSION = "17.0.3"
TWEMOJI_ASSET_DIR_NAME = "emoji"
TWEMOJI_BASE_URL = (
    "https://cdn.jsdelivr.net/gh/jdecked/twemoji@"
    f"{TWEMOJI_VERSION}/assets/72x72"
)

# Broad Unicode ranges used to decide which grapheme clusters are worth
# asking Twemoji about. Unsupported clusters are handled by the 404 check.
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),
    (0x1FC00, 0x1FFFD),
    (0x2300, 0x23FF),
    (0x2600, 0x27BF),
    (0x2B00, 0x2BFF),
)

_GRAPHEME_RE = re.compile(r"\X", re.UNICODE)


def _asset_dir(base_dir: Path) -> Path:
    path = base_dir / "assets" / TWEMOJI_ASSET_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _contains_emoji_codepoint(text: str) -> bool:
    return any(
        any(start <= ord(char) <= end for start, end in _EMOJI_RANGES)
        for char in text
    )


def iter_emoji(text: str) -> list[str]:
    """Return grapheme clusters that look like Unicode emoji."""
    return [
        cluster
        for cluster in _GRAPHEME_RE.findall(text)
        if _contains_emoji_codepoint(cluster)
    ]


def emoji_codepoints(emoji: str) -> str:
    """Return Twemoji's lowercase hyphen-separated codepoint filename."""
    return "-".join(
        f"{ord(char):x}"
        for char in emoji
        if ord(char) not in {0xFE0E, 0xFE0F}
    )


def emoji_path(base_dir: Path, emoji: str) -> Path:
    return _asset_dir(base_dir) / f"{emoji_codepoints(emoji)}.png"


def _download(url: str, destination: Path) -> None:
    request = Request(
        url,
        headers={"User-Agent": "TG2Shorts/1.0"},
    )

    temporary = destination.with_suffix(".tmp")

    try:
        with urlopen(request, timeout=30) as response:
            data = response.read()

        if not data:
            raise RuntimeError("Twemoji returned an empty file")

        temporary.write_bytes(data)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_emoji_asset(base_dir: Path, emoji: str) -> Path:
    """Return a cached Twemoji PNG, downloading it once if necessary."""
    destination = emoji_path(base_dir, emoji)

    if destination.exists() and destination.stat().st_size > 0:
        return destination

    filename = destination.name
    url = f"{TWEMOJI_BASE_URL}/{filename}"

    try:
        _download(url, destination)
    except Exception as exc:
        raise RuntimeError(
            f"Could not download Twemoji asset for {emoji!r} "
            f"({filename}) from {url}: {exc}"
        ) from exc

    try:
        with Image.open(destination) as image:
            image.verify()
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise RuntimeError(
            f"Downloaded Twemoji asset is not a valid PNG: {destination}"
        ) from exc

    return destination


def ensure_text_emoji_assets(base_dir: Path, texts: list[str]) -> dict[str, Path]:
    """Ensure every emoji used by the supplied texts is cached locally."""
    result: dict[str, Path] = {}

    for text in texts:
        for emoji in iter_emoji(text):
            if emoji in result:
                continue
            result[emoji] = ensure_emoji_asset(base_dir, emoji)

    return result


def resize_emoji(image_path: Path, target_height: int) -> Image.Image:
    """Load a Twemoji PNG and scale it to the requested visual height."""
    image = Image.open(image_path).convert("RGBA")

    target_height = max(1, int(target_height))
    ratio = target_height / image.height
    target_width = max(1, int(round(image.width * ratio)))

    return image.resize(
        (target_width, target_height),
        Image.Resampling.LANCZOS,
    )
