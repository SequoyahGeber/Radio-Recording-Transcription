import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime

from backend.config import (
    DATA_DIR,
    DB_NAME,
    RUNTIME_DIR,
    SECURITY_CONFIG_PATH,
    SETTINGS_PATH,
)
from backend.database import connect


BACKUP_DIR = os.path.join(DATA_DIR, "backups")


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_backup_row(row):
    payload = dict(row)
    payload["name"] = os.path.basename(payload.pop("backup_path"))
    return payload


def list_backups():
    with connect(read_only=True) as connection:
        rows = connection.execute(
            "SELECT * FROM backup_history ORDER BY created_at DESC, id DESC LIMIT 100"
        ).fetchall()
    return [_safe_backup_row(row) for row in rows]


def create_backup(username="system"):
    os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    destination = os.path.join(BACKUP_DIR, stamp)
    os.makedirs(destination, mode=0o700)
    database_path = os.path.join(destination, "radio-command-center.db")
    try:
        source = sqlite3.connect(DB_NAME, timeout=30)
        target = sqlite3.connect(database_path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        os.chmod(database_path, 0o600)
        for source_path, filename in (
            (SETTINGS_PATH, "settings.json"),
            (SECURITY_CONFIG_PATH, "security.json"),
        ):
            if os.path.isfile(source_path):
                copied = os.path.join(destination, filename)
                shutil.copy2(source_path, copied)
                os.chmod(copied, 0o600)
        with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as check:
            integrity = check.execute("PRAGMA quick_check").fetchone()[0]
            transcript_count = check.execute("SELECT count(*) FROM transcripts").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"Backup database verification failed: {integrity}")
        checksum = _sha256(database_path)
        manifest = {
            "created_at": datetime.now().isoformat(),
            "database_file": "radio-command-center.db",
            "database_bytes": os.path.getsize(database_path),
            "database_sha256": checksum,
            "transcript_count": transcript_count,
            "includes_settings": os.path.isfile(os.path.join(destination, "settings.json")),
            "includes_security": os.path.isfile(os.path.join(destination, "security.json")),
        }
        manifest_path = os.path.join(destination, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
        os.chmod(manifest_path, 0o600)
        with connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO backup_history(
                    backup_path, status, database_bytes, transcript_count,
                    sha256, created_by, created_at, verified_at
                ) VALUES (?, 'verified', ?, ?, ?, ?, ?, ?)
                """,
                (
                    destination,
                    manifest["database_bytes"],
                    transcript_count,
                    checksum,
                    username,
                    manifest["created_at"],
                    datetime.now().isoformat(),
                ),
            )
            backup_id = cursor.lastrowid
            connection.commit()
        return next(item for item in list_backups() if item["id"] == backup_id)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _backup_path(backup_id):
    with connect(read_only=True) as connection:
        row = connection.execute(
            "SELECT backup_path, sha256 FROM backup_history WHERE id = ?",
            (int(backup_id),),
        ).fetchone()
    if row is None:
        raise ValueError("Backup not found")
    real_root = os.path.realpath(BACKUP_DIR)
    path = os.path.realpath(row["backup_path"])
    if os.path.commonpath([real_root, path]) != real_root:
        raise RuntimeError("Backup path is outside protected storage")
    return path, row["sha256"]


def verify_backup(backup_id):
    path, expected_checksum = _backup_path(backup_id)
    database_path = os.path.join(path, "radio-command-center.db")
    error = ""
    try:
        if _sha256(database_path) != expected_checksum:
            raise RuntimeError("Backup checksum does not match")
        with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            count = connection.execute("SELECT count(*) FROM transcripts").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"Backup integrity check failed: {integrity}")
        status = "verified"
    except Exception as exc:
        status = "failed"
        count = 0
        error = str(exc)[:1000]
    with connect() as connection:
        connection.execute(
            """
            UPDATE backup_history
            SET status = ?, transcript_count = ?, verified_at = ?, last_error = ?
            WHERE id = ?
            """,
            (status, count, datetime.now().isoformat(), error, int(backup_id)),
        )
        connection.commit()
    if error:
        raise RuntimeError(error)
    return next(item for item in list_backups() if item["id"] == int(backup_id))


RESTORE_REQUEST_PATH = os.path.join(RUNTIME_DIR, "restore-request.json")


def request_restore(backup_id, username="system"):
    """Queue a backup to be restored the next time the services start.

    The archive cannot be swapped underneath a running dashboard and worker, so
    the request is recorded here and carried out by the supervisor before it
    launches anything. The backup is verified first, so a request is never
    queued against a file that cannot be read.
    """
    verify_backup(backup_id)
    path, checksum = _backup_path(backup_id)
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    request = {
        "backup_id": int(backup_id),
        "backup_path": path,
        "sha256": checksum,
        "requested_by": username,
        "requested_at": datetime.now().isoformat(),
    }
    temporary_path = f"{RESTORE_REQUEST_PATH}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as handle:
        json.dump(request, handle, indent=2, sort_keys=True)
    os.replace(temporary_path, RESTORE_REQUEST_PATH)
    os.chmod(RESTORE_REQUEST_PATH, 0o600)
    return request


def pending_restore():
    try:
        with open(RESTORE_REQUEST_PATH, "r", encoding="utf-8") as handle:
            request = json.load(handle)
    except (OSError, ValueError):
        return None
    return request if isinstance(request, dict) else None


def apply_pending_restore(log=print):
    """Carry out a queued restore. Safe to call when none is queued.

    The database in place is copied aside before anything replaces it, so a
    restore can itself be undone. The request file is removed whatever happens,
    so a failing restore cannot trap the services in a loop.
    """
    request = pending_restore()
    if not request:
        return None

    result = {"backup_id": request.get("backup_id"), "status": "failed", "error": ""}
    try:
        source = os.path.join(request["backup_path"], "radio-command-center.db")
        if _sha256(source) != request.get("sha256"):
            raise RuntimeError("The backup no longer matches its recorded checksum")
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as check:
            integrity = check.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"The backup failed its integrity check: {integrity}")

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        if os.path.isfile(DB_NAME):
            replaced = f"{DB_NAME}.replaced-{stamp}"
            shutil.copy2(DB_NAME, replaced)
            result["previous_database"] = replaced
            log(f"[RESTORE] Kept the previous archive at {replaced}")

        # The copy lands beside the archive first and only takes its place once
        # it is complete and verified. Copying straight onto the live file left
        # a window where running out of disk - or a data directory on a drive
        # that disconnects - truncated the archive with nothing to fall back to.
        staged = f"{DB_NAME}.restoring-{stamp}"
        try:
            shutil.copy2(source, staged)
            if _sha256(staged) != request.get("sha256"):
                raise RuntimeError("The restored copy did not survive being written")
            os.chmod(staged, 0o600)
            os.replace(staged, DB_NAME)
        except Exception:
            try:
                os.unlink(staged)
            except OSError:
                pass
            raise

        # The old write-ahead log belongs to the database that was just
        # replaced; leaving it in place would corrupt the restored one.
        for suffix in ("-wal", "-shm"):
            try:
                os.unlink(f"{DB_NAME}{suffix}")
            except OSError:
                pass
        result["status"] = "restored"
        log(f"[RESTORE] Restored backup {request.get('backup_id')} into {DB_NAME}")
    except Exception as exc:
        result["error"] = str(exc)
        log(f"[RESTORE] Restore failed and was abandoned: {exc}")
    finally:
        try:
            os.unlink(RESTORE_REQUEST_PATH)
        except OSError:
            pass
    return result


def restore_drill(backup_id):
    path, _ = _backup_path(backup_id)
    source = os.path.join(path, "radio-command-center.db")
    with tempfile.TemporaryDirectory(prefix="radio-restore-drill-") as directory:
        candidate = os.path.join(directory, "candidate.db")
        shutil.copy2(source, candidate)
        with sqlite3.connect(candidate) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            transcripts = connection.execute("SELECT count(*) FROM transcripts").fetchone()[0]
            tables = connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
            ).fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"Restore drill failed: {integrity}")
    return {"status": "passed", "transcript_count": transcripts, "table_count": tables}
