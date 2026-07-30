import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from backend.config import DB_NAME


TRANSCRIPT_COLUMNS = {
    "recorded_at": "TEXT",
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
