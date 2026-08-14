"""Background housekeeping that keeps an always-on installation healthy.

Without this, a Mac left running for months accumulates operational history,
rotated logs, backup folders, and write-ahead-log pages that nothing ever
removes. Every limit here is an administrator setting, and recordings and
transcripts are never touched.
"""

import os
import shutil
import threading
import time
from datetime import datetime, timedelta

from backend.backups import BACKUP_DIR, create_backup, list_backups
from backend.config import LOG_DIR, number_setting
from backend.database import connect


MAINTENANCE_INTERVAL_SECONDS = 30 * 60
_started = threading.Lock()
_running = False


def _retention_cutoff(days):
    return (datetime.now() - timedelta(days=int(days))).isoformat()


def prune_history():
    """Trim tables that grow forever, keeping the configured window."""
    removed = {}
    event_days = number_setting("event_retention_days", default=45, minimum=0, maximum=3650)
    audit_days = number_setting("audit_retention_days", default=730, minimum=0, maximum=3650)
    job_days = number_setting(
        "completed_job_retention_days", default=14, minimum=1, maximum=365
    )
    with connect() as connection:
        if event_days:
            cursor = connection.execute(
                "DELETE FROM events WHERE created_at < ?",
                (_retention_cutoff(event_days),),
            )
            removed["events"] = cursor.rowcount
        if audit_days:
            cursor = connection.execute(
                "DELETE FROM audit_log WHERE timestamp < ?",
                (_retention_cutoff(audit_days),),
            )
            removed["audit_log"] = cursor.rowcount
        cursor = connection.execute(
            """
            DELETE FROM transcription_jobs
            WHERE status IN ('completed', 'cancelled')
              AND coalesce(completed_at, updated_at) < ?
            """,
            (_retention_cutoff(job_days),),
        )
        removed["transcription_jobs"] = cursor.rowcount
        cursor = connection.execute(
            """
            DELETE FROM alert_deliveries
            WHERE attempted_at < ?
            """,
            (_retention_cutoff(max(7, int(event_days or 45))),),
        )
        removed["alert_deliveries"] = cursor.rowcount
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA optimize")
    return {key: value for key, value in removed.items() if value}


def prune_rotated_logs():
    """Delete rotated log files past the retention window."""
    days = number_setting("log_retention_days", default=30, minimum=1, maximum=3650)
    cutoff = time.time() - days * 86400
    removed = 0
    try:
        entries = list(os.scandir(LOG_DIR))
    except OSError:
        return 0
    for entry in entries:
        # Only rotated copies (server.log.1) are removed; live logs stay.
        if not entry.is_file() or ".log." not in entry.name:
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                os.unlink(entry.path)
                removed += 1
        except OSError:
            continue
    return removed


def prune_backups():
    """Keep the newest verified backups and delete the rest from disk."""
    keep = number_setting("backup_retention_count", default=10, minimum=1, maximum=90)
    with connect(read_only=True) as connection:
        rows = connection.execute(
            "SELECT id, backup_path FROM backup_history ORDER BY id DESC"
        ).fetchall()
    stale = rows[keep:]
    if not stale:
        return 0
    protected_root = os.path.realpath(BACKUP_DIR)
    removed_ids = []
    for row in stale:
        path = os.path.realpath(row["backup_path"] or "")
        if path and os.path.commonpath([protected_root, path]) == protected_root:
            shutil.rmtree(path, ignore_errors=True)
        removed_ids.append(row["id"])
    with connect() as connection:
        connection.executemany(
            "DELETE FROM backup_history WHERE id = ?",
            [(value,) for value in removed_ids],
        )
        connection.commit()
    return len(removed_ids)


def scheduled_backup_if_due():
    """Take a verified backup when the configured interval has elapsed."""
    hours = number_setting("backup_interval_hours", default=24, minimum=0, maximum=720)
    if not hours:
        return None
    backups = list_backups()
    for backup in backups:
        if backup.get("status") != "verified":
            continue
        try:
            created = datetime.fromisoformat(backup["created_at"])
        except (TypeError, ValueError, KeyError):
            continue
        if created > datetime.now() - timedelta(hours=hours):
            return None
        break
    return create_backup("automatic")


def run_maintenance_once(logger=None):
    results = {}
    for name, action in (
        ("backup", scheduled_backup_if_due),
        ("history", prune_history),
        ("logs", prune_rotated_logs),
        ("backups_pruned", prune_backups),
    ):
        try:
            results[name] = action()
        except Exception as exc:
            results[name] = f"failed: {exc}"
            if logger:
                logger.warning("Maintenance step %s failed: %s", name, exc)
    return results


def start_maintenance_thread(logger=None, interval=MAINTENANCE_INTERVAL_SECONDS):
    """Run housekeeping now and then on a fixed interval for the process's life."""
    global _running
    with _started:
        if _running:
            return False
        _running = True

    def loop():
        # Let the dashboard finish starting before the first pass does disk work.
        time.sleep(20)
        while True:
            results = run_maintenance_once(logger)
            if logger:
                logger.info("Maintenance pass complete: %s", results)
            time.sleep(interval)

    threading.Thread(target=loop, name="maintenance", daemon=True).start()
    return True
