"""Persistent job queue in SQLite (single-concurrency worker).

Job status is separate from project/storyboard state. Statuses:
queued → analyzing | planning | synthesizing → rendering → validating →
completed | story_ready | failed | canceled | interrupted.
"""

from __future__ import annotations

import sqlite3

from . import db
from .costs import job_has_ambiguous_calls
from .errors import ERROR_CODES, AppError, not_found
from .projects import new_id

HEARTBEAT_STALE_S = 45
FIRST_STATUS = {"generate_story": "analyzing", "regenerate_scene": "planning", "narrate": "synthesizing", "render": "synthesizing"}
KINDS = tuple(FIRST_STATUS)


def job_public(row: sqlite3.Row | dict) -> dict:
    r = dict(row)
    err = None
    if r.get("error_code"):
        detail = db.loads(r.get("error_detail"), None)
        err = {
            "code": r["error_code"],
            "title": ERROR_CODES.get(r["error_code"], ERROR_CODES["internal"])[1],
            "message": r.get("error_message") or "",
            "detail": detail,
            "retryable": r["status"] in ("failed", "interrupted", "canceled"),
            "ambiguous": bool((detail or {}).get("ambiguous")) if isinstance(detail, dict) else False,
        }
    hb = r.get("heartbeat_at")
    return {
        "id": r["id"],
        "project_id": r["project_id"],
        "kind": r["kind"],
        "status": r["status"],
        "active": r["status"] in db.ACTIVE_STATUSES,
        "stage": r.get("stage"),
        "stage_index": r.get("stage_index"),
        "stage_count": r.get("stage_count"),
        "params": db.loads(r.get("params_json"), {}),
        "revision": r.get("revision"),
        "result": db.loads(r.get("result_json"), None),
        "error": err,
        "cancel_requested": bool(r.get("cancel_requested")),
        "attempts": r.get("attempts"),
        "retry_of": r.get("retry_of"),
        "created_at": r["created_at"],
        "started_at": r.get("started_at"),
        "finished_at": r.get("finished_at"),
        "heartbeat_age_s": round(db.now_ts() - hb, 1) if hb else None,
    }


def enqueue(pid: str, kind: str, params: dict, *, revision: int | None = None, dedupe_key: str | None = None,
            retry_of: str | None = None) -> tuple[dict, bool]:
    if kind not in KINDS:
        raise AppError("conflict", f"Unknown job kind {kind}.")
    jid = new_id("job")
    with db.connect() as conn:
        try:
            conn.execute(
                "INSERT INTO jobs(id, project_id, kind, status, stage, params_json, revision, dedupe_key, retry_of, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (jid, pid, kind, "queued", "Waiting for the worker", db.dumps(params), revision, dedupe_key, retry_of, db.now_iso()),
            )
        except sqlite3.IntegrityError:
            row = conn.execute(
                f"SELECT * FROM jobs WHERE dedupe_key = ? AND status IN ({','.join('?' * len(db.ACTIVE_STATUSES))})",
                (dedupe_key, *db.ACTIVE_STATUSES),
            ).fetchone()
            if row is None:
                raise
            return job_public(row), False
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    return job_public(row), True


def _mark_stale(conn: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row:
    """If a running job's heartbeat is stale (worker died), report it as interrupted."""
    if row["status"] in db.RUNNING_STATUSES and row["heartbeat_at"] and db.now_ts() - row["heartbeat_at"] > HEARTBEAT_STALE_S:
        _interrupt(conn, row["id"], "The worker stopped responding while this job was running.")
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
    return row


def _interrupt(conn: sqlite3.Connection, jid: str, message: str) -> None:
    conn.execute(
        "UPDATE provider_calls SET status = 'ambiguous', error_code = 'interrupted', finished_at = ? WHERE job_id = ? AND status = 'started'",
        (db.now_iso(), jid),
    )
    ambiguous = conn.execute("SELECT 1 FROM provider_calls WHERE job_id = ? AND status = 'ambiguous' LIMIT 1", (jid,)).fetchone()
    detail = {"ambiguous": bool(ambiguous)}
    msg = message + (" A paid AI request may have completed before the interruption; retrying could be charged again." if ambiguous else "")
    conn.execute(
        f"UPDATE jobs SET status = 'interrupted', error_code = 'interrupted', error_message = ?, error_detail = ?, finished_at = ?"
        f" WHERE id = ? AND status IN ({','.join('?' * len(db.RUNNING_STATUSES))})",
        (msg, db.dumps(detail), db.now_iso(), jid, *db.RUNNING_STATUSES),
    )


def get_job(jid: str, pid: str | None = None) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
        if row is None or (pid is not None and row["project_id"] != pid):
            raise not_found("Job")
        row = _mark_stale(conn, row)
    return job_public(row)


def list_jobs(pid: str, limit: int = 20) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM jobs WHERE project_id = ? ORDER BY created_at DESC LIMIT ?", (pid, limit)).fetchall()
        rows = [_mark_stale(conn, r) for r in rows]
    return [job_public(r) for r in rows]


def request_cancel(jid: str) -> dict:
    with db.connect() as conn:
        with db.transaction(conn):
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
            if row is None:
                raise not_found("Job")
            if row["status"] == "queued":
                conn.execute(
                    "UPDATE jobs SET status = 'canceled', cancel_requested = 1, error_code = 'canceled',"
                    " error_message = 'Canceled before it started.', finished_at = ? WHERE id = ?",
                    (db.now_iso(), jid),
                )
            elif row["status"] in db.RUNNING_STATUSES:
                conn.execute("UPDATE jobs SET cancel_requested = 1, stage = 'Canceling…' WHERE id = ?", (jid,))
    return get_job(jid)


def claim(worker_id: str) -> dict | None:
    """Atomically move the oldest queued job to its first running status."""
    with db.connect() as conn:
        row = conn.execute(
            """UPDATE jobs SET
                   status = CASE kind WHEN 'generate_story' THEN 'analyzing' WHEN 'regenerate_scene' THEN 'planning'
                                      ELSE 'synthesizing' END,
                   stage = 'Starting', worker_id = ?, heartbeat_at = ?, started_at = ?, attempts = attempts + 1
               WHERE id = (SELECT id FROM jobs WHERE status = 'queued' AND cancel_requested = 0
                           ORDER BY created_at, rowid LIMIT 1)
                 AND status = 'queued'
               RETURNING *""",
            (worker_id, db.now_ts(), db.now_iso()),
        ).fetchone()
    return dict(row) if row else None


def heartbeat(jid: str) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET heartbeat_at = ? WHERE id = ?", (db.now_ts(), jid))


def set_stage(jid: str, status: str | None, stage: str, index: int | None = None, count: int | None = None) -> None:
    with db.connect() as conn:
        if status:
            conn.execute(
                f"UPDATE jobs SET status = ?, stage = ?, stage_index = ?, stage_count = ?, heartbeat_at = ?"
                f" WHERE id = ? AND status IN ({','.join('?' * len(db.RUNNING_STATUSES))})",
                (status, stage, index, count, db.now_ts(), jid, *db.RUNNING_STATUSES),
            )
        else:
            conn.execute("UPDATE jobs SET stage = ?, stage_index = ?, stage_count = ?, heartbeat_at = ? WHERE id = ?",
                         (stage, index, count, db.now_ts(), jid))


def cancel_requested(jid: str) -> bool:
    with db.connect() as conn:
        row = conn.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (jid,)).fetchone()
    return row is None or bool(row["cancel_requested"])


def finish_ok(jid: str, status: str, result: dict, conn: sqlite3.Connection | None = None) -> bool:
    """Mark success unless cancellation was requested. Returns False if the job was canceled instead."""
    def _do(c: sqlite3.Connection) -> bool:
        cur = c.execute(
            f"UPDATE jobs SET status = ?, stage = 'Done', result_json = ?, finished_at = ?"
            f" WHERE id = ? AND cancel_requested = 0 AND status IN ({','.join('?' * len(db.RUNNING_STATUSES))})",
            (status, db.dumps(result), db.now_iso(), jid, *db.RUNNING_STATUSES),
        )
        return cur.rowcount == 1

    if conn is not None:
        return _do(conn)
    with db.connect() as c:
        return _do(c)


def finish_error(jid: str, status: str, err: AppError) -> None:
    detail = err.detail if isinstance(err.detail, dict) else ({"info": err.detail} if err.detail is not None else {})
    detail = dict(detail)
    detail["ambiguous"] = bool(err.ambiguous or job_has_ambiguous_calls(jid))
    with db.connect() as conn:
        conn.execute(
            f"UPDATE jobs SET status = ?, stage = ?, error_code = ?, error_message = ?, error_detail = ?, finished_at = ?"
            f" WHERE id = ? AND status IN ({','.join('?' * len(db.ACTIVE_STATUSES))})",
            (status, "Canceled" if status == "canceled" else ("Interrupted" if status == "interrupted" else "Failed"),
             err.code, err.message, db.dumps(detail), db.now_iso(), jid, *db.ACTIVE_STATUSES),
        )


def recover_interrupted() -> list[str]:
    """Called on worker start: running jobs from a previous process are interrupted."""
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT id FROM jobs WHERE status IN ({','.join('?' * len(db.RUNNING_STATUSES))})", db.RUNNING_STATUSES
        ).fetchall()
        ids = [r["id"] for r in rows]
        for jid in ids:
            _interrupt(conn, jid, "The worker restarted while this job was running.")
    return ids


def retry(jid: str, pid: str, *, confirm_duplicate_charge: bool = False) -> dict:
    old = get_job(jid, pid)
    if old["active"]:
        raise AppError("conflict", "This job is still running.")
    if old["status"] in ("completed", "story_ready"):
        raise AppError("conflict", "This job already finished successfully.")
    if job_has_ambiguous_calls(jid) and not confirm_duplicate_charge:
        raise AppError(
            "ambiguous_paid_request",
            "A paid AI request from this job may already have completed (the outcome is unknown). Completed results are "
            "cached and reused, but retrying may repeat the unknown request and be charged again. Confirm to retry.",
            detail={"job_id": jid},
        )
    params = old["params"]
    dedupe = {"generate_story": f"{pid}:story", "render": f"{pid}:render", "narrate": f"{pid}:narrate"}.get(
        old["kind"], f"{pid}:scene:{params.get('scene_id')}")
    job, _ = enqueue(pid, old["kind"], params, revision=old["revision"], dedupe_key=dedupe, retry_of=jid)
    return job
