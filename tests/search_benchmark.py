#!/usr/bin/env python3
"""Repeatable FTS5 performance gate for realistic multi-year archive sizes."""

import math
import os
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from backend.search import search_transcripts


ARCHIVE_SIZES = (100_000, 500_000)
MAX_P95_MS = float(os.environ.get("RADIO_SEARCH_P95_LIMIT_MS", "200"))
RUNS_PER_SIZE = int(os.environ.get("RADIO_SEARCH_BENCHMARK_RUNS", "12"))


def create_database(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous = OFF;
        PRAGMA temp_store = MEMORY;
        PRAGMA cache_size = -131072;

        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY,
            timestamp TEXT NOT NULL,
            recorded_at TEXT,
            recording_year INTEGER,
            channel TEXT,
            filename TEXT NOT NULL UNIQUE,
            transcript_text TEXT NOT NULL,
            quality_score REAL,
            quality_reason TEXT,
            status TEXT NOT NULL DEFAULT 'ready',
            reviewed INTEGER NOT NULL DEFAULT 0,
            review_state TEXT NOT NULL DEFAULT 'unreviewed',
            reviewed_by TEXT,
            reviewed_at TEXT,
            review_resolution TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            bookmarked INTEGER NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT '',
            corrected_by TEXT,
            corrected_at TEXT,
            transcription_model TEXT,
            retry_status TEXT
        );

        CREATE VIRTUAL TABLE transcripts_fts USING fts5(
            transcript_text,
            notes,
            filename,
            content='transcripts',
            content_rowid='id',
            tokenize='unicode61 remove_diacritics 2',
            prefix='2 3 4'
        );
        """
    )
    return connection


def add_rows(connection, start, stop):
    channels = ("Channel 1", "Channel 2", "Channel 3", "Channel 4")
    models = ("mlx-whisper-large-v3-turbo", "mlx-whisper-medium")
    batch = []
    for row_id in range(start, stop + 1):
        year = 2022 + (row_id % 5)
        month = 1 + (row_id % 12)
        day = 1 + (row_id % 28)
        recorded_at = f"{year:04d}-{month:02d}-{day:02d}T{row_id % 24:02d}:00:00"
        marker = (
            "critical medical beacon confirmed"
            if row_id % 9_973 == 0
            else "routine festival radio traffic"
        )
        batch.append(
            (
                row_id,
                recorded_at,
                recorded_at,
                year,
                channels[row_id % len(channels)],
                f"{recorded_at[:10]}_{row_id:07d}.mp3",
                f"{marker} at north gate unit {row_id % 400}",
                0.96,
                "benchmark",
                "ready",
                0,
                "unreviewed",
                1,
                0,
                "",
                models[row_id % len(models)],
            )
        )
        if len(batch) == 10_000:
            connection.executemany(
                """
                INSERT INTO transcripts (
                    id, timestamp, recorded_at, recording_year, channel,
                    filename, transcript_text, quality_score, quality_reason,
                    status, reviewed, review_state, version, bookmarked,
                    notes, transcription_model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                batch,
            )
            batch.clear()
    if batch:
        connection.executemany(
            """
            INSERT INTO transcripts (
                id, timestamp, recorded_at, recording_year, channel,
                filename, transcript_text, quality_score, quality_reason,
                status, reviewed, review_state, version, bookmarked,
                notes, transcription_model
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            batch,
        )
    connection.commit()


def percentile_95(values):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def measure(connection, archive_size):
    connection.execute("INSERT INTO transcripts_fts(transcripts_fts) VALUES('rebuild')")
    connection.execute("INSERT INTO transcripts_fts(transcripts_fts) VALUES('optimize')")
    connection.commit()

    query = '"critical medical beacon"'
    expected = archive_size // 9_973
    filters = {
        "channel": "Channel 2",
        "year": 2024,
        "include_suspect": True,
    }
    expected_filtered = connection.execute(
        """
        SELECT count(*)
        FROM transcripts
        WHERE id % 9973 = 0 AND channel = 'Channel 2'
          AND recording_year = 2024
        """
    ).fetchone()[0]

    unfiltered = search_transcripts(
        connection, query=query, limit=50, include_suspect=True
    )
    if unfiltered["count"] != expected:
        raise AssertionError(
            f"{archive_size:,}: expected {expected} phrase matches, "
            f"received {unfiltered['count']}"
        )

    search_transcripts(connection, query=query, limit=50, **filters)
    samples = []
    for _ in range(RUNS_PER_SIZE):
        started_at = time.perf_counter()
        result = search_transcripts(
            connection, query=query, limit=50, sort="relevance", **filters
        )
        samples.append((time.perf_counter() - started_at) * 1000)
        if result["count"] != expected_filtered:
            raise AssertionError(
                f"{archive_size:,}: filtered count mismatch "
                f"({result['count']} != {expected_filtered})"
            )
    p95 = percentile_95(samples)
    median = statistics.median(samples)
    print(
        f"{archive_size:,} rows: median={median:.2f} ms, "
        f"p95={p95:.2f} ms, exact_matches={expected_filtered}"
    )
    if p95 >= MAX_P95_MS:
        raise AssertionError(
            f"{archive_size:,}: p95 {p95:.2f} ms exceeds "
            f"{MAX_P95_MS:.0f} ms gate"
        )


def main():
    with tempfile.TemporaryDirectory(prefix="radio-search-benchmark-") as temp_dir:
        database_path = os.path.join(temp_dir, "archive.db")
        connection = create_database(database_path)
        previous_size = 0
        try:
            for archive_size in ARCHIVE_SIZES:
                add_rows(connection, previous_size + 1, archive_size)
                measure(connection, archive_size)
                previous_size = archive_size
        finally:
            connection.close()
    print(
        f"FTS5 benchmark passed at 100k and 500k rows "
        f"(p95 < {MAX_P95_MS:.0f} ms)."
    )


if __name__ == "__main__":
    main()
