"""SQLite persistence shared by the API process and the worker process."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    details_json TEXT NOT NULL,
    current_revision INTEGER,
    ai_budget_usd REAL NOT NULL,
    allow_unpriced INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    role TEXT NOT NULL,              -- media | logo
    kind TEXT NOT NULL,              -- image | video | logo
    classification TEXT NOT NULL,    -- screenshot | photo | recording | logo
    label TEXT NOT NULL,
    original_name TEXT NOT NULL,
    position INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mime TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    duration_s REAL,
    fps REAL,
    vcodec TEXT,
    rotation INTEGER,
    has_audio INTEGER NOT NULL DEFAULT 0,
    has_alpha INTEGER NOT NULL DEFAULT 0,
    original_path TEXT NOT NULL,
    master_path TEXT,
    thumb_path TEXT,
    analysis_path TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS assets_project ON assets(project_id, position);

CREATE TABLE IF NOT EXISTS asset_frames (
    asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    t_s REAL NOT NULL,
    path TEXT NOT NULL,
    PRIMARY KEY (asset_id, idx)
);

CREATE TABLE IF NOT EXISTS analyses (
    cache_key TEXT PRIMARY KEY,
    asset_sha TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    provider TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS storyboard_revisions (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    json TEXT NOT NULL,
    source TEXT NOT NULL,            -- openrouter | fixture | user | revert
    note TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, revision)
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,              -- generate_story | regenerate_scene | narrate | render
    status TEXT NOT NULL,
    stage TEXT,
    stage_index INTEGER,
    stage_count INTEGER,
    params_json TEXT NOT NULL,
    revision INTEGER,
    result_json TEXT,
    error_code TEXT,
    error_message TEXT,
    error_detail TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    worker_id TEXT,
    heartbeat_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    dedupe_key TEXT,
    retry_of TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS jobs_project ON jobs(project_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_active_dedupe ON jobs(dedupe_key)
    WHERE status IN ('queued','analyzing','planning','synthesizing','rendering','validating');

CREATE TABLE IF NOT EXISTS exports (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    quality TEXT NOT NULL,
    dir TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    duration_s REAL NOT NULL,
    size_bytes INTEGER NOT NULL,
    has_audio INTEGER NOT NULL,
    meta_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS speech_cache (
    key TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    duration_s REAL NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    voice TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_calls (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    job_id TEXT,
    purpose TEXT NOT NULL,           -- analyze_image | analyze_video | plan | repair | scene | tts
    model TEXT NOT NULL,
    status TEXT NOT NULL,            -- started | succeeded | failed | ambiguous
    estimated_usd REAL,
    actual_usd REAL,
    cost_source TEXT,                -- provider_reported | estimate | unknown
    generation_id TEXT,
    error_code TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    chars INTEGER,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS calls_project ON provider_calls(project_id);

CREATE TABLE IF NOT EXISTS feedback (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    usefulness INTEGER NOT NULL,
    biggest_issue TEXT NOT NULL,
    willingness_to_pay TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pricing_cache (
    key TEXT PRIMARY KEY,
    json TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
"""

ACTIVE_STATUSES = ("queued", "analyzing", "planning", "synthesizing", "rendering", "validating")
RUNNING_STATUSES = ("analyzing", "planning", "synthesizing", "rendering", "validating")
TERMINAL_STATUSES = ("story_ready", "completed", "failed", "canceled", "interrupted")


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def now_ts() -> float:
    return time.time()


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


_initialised: set[str] = set()


def init_db(path: Path | None = None) -> None:
    s = get_settings()
    path = path or s.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')")
    finally:
        conn.close()
    _initialised.add(str(path))


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    s = get_settings()
    if str(s.db_path) not in _initialised:
        init_db(s.db_path)
    conn = _connect(s.db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def row_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def loads(s: str | None, default=None):
    if s is None:
        return default
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return default


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
