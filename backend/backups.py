import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta

from backend.config import DATA_DIR, DB_NAME, SECURITY_CONFIG_PATH, SETTINGS_PATH
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


def create_scheduled_backup_if_due(hours=24):
    with connect(read_only=True) as connection:
        row = connection.execute(
            "SELECT created_at FROM backup_history WHERE status = 'verified' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row:
        try:
            if datetime.fromisoformat(row["created_at"]) > datetime.now() - timedelta(hours=hours):
                return None
        except (TypeError, ValueError):
            pass
    return create_backup("automatic")
