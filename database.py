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
from datetime import datetime

DB_PATH   = "burglar.db"
CLIPS_DIR = "clips"
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


def _get_distinct_days(cursor):
    """Return list of distinct clip_date values, oldest first."""
    rows = cursor.execute(
        "SELECT DISTINCT clip_date FROM video_clips ORDER BY clip_date ASC"
    ).fetchall()
    return [r["clip_date"] for r in rows]


def _delete_day(cursor, day: str):
    """
    Delete ALL clips (files + DB rows) belonging to `day` (YYYY-MM-DD).
    Also removes the linked motion_event rows.
    """
    rows = cursor.execute(
        "SELECT clip_id, file_path, thumbnail_path, event_id FROM video_clips WHERE clip_date = ?",
        (day,)
    ).fetchall()

    for row in rows:
        # Delete DB rows
        cursor.execute("DELETE FROM video_clips   WHERE clip_id  = ?", (row["clip_id"],))
        cursor.execute("DELETE FROM motion_events WHERE event_id = ?", (row["event_id"],))
        # Delete files from disk
        for path in [row["file_path"], row["thumbnail_path"]]:
            if path and os.path.exists(path):
                os.remove(path)
                print(f"🗑️  Deleted: {path}")

    print(f"📅 Purged all clips for day: {day}")


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


def save_clip_fifo(file_name, file_path, thumbnail_path, duration, size_kb):
    """
    Day-based FIFO insert:
      1. Find how many distinct days are stored.
      2. If today is already one of those days  → just insert (no deletion).
      3. If today is a NEW day AND days == MAX_DAYS
           → delete ALL clips of the OLDEST day
           → then insert new clip.
    Returns the new clip_id.
    """
    conn   = get_connection()
    cursor = conn.cursor()
    now    = datetime.now()
    today  = now.strftime("%Y-%m-%d")     # e.g. "2026-11-16"

    days = _get_distinct_days(cursor)     # oldest → newest

    # Today is a brand-new day AND we're already at the limit
    if today not in days and len(days) >= MAX_DAYS:
        oldest_day = days[0]              # e.g. "2026-10-17"
        print(f"⚠️  Day limit reached ({MAX_DAYS}). Removing oldest day: {oldest_day}")
        _delete_day(cursor, oldest_day)

    # Insert motion event
    cursor.execute(
        "INSERT INTO motion_events (detected_at, status) VALUES (?, 'saved')",
        (now,)
    )
    event_id = cursor.lastrowid

    # Insert clip
    cursor.execute("""
        INSERT INTO video_clips
            (event_id, file_name, file_path, thumbnail_path,
             duration_seconds, file_size_kb, recorded_at, clip_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (event_id, file_name, file_path, thumbnail_path,
          duration, size_kb, now, today))

    conn.commit()
    new_clip_id = cursor.lastrowid
    conn.close()
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
    return [dict(r) for r in rows]


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
    conn   = get_connection()
    cursor = conn.cursor()

    row = cursor.execute(
        "SELECT file_path, thumbnail_path, event_id FROM video_clips WHERE clip_id = ?",
        (clip_id,)
    ).fetchone()

    if row:
        cursor.execute("DELETE FROM video_clips   WHERE clip_id  = ?", (clip_id,))
        cursor.execute("DELETE FROM motion_events WHERE event_id = ?", (row["event_id"],))
        conn.commit()
        for path in [row["file_path"], row["thumbnail_path"]]:
            if path and os.path.exists(path):
                os.remove(path)
    conn.close()
