"""Job queue guarantees: atomic claim, dedupe, cancel, interruption, no partial publishes."""

from __future__ import annotations

import threading
import time

import pytest

from demojo import db, jobs, pipeline, worker
from demojo.errors import AppError
from demojo.projects import get_project, list_exports, project_dir

from .conftest import run_job


def test_atomic_claim_and_dedupe(make_project):
    p = make_project("physical", images=True, logo=False)
    a, created = jobs.enqueue(p["id"], "render", {"quality": "draft"}, dedupe_key=f"{p['id']}:render")
    b, created2 = jobs.enqueue(p["id"], "render", {"quality": "draft"}, dedupe_key=f"{p['id']}:render")
    assert created and not created2 and a["id"] == b["id"]  # repeated clicks don't queue twice
    results: list = []
    threads = [threading.Thread(target=lambda: results.append(jobs.claim("w"))) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    claimed = [r for r in results if r]
    assert len(claimed) == 1 and claimed[0]["id"] == a["id"]


def test_cancel_queued_job_is_never_claimed(make_project):
    p = make_project("physical", images=True, logo=False)
    j, _ = jobs.enqueue(p["id"], "generate_story", {})
    assert jobs.request_cancel(j["id"])["status"] == "canceled"
    assert jobs.claim("w") is None


def test_finish_ok_refuses_after_cancel_request(make_project):
    p = make_project("physical", images=True, logo=False)
    j, _ = jobs.enqueue(p["id"], "narrate", {})
    jobs.claim("w")
    jobs.request_cancel(j["id"])
    assert jobs.finish_ok(j["id"], "completed", {"export_id": "exp_x"}) is False
    assert jobs.get_job(j["id"])["status"] != "completed"


@pytest.mark.render
def test_cancel_during_render_publishes_nothing(make_project):
    p = make_project("software", images=True, recording=True)
    pid = p["id"]
    assert run_job(pid, "generate_story")["status"] == "story_ready"
    job, _ = jobs.enqueue(pid, "render", {"quality": "final", "accept_runtime": True}, revision=get_project(pid)["current_revision"])
    claimed = jobs.claim("w")
    t = threading.Thread(target=pipeline.execute, args=(claimed,))
    t.start()
    deadline = time.time() + 120
    while time.time() < deadline and jobs.get_job(job["id"])["status"] != "rendering":
        time.sleep(0.05)
    assert jobs.get_job(job["id"])["status"] == "rendering"
    time.sleep(0.5)
    jobs.request_cancel(job["id"])
    t.join(timeout=120)
    final = jobs.get_job(job["id"])
    assert final["status"] == "canceled"
    assert list_exports(pid) == []
    assert not (project_dir(pid) / "exports").exists() or not any((project_dir(pid) / "exports").iterdir())
    assert not (project_dir(pid) / "tmp" / job["id"]).exists()


def test_interrupted_job_with_ambiguous_paid_call_needs_confirmation(make_project):
    p = make_project("physical", images=True, logo=False)
    pid = p["id"]
    j, _ = jobs.enqueue(pid, "generate_story", {}, dedupe_key=f"{pid}:story")
    jobs.claim("dead-worker")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO provider_calls(id, project_id, job_id, purpose, model, status, estimated_usd, cost_source, created_at)"
            " VALUES ('call_1', ?, ?, 'plan', 'm', 'started', 0.01, 'estimate', ?)",
            (pid, j["id"], db.now_iso()),
        )
    worker.recover()  # what a restarted worker does first
    after = jobs.get_job(j["id"])
    assert after["status"] == "interrupted"
    assert after["error"]["ambiguous"] is True
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM provider_calls WHERE id = 'call_1'").fetchone()[0] == "ambiguous"
    with pytest.raises(AppError) as e:
        jobs.retry(j["id"], pid)
    assert e.value.code == "ambiguous_paid_request"
    new = jobs.retry(j["id"], pid, confirm_duplicate_charge=True)
    assert new["status"] == "queued" and new["retry_of"] == j["id"]


def test_stale_heartbeat_is_reported_as_interrupted(make_project):
    p = make_project("physical", images=True, logo=False)
    j, _ = jobs.enqueue(p["id"], "narrate", {})
    jobs.claim("w")
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET heartbeat_at = ? WHERE id = ?", (time.time() - 600, j["id"]))
    got = jobs.get_job(j["id"])
    assert got["status"] == "interrupted" and not got["active"]


def test_project_deletion_cancels_work_and_isolates_media(make_project):
    from demojo.projects import delete_project
    from demojo.projects import get_project as gp

    a = make_project("software", images=True, logo=False)
    b = make_project("physical", images=True, logo=False)
    j, _ = jobs.enqueue(a["id"], "generate_story", {})
    a_dir = project_dir(a["id"])
    assert a_dir.exists()
    delete_project(a["id"], wait_s=0.1)
    assert not a_dir.exists()
    with pytest.raises(AppError):
        gp(a["id"])
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE project_id = ?", (a["id"],)).fetchone()[0] == 0
    kept = gp(b["id"])
    assert len(kept["assets"]) == 3 and project_dir(b["id"]).exists()
