"""End-to-end pipeline behaviour with instrumented providers (no network)."""

from __future__ import annotations

import pytest

from demojo import pipeline
from demojo.config import get_settings
from demojo.errors import AppError
from demojo.projects import get_project, get_storyboard, list_exports, project_dir, save_storyboard
from demojo.providers.fixture import FixtureProvider

from .conftest import run_job


class CountingProvider(FixtureProvider):
    """Fixture behaviour, but counts every call that would be paid with a real provider."""

    name = "fixture"  # the storyboard schema only admits known origins

    def __init__(self):
        super().__init__(get_settings())
        self.counts = {"image": 0, "video": 0, "plan": 0, "scene": 0, "tts": 0}

    def analyze_image(self, *a, **k):
        self.counts["image"] += 1
        return super().analyze_image(*a, **k)

    def analyze_video(self, *a, **k):
        self.counts["video"] += 1
        return super().analyze_video(*a, **k)

    def plan_story(self, *a, **k):
        self.counts["plan"] += 1
        return super().plan_story(*a, **k)

    def regenerate_scene(self, *a, **k):
        self.counts["scene"] += 1
        return super().regenerate_scene(*a, **k)

    def synthesize(self, *a, **k):
        self.counts["tts"] += 1
        return super().synthesize(*a, **k)


class FailingTTS(CountingProvider):
    def synthesize(self, *a, **k):
        self.counts["tts"] += 1
        raise AppError("insufficient_credits", "OpenRouter reports insufficient credits.")


class FailingVision(CountingProvider):
    def analyze_image(self, *a, **k):
        raise AppError("invalid_key", "OpenRouter rejected the API key.")


@pytest.fixture
def provider(monkeypatch):
    p = CountingProvider()
    monkeypatch.setattr(pipeline, "get_provider", lambda: p)
    return p


@pytest.mark.render
def test_cached_rerender_does_not_repeat_paid_generation(make_project, provider):
    proj = make_project("software", images=True, recording=True)
    pid = proj["id"]
    j = run_job(pid, "generate_story")
    assert j["status"] == "story_ready", j["error"]
    assert provider.counts["image"] == 3 and provider.counts["video"] == 1 and provider.counts["plan"] == 1

    # Regenerating the story re-plans (explicit user action) but reuses cached analysis.
    j = run_job(pid, "generate_story")
    assert j["status"] == "story_ready"
    assert provider.counts["image"] == 3 and provider.counts["video"] == 1 and provider.counts["plan"] == 2

    rev = get_project(pid)["current_revision"]
    narrated = sum(1 for s in get_storyboard(pid).scenes if s.narration.strip())
    j = run_job(pid, "render", {"quality": "draft", "accept_runtime": True}, revision=rev)
    assert j["status"] == "completed", j["error"]
    assert provider.counts["tts"] == narrated
    assert j["result"]["scene_cache"]["misses"] > 0

    # Resolution change: new pixels, but no repeated AI analysis or narration.
    j = run_job(pid, "render", {"quality": "final", "accept_runtime": True}, revision=rev)
    assert j["status"] == "completed", j["error"]
    assert provider.counts["tts"] == narrated
    assert provider.counts["image"] == 3 and provider.counts["plan"] == 2

    # Same draft again: every scene segment comes from the cache.
    j = run_job(pid, "render", {"quality": "draft", "accept_runtime": True}, revision=rev)
    assert j["result"]["scene_cache"]["misses"] == 0
    assert provider.counts["tts"] == narrated
    assert len(list_exports(pid)) == 3

    # Editing one scene's narration re-synthesises only that scene.
    sb = get_storyboard(pid)
    data = sb.model_dump()
    idx = next(i for i, s in enumerate(sb.scenes) if s.narration.strip())
    data["scenes"][idx]["narration"] = "A different, shorter line."
    save_storyboard(pid, data, base_revision=sb.revision)
    j = run_job(pid, "render", {"quality": "draft", "accept_runtime": True}, revision=sb.revision + 1)
    assert j["status"] == "completed"
    assert provider.counts["tts"] == narrated + 1


@pytest.mark.render
def test_narration_failure_is_an_error_not_silent_output(make_project, monkeypatch):
    prov = FailingTTS()
    monkeypatch.setattr(pipeline, "get_provider", lambda: prov)
    proj = make_project("physical", images=True)
    pid = proj["id"]
    assert run_job(pid, "generate_story")["status"] == "story_ready"
    rev = get_project(pid)["current_revision"]
    j = run_job(pid, "render", {"quality": "draft", "accept_runtime": True}, revision=rev)
    assert j["status"] == "failed"
    assert j["error"]["code"] == "insufficient_credits"
    assert j["error"]["detail"]["narration_failed"] is True
    assert "Continue without narration" in j["error"]["message"]
    assert list_exports(pid) == []  # nothing silent was published

    # Only an explicit choice produces a silent video, and it is labelled as such.
    j = run_job(pid, "render", {"quality": "draft", "without_narration": True}, revision=rev)
    assert j["status"] == "completed", j["error"]
    (exp,) = list_exports(pid)
    assert exp["has_audio"] is False and exp["meta"]["narration"]["mode"] == "none"


def test_provider_failure_during_analysis_preserves_inputs(make_project, monkeypatch):
    prov = FailingVision()
    monkeypatch.setattr(pipeline, "get_provider", lambda: prov)
    proj = make_project("software", images=True)
    j = run_job(proj["id"], "generate_story")
    assert j["status"] == "failed" and j["error"]["code"] == "invalid_key"
    after = get_project(proj["id"])
    assert after["current_revision"] is None
    assert after["details"] == proj["details"] and len(after["assets"]) == 3


def test_fixture_outputs_are_labelled(make_project, provider):
    proj = make_project("physical", images=True)
    run_job(proj["id"], "generate_story")
    sb = get_storyboard(proj["id"])
    assert sb.generated_by == "fixture" and sb.generation_models["story"] == "fixture"
    assert all(s.origin == "fixture" for s in sb.scenes)
    assert any("not AI-generated" in w for w in sb.warnings)
    from demojo.projects import list_revisions

    assert list_revisions(proj["id"])[0]["note"] == "Fixture template generated (not AI)"


def test_story_generation_not_ready_without_workflow(make_project, provider):
    proj = make_project("software", images=True)
    from demojo.projects import update_project

    update_project(proj["id"], details={"workflow_notes": ""})
    j = run_job(proj["id"], "generate_story")
    assert j["status"] == "failed" and j["error"]["code"] == "not_ready"
    assert "workflow" in j["error"]["message"]


def test_scene_regeneration_is_a_proposal_validated_against_revision(make_project, provider):
    proj = make_project("physical", images=True)
    pid = proj["id"]
    run_job(pid, "generate_story")
    sb = get_storyboard(pid)
    target = sb.scenes[1]
    j = run_job(pid, "regenerate_scene", {"scene_id": target.id, "base_revision": sb.revision, "instruction": ""},
                revision=sb.revision)
    assert j["status"] == "story_ready", j["error"]
    assert j["result"]["proposal"]["id"] == target.id  # stable scene id
    assert get_storyboard(pid).revision == sb.revision  # nothing applied until the user accepts
    assert provider.counts["scene"] == 1


@pytest.mark.render
def test_runtime_over_target_requires_explicit_acceptance(make_project, provider):
    proj = make_project("physical", images=True, target=20)
    pid = proj["id"]
    run_job(pid, "generate_story")
    sb = get_storyboard(pid)
    data = sb.model_dump()
    data["scenes"][1]["narration"] = " ".join(["This line is deliberately long so the narration cannot fit."] * 5)
    save_storyboard(pid, data, base_revision=sb.revision)
    rev = sb.revision + 1
    j = run_job(pid, "render", {"quality": "draft"}, revision=rev)
    assert j["status"] == "failed" and j["error"]["code"] == "runtime_over_target"
    assert j["error"]["detail"]["computed_s"] > 20 + 1.5
    tts_before = provider.counts["tts"]
    j = run_job(pid, "render", {"quality": "draft", "accept_runtime": True}, revision=rev)
    assert j["status"] == "completed"
    assert provider.counts["tts"] == tts_before  # accepting reuses the measured narration
    assert not (project_dir(pid) / "tmp").exists() or not any((project_dir(pid) / "tmp").iterdir())
