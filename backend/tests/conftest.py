from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Every test gets its own data dir, fixture provider mode, and no API key."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEMOJO_PROVIDER_MODE", "fixture")
    monkeypatch.setenv("DEMOJO_ENV_FILE", "/dev/null")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from demojo import db
    from demojo.config import reset_settings_cache

    reset_settings_cache()
    db._initialised.clear()
    db.init_db()
    yield tmp_path / "data"
    reset_settings_cache()


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    if not (FIXTURES / "manifest.json").exists():
        from demojo.fixtures_gen import generate

        generate(FIXTURES)
    return FIXTURES


@pytest.fixture(scope="session")
def manifest(fixtures_dir) -> dict:
    return json.loads((fixtures_dir / "manifest.json").read_text())


def add_fixture_asset(pid: str, src: Path, tmp_path: Path, role: str = "media") -> dict:
    from demojo.projects import add_asset

    tmp = tmp_path / f"upload_{src.name}"
    shutil.copy(src, tmp)
    return add_asset(pid, tmp, src.name, role)  # type: ignore[arg-type]


@pytest.fixture
def make_project(fixtures_dir, manifest, tmp_path):
    """Create a project from the synthetic fixtures. kind: 'software' | 'physical'."""

    def _make(kind: str = "software", images: bool = True, recording: bool = False, aspect: str = "16:9",
              target: int = 30, narration: bool = True, logo: bool = True) -> dict:
        from demojo.projects import create_project, get_project, update_project

        m = manifest[kind]
        p = create_project(f"test {kind}")
        pid = p["id"]
        if images:
            for rel in m["images"]:
                add_fixture_asset(pid, fixtures_dir / rel, tmp_path)
        if recording and m["recording"]:
            add_fixture_asset(pid, fixtures_dir / m["recording"], tmp_path)
        details = {k: m["brief"][k] for k in ("product_name", "description", "audience", "selling_points", "cta_text",
                                              "website_text", "product_type", "workflow_notes", "accent_color")}
        details.update(aspect_ratio=aspect, target_duration_s=target, narration=narration)
        if logo:
            lg = add_fixture_asset(pid, fixtures_dir / m["logo"], tmp_path, role="logo")
            details["logo_asset_id"] = lg["id"]
        update_project(pid, details=details)
        return get_project(pid)

    return _make


def run_job(pid: str, kind: str, params: dict | None = None, revision: int | None = None) -> dict:
    """Enqueue, claim, and execute one job synchronously (as the worker would)."""
    from demojo import jobs, pipeline

    job, _ = jobs.enqueue(pid, kind, params or {}, revision=revision)
    claimed = jobs.claim("test-worker")
    assert claimed is not None and claimed["id"] == job["id"]
    pipeline.execute(claimed)
    return jobs.get_job(job["id"])
