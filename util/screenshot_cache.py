"""Screenshot cache helpers for chart screenshots.

Used by today_advisor.py (warmup) and agents/analysts/vision.py (consumption).
Placed in util/ to avoid circular imports between today_advisor and vision agent.
"""

import os
import sqlite3
import uuid
from datetime import datetime
from typing import Dict, Optional

from database.cs2_sqlite_setup import CS2_DB_PATH

WARMUP_TTL_SECONDS = 5 * 60 * 60  # 5 小时


def _conn():
    conn = sqlite3.connect(CS2_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def read_cache_row(item_name: str) -> Optional[Dict]:
    """Return the cs2_screenshot_cache row for an item, or None."""
    with _conn() as conn:
        r = conn.execute(
            "SELECT * FROM cs2_screenshot_cache WHERE item_name = ?",
            (item_name,),
        ).fetchone()
        return dict(r) if r else None


def is_cache_fresh(row: Optional[Dict]) -> bool:
    """缓存命中条件:
        - status == 'done'
        - updated_at 在 TTL 内
        - kline & line 文件都还在磁盘上
    """
    if not row or row.get("status") != "done":
        return False
    updated = row.get("updated_at")
    if not updated:
        return False
    try:
        # SQLite 可能返回 'YYYY-MM-DD HH:MM:SS'
        dt = datetime.fromisoformat(str(updated).replace("Z", "").split(".")[0])
    except ValueError:
        return False
    if (datetime.now() - dt).total_seconds() > WARMUP_TTL_SECONDS:
        return False
    for path_key in ("kline_path", "line_path"):
        p = row.get(path_key)
        if not p or not os.path.exists(p):
            return False
    return True


def get_cached_paths(item_name: str) -> Optional[Dict[str, str]]:
    """Return {'kline': ..., 'line': ...} if fresh, else None."""
    row = read_cache_row(item_name)
    if not is_cache_fresh(row):
        return None
    return {
        "kline": row["kline_path"],
        "line": row["line_path"],
    }


def upsert_cache_row(
    item_name: str,
    status: str,
    kline_path: Optional[str] = None,
    line_path: Optional[str] = None,
    error_msg: Optional[str] = None,
) -> None:
    """Insert or update a cache row. New inserts get a fresh UUID."""
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as conn:
        existing = conn.execute(
            "SELECT id FROM cs2_screenshot_cache WHERE item_name = ?",
            (item_name,),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE cs2_screenshot_cache
                SET status=?, kline_path=COALESCE(?, kline_path),
                    line_path=COALESCE(?, line_path),
                    error_msg=?, updated_at=?
                WHERE item_name=?
                """,
                (status, kline_path, line_path, error_msg, now, item_name),
            )
        else:
            conn.execute(
                """
                INSERT INTO cs2_screenshot_cache
                    (id, item_name, kline_path, line_path, status, error_msg,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    item_name,
                    kline_path,
                    line_path,
                    status,
                    error_msg,
                    now,
                    now,
                ),
            )
        conn.commit()
