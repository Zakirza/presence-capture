"""
database.py — SQLite setup + Day-based FIFO (max 30 days of clips)

FIFO Rule:
  - Clips are grouped by calendar day (DATE of recorded_at).
  - Up to 30 distinct days are kept.
  - When a clip arrives on a NEW day (day 31), ALL clips from the
    OLDEST day are deleted first (files + DB rows), then the new clip
    is saved.
  - Multiple clips on the same day are all kept — no clip is deleted
    mid-day.
"""

import os
import sqlite3
from contextlib import closing
from datetime import datetime

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DB_PATH   = os.path.join(BASE_DIR, "burglar.db")
CLIPS_DIR = os.path.join(BASE_DIR, "clips")
MAX_DAYS  = 30          # ← only this number controls retention


def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist, and migrate old schemas."""
    os.makedirs(CLIPS_DIR, exist_ok=True)
    conn = get_connection()
    cursor = conn.cursor()

    # Create tables (won't touch them if they already exist)
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS motion_events (
            event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at DATETIME NOT NULL,
            ended_at    DATETIME,
            status      TEXT DEFAULT 'saved'
        );

        CREATE TABLE IF NOT EXISTS pending_file_deletions (
            path TEXT PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS video_clips (
            clip_id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id         INTEGER NOT NULL,
            file_name        TEXT NOT NULL,
            file_path        TEXT NOT NULL,
            thumbnail_path   TEXT,
            duration_seconds INTEGER DEFAULT 0,
            file_size_kb     INTEGER DEFAULT 0,
            recorded_at      DATETIME NOT NULL,
            clip_date        DATE NOT NULL DEFAULT '1970-01-01',
            FOREIGN KEY (event_id) REFERENCES motion_events(event_id)
        );
    """)

    # ── Migration: add clip_date column if missing (old DB) ──────────────
    existing_cols = [
        row[1] for row in cursor.execute("PRAGMA table_info(video_clips)").fetchall()
    ]
    if "clip_date" not in existing_cols:
        cursor.execute("ALTER TABLE video_clips ADD COLUMN clip_date DATE NOT NULL DEFAULT '1970-01-01'")
        # Backfill from recorded_at so existing rows get the right date
        cursor.execute("""
            UPDATE video_clips
            SET clip_date = DATE(recorded_at)
            WHERE clip_date = '1970-01-01'
        """)
        print("✅ Migration done: clip_date column added and backfilled.")

    conn.commit()
    conn.close()
    _retry_file_deletions()


def _get_distinct_days(cursor):
    """Return list of distinct clip_date values, oldest first."""
    rows = cursor.execute(
        "SELECT DISTINCT clip_date FROM video_clips ORDER BY clip_date ASC"
    ).fetchall()
    return [r["clip_date"] for r in rows]


def _delete_day(cursor, day: str):
    """Retire a day and queue its files in the same transaction."""
    rows = cursor.execute(
        "SELECT clip_id, file_path, thumbnail_path, event_id FROM video_clips WHERE clip_date = ?",
        (day,)
    ).fetchall()
    paths = set()
    for row in rows:
        cursor.execute("DELETE FROM video_clips WHERE clip_id = ?", (row["clip_id"],))
        cursor.execute("DELETE FROM motion_events WHERE event_id = ?", (row["event_id"],))
        paths.update(
            os.path.normpath(os.path.join(BASE_DIR, path))
            for path in (row["file_path"], row["thumbnail_path"])
            if path
        )
    cursor.executemany("INSERT OR IGNORE INTO pending_file_deletions VALUES (?)",
                       ((path,) for path in paths))


def _retry_file_deletions():
    with closing(get_connection()) as conn, conn:
        paths = conn.execute("SELECT path FROM pending_file_deletions").fetchall()
        for row in paths:
            path = row["path"]
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError:
                continue  # Keep locked files queued across saves and restarts.
            conn.execute("DELETE FROM pending_file_deletions WHERE path = ?", (path,))


def get_clip_count():
    """Total clips currently stored."""
    conn  = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM video_clips").fetchone()[0]
    conn.close()
    return count


def get_day_count():
    """Number of distinct days currently stored."""
    conn  = get_connection()
    count = conn.execute(
        "SELECT COUNT(DISTINCT clip_date) FROM video_clips"
    ).fetchone()[0]
    conn.close()
    return count


def save_clip_fifo(file_name, file_path, thumbnail_path, duration, size_kb, recorded_at=None):
    """Save a clip and prune the oldest stored date when the archive is full."""
    recorded_at = recorded_at or datetime.now()
    clip_date = recorded_at.date().isoformat()

    with closing(get_connection()) as conn, conn:
        # Serialize the date check with the insert and pruning.
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        other_days = [day for day in _get_distinct_days(cursor) if day != clip_date]
        excess = max(0, len(other_days) + 1 - MAX_DAYS)
        for day in other_days[:excess]:
            _delete_day(cursor, day)

        cursor.execute(
            "INSERT INTO motion_events (detected_at, status) VALUES (?, 'saved')",
            (recorded_at,)
        )
        cursor.execute("""
            INSERT INTO video_clips
                (event_id, file_name, file_path, thumbnail_path,
                 duration_seconds, file_size_kb, recorded_at, clip_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            cursor.lastrowid, file_name, os.path.abspath(file_path),
            os.path.abspath(thumbnail_path) if thumbnail_path else "",
            duration, size_kb, recorded_at, clip_date,
        ))
        new_clip_id = cursor.lastrowid

    # A failed insert or commit leaves all existing footage on disk.
    _retry_file_deletions()

    return new_clip_id


def get_all_clips():
    """Return all clips ordered newest first."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT vc.clip_id, vc.file_name, vc.file_path, vc.thumbnail_path,
               vc.duration_seconds, vc.file_size_kb, vc.recorded_at,
               vc.clip_date, me.detected_at
        FROM video_clips vc
        JOIN motion_events me ON vc.event_id = me.event_id
        ORDER BY vc.recorded_at DESC
    """).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        if d.get("file_path") and not os.path.isabs(d["file_path"]):
            d["file_path"] = os.path.normpath(os.path.join(BASE_DIR, d["file_path"]))
        if d.get("thumbnail_path") and not os.path.isabs(d["thumbnail_path"]):
            d["thumbnail_path"] = os.path.normpath(os.path.join(BASE_DIR, d["thumbnail_path"]))
        result.append(d)
    return result


def get_clips_by_day():
    """Return clips grouped by day  {date_str: [clip, ...]} newest day first."""
    clips = get_all_clips()
    grouped = {}
    for clip in clips:
        d = clip["clip_date"]
        grouped.setdefault(d, []).append(clip)
    # sort days newest first
    return dict(sorted(grouped.items(), reverse=True))


def delete_clip(clip_id):
    """Manually delete a single clip."""
    with closing(get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT file_path, thumbnail_path, event_id FROM video_clips WHERE clip_id = ?",
            (clip_id,),
        ).fetchone()
        if row is None:
            return

        # Keep the index entry until both files are gone so deletion can be retried.
        for path in (row["thumbnail_path"], row["file_path"]):
            if path:
                try:
                    os.remove(os.path.join(BASE_DIR, path))
                except FileNotFoundError:
                    pass
        conn.execute("DELETE FROM video_clips WHERE clip_id = ?", (clip_id,))
        conn.execute("DELETE FROM motion_events WHERE event_id = ?", (row["event_id"],))
