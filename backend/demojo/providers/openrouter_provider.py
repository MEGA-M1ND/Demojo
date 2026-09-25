"""Provider methods backed by OpenRouter."""

from __future__ import annotations

from ..ai_schemas import (
    IMAGE_ANALYSIS_SCHEMA,
    SCENE_ONLY_SCHEMA,
    STORY_SCHEMA,
    VIDEO_ANALYSIS_SCHEMA,
    ImageAnalysis,
    PlannedSceneOnly,
    PlannedStory,
    VideoAnalysis,
)
from ..config import Settings
from .openrouter import OpenRouterClient


class OpenRouterProvider:
    name = "openrouter"

    def __init__(self, settings: Settings, client: OpenRouterClient | None = None):
        self.s = settings
        self.client = client or OpenRouterClient(settings)
        self.vision_model = settings.vision_model
        self.story_model = settings.story_model
        self.tts_model = settings.tts_model
        self.tts_voice = settings.tts_voice

    def analyze_image(self, ledger, asset: dict, messages: list[dict]) -> ImageAnalysis:
        return self.client.chat_json(
            ledger, purpose="analyze_image", model=self.vision_model, messages=messages, schema_name="image_analysis",
            schema=IMAGE_ANALYSIS_SCHEMA, result_model=ImageAnalysis, max_tokens=1400, temperature=0.2, images=1,
        )

    def analyze_video(self, ledger, asset: dict, frames: list[dict], messages: list[dict], check) -> VideoAnalysis:
        return self.client.chat_json(
            ledger, purpose="analyze_video", model=self.vision_model, messages=messages, schema_name="recording_analysis",
            schema=VIDEO_ANALYSIS_SCHEMA, result_model=VideoAnalysis, max_tokens=3000, temperature=0.2,
            images=len(frames), semantic_check=check,
        )

    def plan_story(self, ledger, ctx, messages: list[dict], check) -> PlannedStory:
        return self.client.chat_json(
            ledger, purpose="plan", model=self.story_model, messages=messages, schema_name="storyboard",
            schema=STORY_SCHEMA, result_model=PlannedStory, max_tokens=5000, temperature=0.4, semantic_check=check,
        )

    def regenerate_scene(self, ledger, ctx, scene_index: int, messages: list[dict], check) -> PlannedSceneOnly:
        return self.client.chat_json(
            ledger, purpose="scene", model=self.story_model, messages=messages, schema_name="scene",
            schema=SCENE_ONLY_SCHEMA, result_model=PlannedSceneOnly, max_tokens=1500, temperature=0.6, semantic_check=check,
        )

    def synthesize(self, ledger, text: str) -> tuple[bytes, str, str | None]:
        audio, gen_id = self.client.speech(ledger, text=text, model=self.tts_model, voice=self.tts_voice, fmt=self.s.tts_format)
        return audio, self.s.tts_format, gen_id
