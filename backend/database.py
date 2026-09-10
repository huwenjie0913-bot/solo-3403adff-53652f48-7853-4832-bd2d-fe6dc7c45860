"""SQLite 连接与表结构。所有业务数据均保存在本地文件 data/app.db。"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "app.db"


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS project (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    vessel      TEXT DEFAULT '',
    note        TEXT DEFAULT '',
    mm_per_px   REAL DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS fragment (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    code         TEXT NOT NULL,
    front_path   TEXT, back_path TEXT,
    crop         TEXT DEFAULT '',
    scale_mm     REAL DEFAULT 0,
    scale_px     REAL DEFAULT 0,
    mm_per_px    REAL DEFAULT 0,
    contour      TEXT DEFAULT '',
    mask_path    TEXT,
    thumb_path   TEXT,
    thickness    REAL DEFAULT 0,
    thickness_note TEXT DEFAULT '',
    color_bands  TEXT DEFAULT '',
    features     TEXT DEFAULT '',
    created_at   TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS plan (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    is_active   INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS plan_state (
    plan_id   INTEGER PRIMARY KEY REFERENCES plan(id) ON DELETE CASCADE,
    layout    TEXT DEFAULT '{}',
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS candidate (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    frag_a      INTEGER NOT NULL REFERENCES fragment(id) ON DELETE CASCADE,
    frag_b      INTEGER NOT NULL REFERENCES fragment(id) ON DELETE CASCADE,
    score       REAL NOT NULL,
    reasons     TEXT DEFAULT '[]',
    metrics     TEXT DEFAULT '{}',
    params      TEXT DEFAULT '{}',
    edge_a      TEXT DEFAULT '',
    edge_b      TEXT DEFAULT '',
    overlap     REAL DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS decision (
    plan_id     INTEGER NOT NULL REFERENCES plan(id) ON DELETE CASCADE,
    candidate_id INTEGER NOT NULL REFERENCES candidate(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'pending',
    note        TEXT DEFAULT '',
    updated_at  TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (plan_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS review (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id      INTEGER NOT NULL REFERENCES plan(id) ON DELETE CASCADE,
    candidate_id INTEGER NOT NULL REFERENCES candidate(id) ON DELETE CASCADE,
    status       TEXT NOT NULL DEFAULT 'pending',   -- accepted / pending / excluded
    note         TEXT DEFAULT '',
    adjustments  TEXT DEFAULT '{}',   -- 锚点、排除区段等人工作业内容
    result       TEXT DEFAULT '{}',   -- 服务端重算的指标与修正变换
    auto_snapshot TEXT DEFAULT '{}',  -- 复核开始时自动结果快照
    created_at   TEXT DEFAULT (datetime('now','localtime')),
    updated_at   TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE (plan_id, candidate_id)
);
"""


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"数据库已初始化: {DB_PATH}")
