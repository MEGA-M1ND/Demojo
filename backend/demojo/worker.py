"""Single-concurrency job worker (separate process).

    python -m demojo.worker

Claims one job at a time from SQLite, keeps a heartbeat while it runs, and on
startup marks jobs left running by a previous process as interrupted (paid
calls whose outcome is unknown are marked ambiguous and never auto-retried).
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import signal
import threading
import time

from . import db, jobs
from .config import get_settings
from .costs import mark_ambiguous_calls
from .pipeline import execute

log = logging.getLogger("demojo.worker")


def _heartbeat_loop(job_id: str, done: threading.Event) -> None:
    while not done.wait(5.0):
        try:
            jobs.heartbeat(job_id)
        except Exception:  # pragma: no cover
            log.exception("heartbeat failed")


def recover() -> None:
    ids = jobs.recover_interrupted()
    n = mark_ambiguous_calls()
    s = get_settings()
    for pdir in (s.data_dir / "projects").glob("prj_*/tmp"):
        shutil.rmtree(pdir, ignore_errors=True)
    if ids or n:
        log.warning("recovered %d interrupted job(s); %d paid call(s) marked ambiguous", len(ids), n)


def run_one(worker_id: str, stop: threading.Event) -> bool:
    job = jobs.claim(worker_id)
    if job is None:
        return False
    log.info("claimed %s (%s) for %s", job["id"], job["kind"], job["project_id"])
    done = threading.Event()
    hb = threading.Thread(target=_heartbeat_loop, args=(job["id"], done), daemon=True)
    hb.start()
    try:
        execute(job, stop)
    finally:
        done.set()
        hb.join(timeout=2)
    log.info("finished %s -> %s", job["id"], jobs.get_job(job["id"])["status"] if _exists(job["id"]) else "deleted")
    return True


def _exists(jid: str) -> bool:
    with db.connect() as conn:
        return conn.execute("SELECT 1 FROM jobs WHERE id = ?", (jid,)).fetchone() is not None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = get_settings()
    db.init_db()
    worker_id = f"wkr_{os.getpid()}_{secrets.token_hex(3)}"
    stop = threading.Event()

    def _stop(signum, frame):  # noqa: ARG001
        log.info("signal %s: finishing up", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    recover()
    log.info("worker %s ready (provider mode: %s, data: %s)", worker_id, s.provider_mode, s.data_dir)
    while not stop.is_set():
        try:
            if not run_one(worker_id, stop):
                stop.wait(1.0)
        except Exception:  # pragma: no cover
            log.exception("worker loop error")
            time.sleep(2)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
