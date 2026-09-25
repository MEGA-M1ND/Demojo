"""AI provider registry.

The OpenRouter provider is the default. The fixture provider is only used when
DEMOJO_PROVIDER_MODE=fixture is set explicitly (tests, offline demos); its
outputs are always labelled as fixture output, never as AI results.
"""

from __future__ import annotations

from typing import Protocol

from ..config import Settings, get_settings


class Provider(Protocol):
    name: str  # "openrouter" | "fixture"
    vision_model: str
    story_model: str
    tts_model: str
    tts_voice: str

    def analyze_image(self, ledger, asset: dict, messages: list[dict]): ...
    def analyze_video(self, ledger, asset: dict, frames: list[dict], messages: list[dict], check): ...
    def plan_story(self, ledger, ctx, messages: list[dict], check): ...
    def regenerate_scene(self, ledger, ctx, scene_index: int, messages: list[dict], check): ...
    def synthesize(self, ledger, text: str) -> tuple[bytes, str, str | None]: ...


def get_provider(settings: Settings | None = None) -> Provider:
    s = settings or get_settings()
    if s.fixture_mode:
        from .fixture import FixtureProvider

        return FixtureProvider(s)
    from .openrouter_provider import OpenRouterProvider

    return OpenRouterProvider(s)
