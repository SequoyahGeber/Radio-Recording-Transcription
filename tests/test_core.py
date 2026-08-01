import atexit
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock


TEST_ROOT = tempfile.TemporaryDirectory()
atexit.register(TEST_ROOT.cleanup)
SOURCE_DIR = os.path.join(TEST_ROOT.name, "source")
AUDIO_DIR = os.path.join(TEST_ROOT.name, "audio")
DATA_DIR = os.path.join(TEST_ROOT.name, "data")
os.makedirs(SOURCE_DIR)
os.makedirs(AUDIO_DIR)
os.environ["RADIO_DATA_DIR"] = DATA_DIR
os.environ["RADIO_SOURCE_DIR"] = SOURCE_DIR
os.environ["RADIO_AUDIO_DIR"] = AUDIO_DIR
os.environ["RADIO_DB_PATH"] = os.path.join(DATA_DIR, "test.db")
os.environ["RADIO_RECORDING_YEAR"] = "2026"

from backend.alerts import rule_matches
from backend.database import connect, initialize_database
from backend.transcript_quality import (
    assess_transcript,
    choose_retry_result,
    should_retry_with_larger_model,
)
import sync


class TranscriptQualityTests(unittest.TestCase):
    def test_blank_audio_is_hidden(self):
        result = assess_transcript("[silence]", 5)
        self.assertEqual(result["status"], "blank")

    def test_long_single_word_loop_is_suspect(self):
        result = assess_transcript("hello hello hello hello hello hello", 12)
        self.assertEqual(result["status"], "suspect")
        self.assertLess(result["score"], 0.45)

    def test_repeating_phrase_is_suspect(self):
        result = assess_transcript("check radio check radio check radio check radio", 9)
        self.assertEqual(result["status"], "suspect")

    def test_short_urgent_repetition_is_not_automatically_hidden(self):
        result = assess_transcript("go go go", 2)
        self.assertEqual(result["status"], "ready")

    def test_normal_radio_message_is_ready(self):
        result = assess_transcript(
            "Security team proceed to the north entrance for a medical call",
            8,
        )
        self.assertEqual(result["status"], "ready")

    def test_suspect_transcript_is_eligible_for_large_v3_rescue(self):
        quality = assess_transcript("hello hello hello hello hello hello", 12)
        self.assertTrue(should_retry_with_larger_model(quality, 12))

    def test_blank_or_very_long_recording_skips_large_v3_rescue(self):
        self.assertFalse(
            should_retry_with_larger_model(assess_transcript("[silence]", 5), 5)
        )
        quality = assess_transcript("ordinary radio traffic", 240)
        self.assertFalse(should_retry_with_larger_model(quality, 240))

    def test_ready_large_v3_result_can_replace_suspect_medium_result(self):
        primary = assess_transcript("hello hello hello hello hello hello", 12)
        retry = assess_transcript(
            "Security team proceed to the north entrance for a medical call",
            12,
        )
        text, quality, used_retry = choose_retry_result(
            "hello hello hello hello hello hello",
            primary,
            "Security team proceed to the north entrance for a medical call",
            retry,
        )
        self.assertTrue(used_retry)
        self.assertEqual(quality["status"], "ready")
        self.assertIn("north entrance", text)

    def test_blank_large_v3_result_never_replaces_medium_result(self):
        primary_text = "Security team proceed to the north entrance"
        primary = assess_transcript(primary_text, 8)
        retry = assess_transcript("", 8)
        text, _, used_retry = choose_retry_result(
            primary_text,
            primary,
            "",
            retry,
        )
        self.assertFalse(used_retry)
        self.assertEqual(text, primary_text)


class DatabaseMigrationTests(unittest.TestCase):
    def test_schema_contains_reliability_and_review_fields(self):
        initialize_database()
        with connect(read_only=True) as connection:
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(transcripts)")
            }
        self.assertTrue(
            {
                "quality_score",
                "status",
                "broadcast_pending",
                "reviewed",
                "bookmarked",
                "notes",
                "transcription_model",
                "retry_transcript_text",
                "retry_model",
                "retry_status",
                "recording_year",
                "channel",
                "review_state",
                "version",
            }.issubset(columns)
        )
        with connect(read_only=True) as connection:
            fts_table = connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name = 'transcripts_fts'
                """
            ).fetchone()
            phase_four_tables = {
                row["name"]
                for row in connection.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table'
                      AND name IN (
                          'events', 'alert_rules', 'alert_events',
                          'alert_acknowledgements', 'alert_deliveries',
                          'user_notification_preferences'
                      )
                    """
                )
            }
            default_rules = connection.execute(
                "SELECT count(*) FROM alert_rules WHERE is_default = 1"
            ).fetchone()[0]
        self.assertIsNotNone(fts_table)
        self.assertEqual(len(phase_four_tables), 6)
        self.assertGreaterEqual(default_rules, 5)


class AlertRuleMatchingTests(unittest.TestCase):
    def rule(self, **overrides):
        base = {
            "minimum_quality": 0.5,
            "channels": ["Medical"],
            "start_time": "",
            "end_time": "",
            "match_mode": "whole_word",
            "terms": ["medical", "not breathing"],
            "exclusions": ["training exercise"],
        }
        return {**base, **overrides}

    def transcript(self, text, **overrides):
        base = {
            "transcript_text": text,
            "channel": "Medical",
            "quality_score": 0.9,
            "recorded_at": "2026-07-30T13:30:00",
        }
        return {**base, **overrides}

    def test_whole_word_phrase_channel_quality_and_exclusion_filters(self):
        self.assertEqual(
            rule_matches(
                self.rule(),
                self.transcript("Medical requested, guest is not breathing."),
            ),
            ["Medical", "not breathing"],
        )
        self.assertEqual(
            rule_matches(
                self.rule(),
                self.transcript("Biomedical equipment check"),
            ),
            [],
        )
        self.assertEqual(
            rule_matches(
                self.rule(),
                self.transcript("Medical training exercise only"),
            ),
            [],
        )
        self.assertEqual(
            rule_matches(
                self.rule(),
                self.transcript("Medical requested", channel="Security"),
            ),
            [],
        )
        self.assertEqual(
            rule_matches(
                self.rule(),
                self.transcript("Medical requested", quality_score=0.2),
            ),
            [],
        )

    def test_prefix_and_overnight_time_scope(self):
        rule = self.rule(
            channels=[],
            match_mode="prefix",
            terms=["medic"],
            exclusions=[],
            start_time="22:00",
            end_time="06:00",
        )
        self.assertEqual(
            rule_matches(
                rule,
                self.transcript(
                    "Medical team requested",
                    recorded_at="2026-07-30T23:15:00",
                ),
            ),
            ["Medical"],
        )
        self.assertEqual(
            rule_matches(
                rule,
                self.transcript(
                    "Medical team requested",
                    recorded_at="2026-07-30T12:15:00",
                ),
            ),
            [],
        )


class MultiYearArchiveMigrationTests(unittest.TestCase):
    def test_annual_databases_import_idempotently_with_backup_and_year_counts(self):
        from backend import database

        with tempfile.TemporaryDirectory() as root:
            destination = os.path.join(root, "festival_radio_2026.db")
            source = os.path.join(root, "festival_radio_2024.db")
            source_connection = sqlite3.connect(source)
            source_connection.execute(
                """
                CREATE TABLE transcripts(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    recorded_at TEXT,
                    filename TEXT NOT NULL UNIQUE,
                    transcript_text TEXT,
                    notes TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'ready',
                    reviewed INTEGER NOT NULL DEFAULT 0,
                    bookmarked INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            source_connection.execute(
                """
                INSERT INTO transcripts(
                    timestamp, recorded_at, filename, transcript_text
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    "2024-07-01T10:00:05",
                    "2024-07-01T10:00:00",
                    "Archive/2024-07-01-10-00-00-archive.mp3",
                    "historical archive verification",
                ),
            )
            source_connection.commit()
            source_connection.close()

            with mock.patch.object(database, "DB_NAME", destination):
                database.initialize_database()
                database.initialize_database()
                with database.connect(read_only=True) as connection:
                    rows = connection.execute(
                        """
                        SELECT recording_year, channel, count(*) AS count
                        FROM transcripts
                        GROUP BY recording_year, channel
                        """
                    ).fetchall()
                    imports = connection.execute(
                        "SELECT source_count, imported_count FROM archive_imports"
                    ).fetchall()
                    fts_count = connection.execute(
                        """
                        SELECT count(*) FROM transcripts_fts
                        WHERE transcripts_fts MATCH 'historical'
                        """
                    ).fetchone()[0]

            self.assertEqual(
                [(row["recording_year"], row["channel"], row["count"]) for row in rows],
                [(2024, "Archive", 1)],
            )
            self.assertEqual(
                [(row["source_count"], row["imported_count"]) for row in imports],
                [(1, 1)],
            )
            self.assertEqual(fts_count, 1)
            self.assertTrue(
                os.path.isfile(f"{destination}.pre-multiyear.bak")
            )


class SyncPriorityTests(unittest.TestCase):
    def setUp(self):
        for filename in os.listdir(SOURCE_DIR):
            os.unlink(os.path.join(SOURCE_DIR, filename))

    def test_live_recordings_sort_before_backlog(self):
        old_name = "2026-01-01-00-00-00-Archive.mp3"
        live_time = datetime.now() - timedelta(minutes=1)
        live_name = live_time.strftime("%Y-%m-%d-%H-%M-%S") + "-Live.mp3"
        for filename in (old_name, live_name):
            with open(os.path.join(SOURCE_DIR, filename), "wb") as handle:
                handle.write(b"audio")
        candidates = sync.candidate_files()
        self.assertEqual(candidates[0][2], live_name)
        self.assertEqual(candidates[0][0], 0)
        self.assertEqual(candidates[1][0], 1)

    def test_backlog_uses_newest_supported_audio_first(self):
        filenames = (
            "2026-01-01-00-00-00-Archive.mp3",
            "2026-02-01-00-00-00-Newer.wav",
            "2026-03-01-00-00-00-Newest.m4a",
        )
        for filename in filenames:
            with open(os.path.join(SOURCE_DIR, filename), "wb") as handle:
                handle.write(b"audio")

        candidates = sync.candidate_files()

        self.assertEqual(
            [candidate[2] for candidate in candidates],
            list(reversed(filenames)),
        )

    def test_candidate_scan_accepts_multiple_years_and_generic_audio_names(self):
        filenames = (
            "2024-01-01-00-00-00-Archive.mp3",
            "2025-01-01-00-00-00-Archive.wav",
            "untimestamped-radio-clip.m4a",
        )
        for filename in filenames:
            path = os.path.join(SOURCE_DIR, filename)
            with open(path, "wb") as handle:
                handle.write(b"audio")
            old_time = datetime(2025, 1, 2).timestamp()
            os.utime(path, (old_time, old_time))

        candidates = sync.candidate_files()

        self.assertEqual(
            {candidate[2] for candidate in candidates},
            set(filenames),
        )

    def test_timestamped_archive_scan_does_not_stat_every_network_file(self):
        filename = "2026-04-01-12-00-00-Archive.mp3"
        with open(os.path.join(SOURCE_DIR, filename), "wb") as handle:
            handle.write(b"audio")

        with mock.patch.object(
            sync.os,
            "stat",
            side_effect=AssertionError("timestamped files should be sorted by name"),
        ):
            candidates = sync.candidate_files()

        self.assertEqual(candidates[0][2], filename)
        self.assertIsNone(candidates[0][4])

    def test_unwritable_destination_fails_before_network_scan(self):
        heartbeat_calls = []
        with (
            mock.patch.object(sync.os.path, "isdir", return_value=True),
            mock.patch.object(
                sync,
                "destination_write_error",
                return_value="Operation not permitted",
            ),
            mock.patch.object(sync, "candidate_files") as candidate_files,
            mock.patch.object(
                sync,
                "update_heartbeat",
                side_effect=lambda *args, **kwargs: heartbeat_calls.append(
                    (args, kwargs)
                ),
            ),
        ):
            sync.sync_once()

        candidate_files.assert_not_called()
        self.assertEqual(heartbeat_calls[-1][0][1], "error")
        self.assertEqual(
            heartbeat_calls[-1][0][2]["reason"],
            "destination_not_writable",
        )

    def test_verified_copy_records_source_identity(self):
        filename = "2026-01-01-00-00-00-Test.mp3"
        source_path = os.path.join(SOURCE_DIR, filename)
        destination_path = os.path.join(AUDIO_DIR, filename)
        with open(source_path, "wb") as handle:
            handle.write(b"verified audio bytes")
        source_stat = os.stat(source_path)
        with mock.patch.object(
            sync.shutil,
            "copy2",
            side_effect=AssertionError("network metadata must not be copied"),
        ):
            sync.copy_verified(filename, source_path, source_stat, destination_path)
        with open(destination_path, "rb") as handle:
            self.assertEqual(handle.read(), b"verified audio bytes")
        with connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT sha256 FROM synced_files WHERE relative_path = ?",
                (filename,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertTrue(row["sha256"])

    def test_low_disk_heartbeat_remains_paused_after_scan(self):
        filename = "2026-01-01-00-00-00-LowDisk.mp3"
        source_path = os.path.join(SOURCE_DIR, filename)
        with open(source_path, "wb") as handle:
            handle.write(b"audio")
        source_stat = os.stat(source_path)
        heartbeat_calls = []

        with (
            mock.patch.object(
                sync,
                "candidate_files",
                return_value=[
                    (
                        1,
                        datetime(2026, 1, 1).timestamp(),
                        filename,
                        source_path,
                        source_stat,
                    )
                ],
            ),
            mock.patch.object(sync, "load_sync_records", return_value={}),
            mock.patch.object(sync, "source_is_stable", return_value=True),
            mock.patch.object(
                sync.shutil,
                "disk_usage",
                return_value=SimpleNamespace(free=0),
            ),
            mock.patch.object(
                sync,
                "update_heartbeat",
                side_effect=lambda *args, **kwargs: heartbeat_calls.append(
                    (args, kwargs)
                ),
            ),
        ):
            sync.sync_once()

        self.assertTrue(heartbeat_calls)
        self.assertEqual(heartbeat_calls[-1][0][1], "paused")
        self.assertEqual(
            heartbeat_calls[-1][0][2]["reason"],
            "low_disk_space",
        )


if __name__ == "__main__":
    unittest.main()
