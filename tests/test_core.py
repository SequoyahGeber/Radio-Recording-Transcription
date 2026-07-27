import atexit
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta


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

from backend.database import connect, initialize_database
from backend.transcript_quality import assess_transcript
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
            }.issubset(columns)
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

    def test_verified_copy_records_source_identity(self):
        filename = "2026-01-01-00-00-00-Test.mp3"
        source_path = os.path.join(SOURCE_DIR, filename)
        destination_path = os.path.join(AUDIO_DIR, filename)
        with open(source_path, "wb") as handle:
            handle.write(b"verified audio bytes")
        source_stat = os.stat(source_path)
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


if __name__ == "__main__":
    unittest.main()
