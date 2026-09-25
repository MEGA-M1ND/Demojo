"""HTTP API: uploads, ownership checks, byte ranges, conflicts, export gating."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from demojo.config import reset_settings_cache


@pytest.fixture
def client():
    from demojo.api import app

    with TestClient(app) as c:
        yield c


def _png(w=800, h=500, color=(30, 120, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def _upload(client, pid, data: bytes, name: str, role="media"):
    return client.post(f"/api/projects/{pid}/assets", params={"filename": name, "role": role}, content=data)


def test_project_crud_and_upload_flow(client, fixtures_dir):
    p = client.post("/api/projects", json={"name": "API test"}).json()
    pid = p["id"]
    r = _upload(client, pid, _png(), "shot one.png")
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["kind"] == "image" and a["classification"] == "screenshot" and a["width"] == 800
    rec = _upload(client, pid, (fixtures_dir / "software/tallyfox_recording.mp4").read_bytes(), "rec.mp4")
    assert rec.status_code == 201, rec.text
    rv = rec.json()
    assert rv["kind"] == "video" and abs(rv["duration_s"] - 14.0) < 0.1 and 4 <= len(rv["frames"]) <= 24
    # Second recording is rejected with an actionable message.
    r2 = _upload(client, pid, (fixtures_dir / "software/tallyfox_recording.mp4").read_bytes(), "rec2.mp4")
    assert r2.status_code == 400 and "one screen recording" in r2.json()["error"]["message"]
    # Rename + reclassify + reorder.
    assert client.patch(f"/api/projects/{pid}/assets/{a['id']}", json={"label": "Dashboard", "classification": "photo"}).json()["label"] == "Dashboard"
    order = client.post(f"/api/projects/{pid}/assets/reorder", json={"ids": [rv["id"], a["id"]]}).json()
    assert [x["id"] for x in order] == [rv["id"], a["id"]]
    proj = client.get(f"/api/projects/{pid}").json()
    assert proj["readiness"]["ready"] is False
    client.patch(f"/api/projects/{pid}", json={"details": {"product_name": "Tallyfox", "description": "Invoicing software for freelancers."}})
    assert client.get(f"/api/projects/{pid}").json()["readiness"]["ready"] is True


def test_media_endpoint_supports_byte_ranges(client, fixtures_dir):
    pid = client.post("/api/projects", json={}).json()["id"]
    rv = _upload(client, pid, (fixtures_dir / "software/tallyfox_recording.mp4").read_bytes(), "rec.mp4").json()
    url = f"/api/projects/{pid}/assets/{rv['id']}/content"
    full = client.get(url)
    assert full.status_code == 200 and full.headers["content-type"] == "video/mp4"
    part = client.get(url, headers={"Range": "bytes=100-199"})
    assert part.status_code == 206
    assert part.headers["content-range"].startswith("bytes 100-199/")
    assert part.content == full.content[100:200]
    assert client.get(f"/api/projects/{pid}/assets/{rv['id']}/frames/0").headers["content-type"] == "image/jpeg"


def test_assets_are_scoped_to_their_project(client):
    a = client.post("/api/projects", json={}).json()["id"]
    b = client.post("/api/projects", json={}).json()["id"]
    img = _upload(client, a, _png(), "a.png").json()
    assert client.get(f"/api/projects/{b}/assets/{img['id']}/content").status_code == 404
    assert client.delete(f"/api/projects/{b}/assets/{img['id']}").status_code == 404
    assert client.get("/api/projects/prj_doesnotexist").status_code == 404
    assert client.get("/api/projects/..%2F..%2Fetc/assets/x/content").status_code == 404


def test_rejects_unsupported_corrupt_and_oversized_uploads(client, monkeypatch):
    pid = client.post("/api/projects", json={}).json()["id"]
    r = _upload(client, pid, b"#!/bin/sh\necho hi\n" * 10, "script.png")
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_upload"
    png = _png()
    r = _upload(client, pid, png[: len(png) // 3], "truncated.png")
    assert r.status_code == 400 and "could not be decoded" in r.json()["error"]["message"]
    fake_mp4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 5000
    r = _upload(client, pid, fake_mp4, "fake.mp4")
    assert r.status_code == 415 and r.json()["error"]["code"] == "unavailable_codec"
    r = _upload(client, pid, b"", "empty.png")
    assert r.status_code == 400
    # Decoded-pixel limit (decompression bomb protection), lowered for the test.
    monkeypatch.setenv("MAX_IMAGE_PIXELS", "1000000")
    reset_settings_cache()
    r = _upload(client, pid, _png(1400, 1000), "big.png")
    assert r.status_code == 400 and "MP" in r.json()["error"]["message"]
    # Declared body larger than the limit is refused before reading it.
    monkeypatch.setenv("MAX_VIDEO_MB", "1")
    monkeypatch.setenv("MAX_IMAGE_MB", "1")
    reset_settings_cache()
    r = client.post(f"/api/projects/{pid}/assets", params={"filename": "x.png"}, content=b"\x89PNG" + b"0" * (2 * 1024 * 1024))
    assert r.status_code == 413


def test_storyboard_conflicts_and_export_gating(client, make_project):
    from tests.conftest import run_job

    p = make_project("physical", images=True, logo=False)
    pid = p["id"]
    assert run_job(pid, "generate_story")["status"] == "story_ready"
    st = client.get(f"/api/projects/{pid}/storyboard").json()
    sb = st["storyboard"]
    assert st["timeline"]["requested_s"] == 30 and st["timeline"]["all_speech_measured"] is False
    # Edit with the right base revision works.
    sb["scenes"][1]["narration"] = "Sleek bottle, now with an inferred 99% leak-proof rating."
    sb["scenes"][1]["claims"].append({"id": "clm_abcd1234", "text": "99% leak-proof rating", "basis": "inferred", "source_ref": None, "status": "ok"})
    r = client.patch(f"/api/projects/{pid}/storyboard", json={"storyboard": sb, "base_revision": sb["revision"]})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["storyboard"]["revision"] == sb["revision"] + 1
    assert new["blockers"] and new["blockers"][0]["code"] == "claim_unconfirmed"
    assert any("narration" in c for c in new["changes_since_ai_draft"]["changes"])
    # A stale edit is refused and does not overwrite newer work.
    r = client.patch(f"/api/projects/{pid}/storyboard", json={"storyboard": sb, "base_revision": sb["revision"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_revision"
    # Export is blocked until the claim is confirmed or removed.
    r = client.post(f"/api/projects/{pid}/renders", json={"quality": "draft"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "export_blocked"
    cur = new["storyboard"]
    for c in cur["scenes"][1]["claims"]:
        if c["status"] == "needs_confirmation":
            c["status"] = "confirmed"
    r = client.patch(f"/api/projects/{pid}/storyboard", json={"storyboard": cur, "base_revision": cur["revision"]})
    assert r.json()["blockers"] == []
    r = client.post(f"/api/projects/{pid}/renders", json={"quality": "draft"})
    assert r.status_code == 202 and r.json()["created"] is True
    r2 = client.post(f"/api/projects/{pid}/renders", json={"quality": "draft"})
    assert r2.json()["created"] is False and r2.json()["job"]["id"] == r.json()["job"]["id"]
    # Cancel the queued job, and revert to the AI draft.
    assert client.post(f"/api/jobs/{r.json()['job']['id']}/cancel").json()["status"] == "canceled"
    rev = client.post(f"/api/projects/{pid}/storyboard/revert", json={"revision": 1, "base_revision": cur["revision"] + 1}).json()
    assert rev["storyboard"]["scenes"][1]["narration"] != cur["scenes"][1]["narration"]


def test_feedback_and_health(client):
    pid = client.post("/api/projects", json={}).json()["id"]
    assert client.post(f"/api/projects/{pid}/feedback", json={"usefulness": 4, "biggest_issue": "voice", "willingness_to_pay": "$15"}).status_code == 201
    assert client.post(f"/api/projects/{pid}/feedback", json={"usefulness": 9}).status_code == 422
    h = client.get("/api/health").json()
    assert h["ok"] is True and h["provider_mode"] == "fixture" and "key" not in str(h).replace("openrouter_key_configured", "")
    cfg = client.get("/api/config").json()
    assert cfg["fixture_mode"] is True and "OPENROUTER_API_KEY" not in str(cfg)
