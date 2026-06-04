from __future__ import annotations

import os
import sqlite3
import glob


def backup_once(db_path: str, backup_dir: str, stamp: str, keep: int = 20) -> str:
    """Online, WAL-safe backup via VACUUM INTO. `stamp` is injected (no clock here).
    Returns the backup file path; prunes to the most recent `keep`."""
    os.makedirs(backup_dir, exist_ok=True)
    dest = os.path.join(backup_dir, f"app-{stamp}.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("VACUUM INTO ?", (dest,))
    finally:
        conn.close()
    backups = sorted(glob.glob(os.path.join(backup_dir, "app-*.db")))
    for old in backups[:-keep]:
        os.remove(old)
    return dest
