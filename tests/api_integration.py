import base64
import os
import secrets
import sys
import tempfile
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

TEST_ROOT = tempfile.TemporaryDirectory()
SOURCE_DIR = os.path.join(TEST_ROOT.name, "source")
AUDIO_DIR = os.path.join(TEST_ROOT.name, "audio")
DATA_DIR = os.path.join(TEST_ROOT.name, "data")
os.makedirs(SOURCE_DIR)
os.makedirs(AUDIO_DIR)
os.environ["RADIO_DATA_DIR"] = DATA_DIR
os.environ["RADIO_SOURCE_DIR"] = SOURCE_DIR
os.environ["RADIO_AUDIO_DIR"] = AUDIO_DIR
os.environ["RADIO_DB_PATH"] = os.path.join(DATA_DIR, "api.db")
os.environ["RADIO_HOST"] = "127.0.0.1"

from fastapi.testclient import TestClient
from backend.database import connect
from backend.security import hash_password, save_security_config


salt, password_digest = hash_password("admin-password")
save_security_config(
    {
        "users": [
            {
                "username": "admin",
                "display_name": "Test Administrator",
                "role": "admin",
                "active": True,
                "password_salt": salt,
                "password_hash": password_digest,
            }
        ],
        "session_secret": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"),
        "internal_token": "test-internal-token",
    }
)

from backend.server import app


class ApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with connect() as connection:
            connection.execute(
                """
                INSERT INTO transcripts(
                    timestamp, recorded_at, filename, transcript_text,
                    raw_transcript_text, quality_score, quality_reason, status
                ) VALUES
                    ('2026-07-25T10:00:00', '2026-07-25T09:59:55',
                     'Alpha/2026-07-25-09-59-55-Alpha.mp3',
                     'medical requested at north gate',
                     'medical requested at north gate', 1.0, '', 'ready'),
                    ('2026-07-25T10:01:00', '2026-07-25T10:00:55',
                     'Bravo/2026-07-25-10-00-55-Bravo.mp3',
                     'hello hello hello hello hello',
                     'hello hello hello hello hello', 0.1,
                     'One word dominates the transcript', 'suspect')
                """
            )
            connection.commit()

    def setUp(self):
        self.client = TestClient(app, base_url="https://127.0.0.1")
        response = self.client.post(
            "/api/login",
            json={"username": "admin", "password": "admin-password"},
        )
        self.assertEqual(response.status_code, 200)

    def test_archive_search_scans_database_and_suspect_is_opt_in(self):
        response = self.client.get("/api/history", params={"q": "north gate"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        self.assertIn("medical requested", response.json()[0]["transcript_text"])

        normal = self.client.get("/api/history").json()
        reviewed = self.client.get(
            "/api/history", params={"include_suspect": "true"}
        ).json()
        self.assertEqual(len(normal), 1)
        self.assertEqual(len(reviewed), 2)

    def test_profile_clearance_and_transcript_updates(self):
        create_response = self.client.post(
            "/api/users",
            json={
                "username": "viewer",
                "display_name": "Read Only",
                "role": "viewer",
                "password": "viewer-password",
                "active": True,
            },
        )
        self.assertEqual(create_response.status_code, 200)

        viewer = TestClient(app, base_url="https://127.0.0.1")
        self.assertEqual(
            viewer.post(
                "/api/login",
                json={"username": "viewer", "password": "viewer-password"},
            ).status_code,
            200,
        )
        self.assertEqual(viewer.get("/api/export.csv").status_code, 403)
        self.assertEqual(
            viewer.get("/audio/Alpha/2026-07-25-09-59-55-Alpha.mp3").status_code,
            403,
        )

        transcript_id = self.client.get("/api/history").json()[0]["id"]
        update = self.client.patch(
            f"/api/transcripts/{transcript_id}",
            json={"reviewed": True, "bookmarked": True, "notes": "Incident reviewed"},
        )
        self.assertEqual(update.status_code, 200)
        self.assertTrue(update.json()["reviewed"])
        self.assertTrue(update.json()["bookmarked"])

    def test_internal_delivery_requires_private_token(self):
        payload = {
            "id": 99,
            "filename": "Alpha/test.mp3",
            "transcript_text": "test transmission",
            "timestamp": "2026-07-25T10:00:00",
            "status": "ready",
        }
        self.assertEqual(self.client.post("/api/new_transcript", json=payload).status_code, 403)
        self.assertEqual(
            self.client.post(
                "/api/new_transcript",
                json=payload,
                headers={"X-Radio-Internal-Token": "test-internal-token"},
            ).status_code,
            200,
        )


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        TEST_ROOT.cleanup()
