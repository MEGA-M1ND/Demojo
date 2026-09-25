"""Server-side settings. Secrets never leave the backend."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

# Defaults verified against the OpenRouter model catalog on 2026-09-25.
# See docs/MODELS.md for the selection rationale and pricing assumptions.
DEFAULT_VISION_MODEL = "google/gemini-3.1-flash-lite"
DEFAULT_STORY_MODEL = "google/gemini-3.1-flash-lite"
DEFAULT_TTS_MODEL = "hexgrad/kokoro-82m"
DEFAULT_TTS_VOICE = "af_heart"
DEFAULT_TTS_FORMAT = "mp3"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing env vars win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    openrouter_api_key: str | None
    openrouter_base_url: str
    vision_model: str
    story_model: str
    tts_model: str
    tts_voice: str
    tts_format: str
    provider_mode: str  # "openrouter" | "fixture"
    max_upload_mb: int
    max_image_mb: int
    max_images: int
    max_video_mb: int
    max_video_seconds: int
    max_image_pixels: int
    ai_project_budget_usd: float
    enable_generative_broll: bool
    ffmpeg_timeout_s: int
    render_threads: int
    app_referer: str = "http://127.0.0.1:8000"
    app_title: str = "Demojo"
    extra: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "demojo.sqlite3"

    @property
    def fixture_mode(self) -> bool:
        return self.provider_mode == "fixture"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    root = Path(__file__).resolve().parents[2]
    _load_dotenv(Path(os.environ.get("DEMOJO_ENV_FILE", root / ".env")))
    data_dir = Path(os.environ.get("DATA_DIR") or "data")
    if not data_dir.is_absolute():
        data_dir = root / data_dir  # relative paths are relative to the repository root
    data_dir = data_dir.resolve()
    mode = os.environ.get("DEMOJO_PROVIDER_MODE", "openrouter").strip().lower()
    if mode not in {"openrouter", "fixture"}:
        mode = "openrouter"
    key = os.environ.get("OPENROUTER_API_KEY") or None
    return Settings(
        data_dir=data_dir,
        openrouter_api_key=key,
        openrouter_base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
        vision_model=os.environ.get("OPENROUTER_VISION_MODEL") or DEFAULT_VISION_MODEL,
        story_model=os.environ.get("OPENROUTER_STORY_MODEL") or DEFAULT_STORY_MODEL,
        tts_model=os.environ.get("OPENROUTER_TTS_MODEL") or DEFAULT_TTS_MODEL,
        tts_voice=os.environ.get("OPENROUTER_TTS_VOICE") or DEFAULT_TTS_VOICE,
        tts_format=DEFAULT_TTS_FORMAT,
        provider_mode=mode,
        max_upload_mb=_int("MAX_UPLOAD_MB", 350),
        max_image_mb=_int("MAX_IMAGE_MB", 15),
        max_images=_int("MAX_IMAGES", 12),
        max_video_mb=_int("MAX_VIDEO_MB", 250),
        max_video_seconds=_int("MAX_VIDEO_SECONDS", 300),
        max_image_pixels=_int("MAX_IMAGE_PIXELS", 40_000_000),
        ai_project_budget_usd=_float("AI_PROJECT_BUDGET_USD", 3.0),
        enable_generative_broll=_bool("ENABLE_GENERATIVE_BROLL", False),
        ffmpeg_timeout_s=_int("FFMPEG_TIMEOUT_S", 900),
        render_threads=_int("RENDER_THREADS", 2),
    )


def reset_settings_cache() -> None:
    get_settings.cache_clear()
