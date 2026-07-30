import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from glob import glob

from backend.config import DB_NAME


TRANSCRIPT_COLUMNS = {
    "recorded_at": "TEXT",
    "recording_year": "INTEGER",
    "channel": "TEXT",
    "raw_transcript_text": "TEXT",
    "quality_score": "REAL NOT NULL DEFAULT 1.0",
    "quality_reason": "TEXT",
    "quality_metrics": "TEXT NOT NULL DEFAULT '{}'",
    "status": "TEXT NOT NULL DEFAULT 'ready'",
    "broadcast_pending": "INTEGER NOT NULL DEFAULT 0",
    "broadcast_attempts": "INTEGER NOT NULL DEFAULT 0",
    "last_broadcast_error": "TEXT",
    "reviewed": "INTEGER NOT NULL DEFAULT 0",
    "review_state": "TEXT NOT NULL DEFAULT 'unreviewed'",
    "reviewed_by": "TEXT",
    "reviewed_at": "TEXT",
    "review_resolution": "TEXT NOT NULL DEFAULT ''",
    "version": "INTEGER NOT NULL DEFAULT 1",
    "bookmarked": "INTEGER NOT NULL DEFAULT 0",
    "notes": "TEXT NOT NULL DEFAULT ''",
    "corrected_by": "TEXT",
    "corrected_at": "TEXT",
    "transcription_model": "TEXT",
    "retry_transcript_text": "TEXT",
    "retry_model": "TEXT",
    "retry_quality_score": "REAL",
    "retry_quality_reason": "TEXT",
    "retry_quality_metrics": "TEXT",
    "retry_status": "TEXT",
    "retry_attempted_at": "TEXT",
}

FTS_SCHEMA_VERSION = "1"
TIMESTAMP_PREFIX = re.compile(
    r"(?P<year>19\d{2}|20\d{2})-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}"
)


def infer_recording_year(recorded_at, timestamp, filename):
    for value in (recorded_at, timestamp, filename):
        match = re.search(r"(19\d{2}|20\d{2})", str(value or ""))
        if match:
            return int(match.group(1))
    return None


def infer_channel(filename):
    value = str(filename or "").replace("\\", "/").strip("/")
    if "/" in value:
        return value.split("/", 1)[0][:160]
    basename = os.path.splitext(os.path.basename(value))[0]
    basename = TIMESTAMP_PREFIX.sub("", basename, count=1).lstrip("-_ ")
    return (basename or "Unknown")[:160]


def backfill_archive_metadata(connection):
    rows = connection.execute(
        """
        SELECT id, recorded_at, timestamp, filename, recording_year, channel
        FROM transcripts
        WHERE recording_year IS NULL OR channel IS NULL OR channel = ''
        """
    ).fetchall()
    updates = []
    for row in rows:
        updates.append(
            (
                row["recording_year"]
                or infer_recording_year(
                    row["recorded_at"], row["timestamp"], row["filename"]
                ),
                row["channel"] or infer_channel(row["filename"]),
                row["id"],
            )
        )
    if updates:
        connection.executemany(
            """
            UPDATE transcripts
            SET recording_year = ?, channel = ?
            WHERE id = ?
            """,
            updates,
        )


def annual_database_paths():
    database_directory = os.path.dirname(DB_NAME)
    active_path = os.path.realpath(DB_NAME)
    candidates = {
        os.path.realpath(path)
        for path in glob(os.path.join(database_directory, "festival_radio_*.db"))
    }
    return sorted(path for path in candidates if path != active_path)


def backup_before_archive_import(connection):
    backup_path = f"{DB_NAME}.pre-multiyear.bak"
    if os.path.exists(backup_path):
        return backup_path
    backup_connection = sqlite3.connect(backup_path)
    try:
        connection.backup(backup_connection)
    finally:
        backup_connection.close()
    return backup_path


def import_annual_databases(connection):
    imported = []
    sources = annual_database_paths()
    if not sources:
        return imported
    connection.commit()
    backup_before_archive_import(connection)
    destination_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(transcripts)")
    }
    for index, source_path in enumerate(sources):
        source_stat = os.stat(source_path)
        existing = connection.execute(
            """
            SELECT source_size, source_mtime_ns
            FROM archive_imports
            WHERE source_path = ?
            """,
            (source_path,),
        ).fetchone()
        if (
            existing is not None
            and existing["source_size"] == source_stat.st_size
            and existing["source_mtime_ns"] == source_stat.st_mtime_ns
        ):
            continue
        alias = f"annual_{index}"
        connection.execute(f"ATTACH DATABASE ? AS {alias}", (source_path,))
        try:
            table_exists = connection.execute(
                f"""
                SELECT 1 FROM {alias}.sqlite_master
                WHERE type = 'table' AND name = 'transcripts'
                """
            ).fetchone()
            if table_exists is None:
                continue
            source_columns = {
                row["name"]
                for row in connection.execute(
                    f"PRAGMA {alias}.table_info(transcripts)"
                )
            }
            columns = sorted(
                (destination_columns & source_columns) - {"id"}
            )
            if not {"filename", "transcript_text"}.issubset(columns):
                continue
            quoted_columns = ", ".join(f'"{column}"' for column in columns)
            before_count = connection.execute(
                "SELECT count(*) FROM transcripts"
            ).fetchone()[0]
            connection.execute(
                f"""
                INSERT OR IGNORE INTO transcripts({quoted_columns})
                SELECT {quoted_columns} FROM {alias}.transcripts
                """
            )
            after_count = connection.execute(
                "SELECT count(*) FROM transcripts"
            ).fetchone()[0]
            source_count = connection.execute(
                f"SELECT count(*) FROM {alias}.transcripts"
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO archive_imports(
                    source_path, source_size, source_mtime_ns,
                    source_count, imported_count, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_path) DO UPDATE SET
                    source_size = excluded.source_size,
                    source_mtime_ns = excluded.source_mtime_ns,
                    source_count = excluded.source_count,
                    imported_count = excluded.imported_count,
                    imported_at = excluded.imported_at
                """,
                (
                    source_path,
                    source_stat.st_size,
                    source_stat.st_mtime_ns,
                    source_count,
                    after_count - before_count,
                    datetime.now().isoformat(),
                ),
            )
            imported.append(
                {
                    "source_path": source_path,
                    "source_count": source_count,
                    "imported_count": after_count - before_count,
                }
            )
        finally:
            connection.commit()
            connection.execute(f"DETACH DATABASE {alias}")
    backfill_archive_metadata(connection)
    return imported


@contextmanager
def connect(read_only=False):
    if read_only:
        connection = sqlite3.connect(
            f"file:{DB_NAME}?mode=ro",
            uri=True,
            timeout=15,
        )
    else:
        connection = sqlite3.connect(DB_NAME, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def initialize_database():
    with connect() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS transcripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                filename TEXT NOT NULL UNIQUE,
                transcript_text TEXT
            )
            """
        )
        existing_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(transcripts)")
        }
        for name, definition in TRANSCRIPT_COLUMNS.items():
            if name not in existing_columns:
                try:
                    connection.execute(
                        f"ALTER TABLE transcripts ADD COLUMN {name} {definition}"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise

        connection.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_transcripts_filename
                ON transcripts(filename);
            CREATE INDEX IF NOT EXISTS idx_transcripts_timestamp
                ON transcripts(timestamp);
            CREATE INDEX IF NOT EXISTS idx_transcripts_recorded_at
                ON transcripts(recorded_at);
            CREATE INDEX IF NOT EXISTS idx_transcripts_recorded_at_id
                ON transcripts(recorded_at, id);
            CREATE INDEX IF NOT EXISTS idx_transcripts_year_recorded_id
                ON transcripts(recording_year, recorded_at, id);
            CREATE INDEX IF NOT EXISTS idx_transcripts_channel_recorded_id
                ON transcripts(channel, recorded_at, id);
            CREATE INDEX IF NOT EXISTS idx_transcripts_review_state
                ON transcripts(review_state, recorded_at, id);
            CREATE INDEX IF NOT EXISTS idx_transcripts_model
                ON transcripts(transcription_model, recorded_at, id);
            CREATE INDEX IF NOT EXISTS idx_transcripts_status_id
                ON transcripts(status, id);
            CREATE INDEX IF NOT EXISTS idx_transcripts_pending
                ON transcripts(broadcast_pending, id);
            CREATE INDEX IF NOT EXISTS idx_transcripts_bookmarked_status_id
                ON transcripts(bookmarked, status, id);

            CREATE TABLE IF NOT EXISTS service_heartbeats (
                service TEXT PRIMARY KEY,
                last_seen TEXT NOT NULL,
                status TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                username TEXT NOT NULL,
                action TEXT NOT NULL,
                transcript_id INTEGER,
                details TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS synced_files (
                relative_path TEXT PRIMARY KEY,
                source_size INTEGER NOT NULL,
                source_mtime_ns INTEGER NOT NULL,
                sha256 TEXT,
                synced_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS transcript_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transcript_id INTEGER NOT NULL,
                version INTEGER NOT NULL,
                changed_at TEXT NOT NULL,
                changed_by TEXT NOT NULL,
                change_type TEXT NOT NULL,
                before_json TEXT NOT NULL,
                after_json TEXT NOT NULL,
                FOREIGN KEY(transcript_id) REFERENCES transcripts(id)
            );
            CREATE INDEX IF NOT EXISTS idx_transcript_versions_transcript
                ON transcript_versions(transcript_id, id DESC);

            CREATE TABLE IF NOT EXISTS saved_workspaces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_username TEXT NOT NULL,
                name TEXT NOT NULL,
                configuration TEXT NOT NULL DEFAULT '{}',
                is_shared INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(owner_username, name)
            );
            CREATE INDEX IF NOT EXISTS idx_saved_workspaces_visibility
                ON saved_workspaces(is_shared, owner_username, name);

            CREATE TABLE IF NOT EXISTS user_preferences (
                username TEXT PRIMARY KEY,
                configuration TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS saved_searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_username TEXT NOT NULL,
                name TEXT NOT NULL,
                configuration TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(owner_username, name)
            );
            CREATE INDEX IF NOT EXISTS idx_saved_searches_owner_name
                ON saved_searches(owner_username, name);

            CREATE TABLE IF NOT EXISTS archive_imports (
                source_path TEXT PRIMARY KEY,
                source_size INTEGER NOT NULL,
                source_mtime_ns INTEGER NOT NULL,
                source_count INTEGER NOT NULL,
                imported_count INTEGER NOT NULL,
                imported_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS schema_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS transcripts_fts USING fts5(
                transcript_text,
                notes,
                filename,
                content='transcripts',
                content_rowid='id',
                tokenize='unicode61 remove_diacritics 2',
                prefix='2 3 4'
            );

            CREATE TRIGGER IF NOT EXISTS transcripts_fts_insert
            AFTER INSERT ON transcripts BEGIN
                INSERT INTO transcripts_fts(
                    rowid, transcript_text, notes, filename
                ) VALUES (
                    new.id, new.transcript_text, new.notes, new.filename
                );
            END;

            CREATE TRIGGER IF NOT EXISTS transcripts_fts_delete
            AFTER DELETE ON transcripts BEGIN
                INSERT INTO transcripts_fts(
                    transcripts_fts, rowid, transcript_text, notes, filename
                ) VALUES (
                    'delete', old.id, old.transcript_text, old.notes, old.filename
                );
            END;

            CREATE TRIGGER IF NOT EXISTS transcripts_fts_update
            AFTER UPDATE OF transcript_text, notes, filename ON transcripts BEGIN
                INSERT INTO transcripts_fts(
                    transcripts_fts, rowid, transcript_text, notes, filename
                ) VALUES (
                    'delete', old.id, old.transcript_text, old.notes, old.filename
                );
                INSERT INTO transcripts_fts(
                    rowid, transcript_text, notes, filename
                ) VALUES (
                    new.id, new.transcript_text, new.notes, new.filename
                );
            END;
            """
        )
        connection.execute(
            """
            UPDATE transcripts
            SET review_state = CASE
                WHEN reviewed = 1 THEN 'confirmed'
                ELSE 'unreviewed'
            END
            WHERE review_state IS NULL
               OR review_state = ''
               OR (reviewed = 1 AND review_state = 'unreviewed')
            """
        )
        connection.execute(
            """
            UPDATE transcripts
            SET raw_transcript_text = transcript_text
            WHERE raw_transcript_text IS NULL
            """
        )
        backfill_archive_metadata(connection)
        imported_archives = import_annual_databases(connection)
        fts_version = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'fts_schema_version'"
        ).fetchone()
        if fts_version is None or fts_version["value"] != FTS_SCHEMA_VERSION:
            connection.execute(
                "INSERT INTO transcripts_fts(transcripts_fts) VALUES ('rebuild')"
            )
            connection.execute(
                """
                INSERT INTO schema_metadata(key, value, updated_at)
                VALUES ('fts_schema_version', ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (FTS_SCHEMA_VERSION, datetime.now().isoformat()),
            )
        elif imported_archives:
            # Import triggers index new rows; optimize the segments after a
            # potentially large historical merge.
            connection.execute(
                "INSERT INTO transcripts_fts(transcripts_fts) VALUES ('optimize')"
            )
        from backend.transcript_quality import assess_transcript

        legacy_rows = connection.execute(
            """
            SELECT id, transcript_text
            FROM transcripts
            WHERE quality_reason IS NULL
            """
        ).fetchall()
        for row in legacy_rows:
            quality = assess_transcript(row["transcript_text"])
            connection.execute(
                """
                UPDATE transcripts
                SET quality_score = ?, quality_reason = ?, status = ?
                WHERE id = ?
                """,
                (
                    quality["score"],
                    quality["reason"],
                    quality["status"],
                    row["id"],
                ),
            )
        connection.commit()


def update_heartbeat(service, status="online", details=None):
    payload = json.dumps(details or {}, separators=(",", ":"), sort_keys=True)
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO service_heartbeats(service, last_seen, status, details)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(service) DO UPDATE SET
                last_seen = excluded.last_seen,
                status = excluded.status,
                details = excluded.details
            """,
            (service, datetime.now().isoformat(), status, payload),
        )
        connection.commit()


def audit(username, action, transcript_id=None, details=None):
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO audit_log(timestamp, username, action, transcript_id, details)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                datetime.now().isoformat(),
                username,
                action,
                transcript_id,
                json.dumps(details or {}, separators=(",", ":"), sort_keys=True),
            ),
        )
        connection.commit()


initialize_database()
