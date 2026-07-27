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
from backend.config import LOG_DIR
from backend.database import connect
from backend.security import hash_password, load_security_config, save_security_config


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
        self.assertEqual(viewer.get("/api/console").status_code, 403)
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

    def test_operator_has_dashboard_access_without_admin_console(self):
        create_response = self.client.post(
            "/api/users",
            json={
                "username": "operator",
                "display_name": "Dashboard Operator",
                "role": "operator",
                "password": "operator-password",
                "active": True,
            },
        )
        self.assertEqual(create_response.status_code, 200)

        operator = TestClient(app, base_url="https://127.0.0.1")
        self.assertEqual(
            operator.post(
                "/api/login",
                json={"username": "operator", "password": "operator-password"},
            ).status_code,
            200,
        )
        profile = operator.get("/api/me")
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.json()["role"], "operator")
        self.assertFalse(profile.json()["permissions"]["console"])
        self.assertEqual(operator.get("/api/history").status_code, 200)
        self.assertEqual(operator.get("/api/console").status_code, 403)

    def test_first_run_setup_creates_only_one_administrator(self):
        original_config = load_security_config()
        try:
            fresh_config = dict(original_config)
            fresh_config["users"] = []
            save_security_config(fresh_config)

            fresh_client = TestClient(
                app,
                base_url="https://127.0.0.1",
                follow_redirects=False,
            )
            dashboard = fresh_client.get(
                "/",
                headers={"Accept": "text/html"},
            )
            self.assertEqual(dashboard.status_code, 303)
            self.assertEqual(dashboard.headers["location"], "/setup")
            self.assertEqual(fresh_client.get("/setup").status_code, 200)
            self.assertEqual(
                fresh_client.post(
                    "/api/setup",
                    json={
                        "display_name": "Too Short",
                        "username": "admin",
                        "password": "short",
                    },
                ).status_code,
                400,
            )

            setup = fresh_client.post(
                "/api/setup",
                json={
                    "display_name": "Festival Administrator",
                    "username": "festival-admin",
                    "password": "a-secure-password",
                },
            )
            self.assertEqual(setup.status_code, 200)
            self.assertIn("radio_session", setup.cookies)

            profile = fresh_client.get("/api/me")
            self.assertEqual(profile.status_code, 200)
            self.assertEqual(profile.json()["username"], "festival-admin")
            self.assertEqual(profile.json()["role"], "admin")

            repeated = fresh_client.post(
                "/api/setup",
                json={
                    "display_name": "Second Administrator",
                    "username": "second-admin",
                    "password": "another-secure-password",
                },
            )
            self.assertEqual(repeated.status_code, 409)
        finally:
            save_security_config(original_config)

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

    def test_archive_loads_recent_page_then_older_page_without_overlap(self):
        filenames = [f"Paging/pagination-{index:03d}.mp3" for index in range(125)]
        try:
            with connect() as connection:
                connection.executemany(
                    """
                    INSERT INTO transcripts(
                        timestamp, recorded_at, filename, transcript_text,
                        raw_transcript_text, quality_score, quality_reason, status
                    ) VALUES (?, ?, ?, ?, ?, 1.0, '', 'ready')
                    """,
                    [
                        (
                            f"2026-07-24T08:{index // 60:02d}:{index % 60:02d}",
                            f"2026-07-24T08:{index // 60:02d}:{index % 60:02d}",
                            filename,
                            f"pagination-marker message {index}",
                            f"pagination-marker message {index}",
                        )
                        for index, filename in enumerate(filenames)
                    ],
                )
                connection.commit()

            recent = self.client.get(
                "/api/history",
                params={"q": "pagination-marker", "limit": 100},
            )
            self.assertEqual(recent.status_code, 200)
            recent_rows = recent.json()
            self.assertEqual(len(recent_rows), 100)

            older = self.client.get(
                "/api/history",
                params={
                    "q": "pagination-marker",
                    "limit": 100,
                    "before_id": recent_rows[0]["id"],
                },
            )
            self.assertEqual(older.status_code, 200)
            older_rows = older.json()
            self.assertEqual(len(older_rows), 25)
            self.assertTrue(
                set(row["id"] for row in recent_rows).isdisjoint(
                    row["id"] for row in older_rows
                )
            )
        finally:
            with connect() as connection:
                connection.executemany(
                    "DELETE FROM transcripts WHERE filename = ?",
                    [(filename,) for filename in filenames],
                )
                connection.commit()

    def test_archive_uses_recording_time_instead_of_import_order(self):
        filenames = [
            "LateImport/2026-07-27-08-00-00-LateImport.mp3",
            "RecentFeed/2026-07-27-12-00-00-RecentFeed.mp3",
            "AnotherFeed/2026-07-27-11-00-00-AnotherFeed.mp3",
        ]
        try:
            with connect() as connection:
                connection.executemany(
                    """
                    INSERT INTO transcripts(
                        timestamp, recorded_at, filename, transcript_text,
                        raw_transcript_text, quality_score, quality_reason, status
                    ) VALUES (?, ?, ?, ?, ?, 1.0, '', 'ready')
                    """,
                    [
                        (
                            "2026-07-27T08:00:00",
                            "2026-07-27T08:00:00",
                            filenames[0],
                            "recording-order-marker oldest",
                            "recording-order-marker oldest",
                        ),
                        (
                            "2026-07-27T12:00:00",
                            "2026-07-27T12:00:00",
                            filenames[1],
                            "recording-order-marker newest",
                            "recording-order-marker newest",
                        ),
                        (
                            "2026-07-27T11:00:00",
                            "2026-07-27T11:00:00",
                            filenames[2],
                            "recording-order-marker middle",
                            "recording-order-marker middle",
                        ),
                    ],
                )
                connection.commit()

            response = self.client.get(
                "/api/history",
                params={"q": "recording-order-marker", "limit": 3},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                [row["filename"] for row in response.json()],
                [filenames[0], filenames[2], filenames[1]],
            )
        finally:
            with connect() as connection:
                connection.executemany(
                    "DELETE FROM transcripts WHERE filename = ?",
                    [(filename,) for filename in filenames],
                )
                connection.commit()

    def test_console_returns_whitelisted_service_log_tail(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        server_log = os.path.join(LOG_DIR, "server.log")
        with open(server_log, "w", encoding="utf-8") as handle:
            handle.write(
                "2026-07-27 12:00:00,000 - ERROR - console-test failure marker\n"
            )

        response = self.client.get(
            "/api/console",
            params={"service": "server", "lines": 20},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            any(
                "console-test failure marker" in entry["message"]
                and entry["level"] == "error"
                for entry in response.json()["entries"]
            )
        )
        self.assertEqual(
            self.client.get(
                "/api/console",
                params={"service": "../security"},
            ).status_code,
            400,
        )


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        TEST_ROOT.cleanup()
