import base64
import csv
import io
import os
import secrets
import sys
import tempfile
import unittest
import zipfile


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

from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from backend.backups import apply_pending_restore, pending_restore, request_restore
from backend.config import DB_NAME, LOG_DIR, load_settings, save_settings
from backend.database import connect
from backend.maintenance import prune_history
from backend.server import escalate_overdue_alerts
from backend.security import (
    create_session_token,
    ensure_app_key,
    find_user,
    hash_password,
    load_security_config,
    save_security_config,
)


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

    def test_history_paginates_one_channel_without_leaking_other_feeds(self):
        with connect() as connection:
            connection.executemany(
                """
                INSERT INTO transcripts(
                    timestamp, recorded_at, channel, filename,
                    transcript_text, raw_transcript_text,
                    quality_score, quality_reason, status
                ) VALUES (?, ?, ?, ?, ?, ?, 1.0, '', 'ready')
                """,
                [
                    (
                        "2026-08-01T10:00:05",
                        "2026-08-01T10:00:00",
                        "Paging Alpha",
                        "Paging Alpha/2026-08-01-10-00-00-Paging Alpha.mp3",
                        "alpha older message",
                        "alpha older message",
                    ),
                    (
                        "2026-08-01T10:01:05",
                        "2026-08-01T10:01:00",
                        "Paging Bravo",
                        "Paging Bravo/2026-08-01-10-01-00-Paging Bravo.mp3",
                        "bravo message",
                        "bravo message",
                    ),
                    (
                        "2026-08-01T10:02:05",
                        "2026-08-01T10:02:00",
                        "Paging Alpha",
                        "Paging Alpha/2026-08-01-10-02-00-Paging Alpha.mp3",
                        "alpha newer message",
                        "alpha newer message",
                    ),
                ],
            )
            connection.commit()

        newest = self.client.get(
            "/api/history",
            params={"channel": "Paging Alpha", "limit": 1},
        )
        self.assertEqual(newest.status_code, 200)
        self.assertEqual(
            [row["transcript_text"] for row in newest.json()],
            ["alpha newer message"],
        )

        older = self.client.get(
            "/api/history",
            params={
                "channel": "Paging Alpha",
                "before_id": newest.json()[0]["id"],
                "limit": 10,
            },
        )
        self.assertEqual(older.status_code, 200)
        self.assertEqual(
            [row["transcript_text"] for row in older.json()],
            ["alpha older message"],
        )
        self.assertTrue(all(row["channel"] == "Paging Alpha" for row in older.json()))

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
        self.assertEqual(viewer.get("/api/incidents").status_code, 200)
        self.assertEqual(viewer.get("/api/health-center").status_code, 200)
        self.assertEqual(viewer.get("/api/diagnostics.zip").status_code, 403)
        self.assertEqual(
            viewer.post(
                "/api/incidents",
                json={"title": "Viewer must not create this"},
            ).status_code,
            403,
        )
        self.assertEqual(
            viewer.get("/audio/Alpha/2026-07-25-09-59-55-Alpha.mp3").status_code,
            403,
        )

        transcript_id = self.client.get("/api/history").json()[0]["id"]
        version = self.client.get(f"/api/transcripts/{transcript_id}").json()["version"]
        self.assertEqual(
            self.client.patch(
                f"/api/transcripts/{transcript_id}",
                json={"bookmarked": True},
            ).status_code,
            428,
        )
        update = self.client.patch(
            f"/api/transcripts/{transcript_id}",
            json={
                "reviewed": True,
                "bookmarked": True,
                "notes": "Incident reviewed",
                "version": version,
            },
        )
        self.assertEqual(update.status_code, 200)
        self.assertTrue(update.json()["reviewed"])
        self.assertTrue(update.json()["bookmarked"])

    def test_phase_two_review_workflow_history_and_version_conflict(self):
        filename = "PhaseTwo/2026-07-30-12-00-00-review-workflow.mp3"
        with connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO transcripts(
                    timestamp, recorded_at, filename, transcript_text,
                    raw_transcript_text, quality_score, quality_reason, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-07-30T12:00:05",
                    "2026-07-30T12:00:00",
                    filename,
                    "phase two workflow transmission",
                    "phase two workflow transmission",
                    0.92,
                    "",
                    "ready",
                ),
            )
            transcript_id = cursor.lastrowid
            connection.execute(
                """
                INSERT INTO transcript_segments(
                    transcript_id, result_kind, segment_index,
                    start_seconds, end_seconds, text, confidence
                ) VALUES (?, 'primary', 0, 0.0, 1.2, 'hello hello', 0.4)
                """,
                (transcript_id,),
            )
            connection.commit()
        try:
            detail = self.client.get(f"/api/transcripts/{transcript_id}")
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.json()["review_state"], "unreviewed")
            starting_version = detail.json()["version"]

            review = self.client.patch(
                f"/api/transcripts/{transcript_id}",
                json={
                    "review_state": "in_review",
                    "review_resolution": "Confirm the callsign",
                    "notes": "Assigned during the current shift.",
                    "version": starting_version,
                },
            )
            self.assertEqual(review.status_code, 200)
            self.assertEqual(review.json()["review_state"], "in_review")
            self.assertEqual(review.json()["version"], starting_version + 1)
            self.assertEqual(review.json()["reviewed_by"], "admin")

            stale = self.client.patch(
                f"/api/transcripts/{transcript_id}",
                json={"bookmarked": True, "version": starting_version},
            )
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(
                stale.json()["detail"]["current"]["version"],
                starting_version + 1,
            )

            correction = self.client.patch(
                f"/api/transcripts/{transcript_id}",
                json={
                    "transcript_text": "Corrected phase two workflow transmission",
                    "version": review.json()["version"],
                },
            )
            self.assertEqual(correction.status_code, 200)
            self.assertEqual(correction.json()["review_state"], "corrected")
            self.assertTrue(correction.json()["reviewed"])

            refreshed = self.client.get(f"/api/transcripts/{transcript_id}").json()
            self.assertEqual(len(refreshed["history"]), 2)
            self.assertEqual(refreshed["history"][0]["change_type"], "correction")
            self.assertEqual(refreshed["history"][1]["change_type"], "review")
        finally:
            with connect() as connection:
                connection.execute(
                    "DELETE FROM transcript_versions WHERE transcript_id = ?",
                    (transcript_id,),
                )
                connection.execute(
                    "DELETE FROM transcripts WHERE id = ?",
                    (transcript_id,),
                )
                connection.commit()

    def test_phase_two_saved_workspaces_are_server_backed_and_scoped(self):
        personal_name = "Phase Two Personal"
        shared_name = "Phase Two Shared"
        viewer_name = "Phase Two Viewer"
        configuration = {
            "visible_feeds": ["Alpha"],
            "feed_order": ["Alpha", "Bravo"],
            "focused_feed": "Alpha",
            "view_mode": "board",
            "filters": {"query": "medical"},
            "compact": True,
            "alerts_visible": False,
        }
        try:
            personal = self.client.post(
                "/api/workspaces",
                json={
                    "name": personal_name,
                    "configuration": configuration,
                    "is_shared": False,
                },
            )
            self.assertEqual(personal.status_code, 200)
            shared = self.client.post(
                "/api/workspaces",
                json={
                    "name": shared_name,
                    "configuration": configuration,
                    "is_shared": True,
                },
            )
            self.assertEqual(shared.status_code, 200)

            self.assertEqual(
                self.client.post(
                    "/api/users",
                    json={
                        "username": "phase-two-viewer",
                        "display_name": "Phase Two Viewer",
                        "role": "viewer",
                        "password": "phase-two-viewer-password",
                        "active": True,
                    },
                ).status_code,
                200,
            )
            viewer = TestClient(app, base_url="https://127.0.0.1")
            self.assertEqual(
                viewer.post(
                    "/api/login",
                    json={
                        "username": "phase-two-viewer",
                        "password": "phase-two-viewer-password",
                    },
                ).status_code,
                200,
            )
            visible = viewer.get("/api/workspaces")
            self.assertEqual(visible.status_code, 200)
            self.assertEqual(
                [workspace["name"] for workspace in visible.json()],
                [shared_name],
            )
            forbidden = viewer.post(
                "/api/workspaces",
                json={
                    "name": viewer_name,
                    "configuration": configuration,
                    "is_shared": True,
                },
            )
            self.assertEqual(forbidden.status_code, 403)
            own = viewer.post(
                "/api/workspaces",
                json={
                    "name": viewer_name,
                    "configuration": configuration,
                    "is_shared": False,
                },
            )
            self.assertEqual(own.status_code, 200)
            self.assertEqual(
                viewer.delete(f"/api/workspaces/{shared.json()['id']}").status_code,
                403,
            )
        finally:
            with connect() as connection:
                connection.execute(
                    """
                    DELETE FROM saved_workspaces
                    WHERE name IN (?, ?, ?)
                    """,
                    (personal_name, shared_name, viewer_name),
                )
                connection.commit()

    def test_phase_three_fts_search_filters_snippets_and_cursor(self):
        rows = (
            (
                "2024-06-01T10:00:05",
                "2024-06-01T10:00:00",
                2024,
                "Medical",
                "Medical/2024-06-01-10-00-00-medical.mp3",
                "medical response alpha at the north gate",
                "mlx-community/whisper-medium-mlx",
            ),
            (
                "2025-06-01T11:00:05",
                "2025-06-01T11:00:00",
                2025,
                "Security",
                "Security/2025-06-01-11-00-00-security.mp3",
                "security perimeter bravo at the south gate",
                "mlx-community/whisper-large-v3-mlx",
            ),
            (
                "2026-06-01T12:00:05",
                "2026-06-01T12:00:00",
                2026,
                "Medical",
                "Medical/2026-06-01-12-00-00-medical.mp3",
                "medical response alpha at the east gate",
                "mlx-community/whisper-medium-mlx",
            ),
        )
        with connect() as connection:
            connection.executemany(
                """
                INSERT INTO transcripts(
                    timestamp, recorded_at, recording_year, channel,
                    filename, transcript_text, raw_transcript_text,
                    quality_score, quality_reason, status, transcription_model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1.0, '', 'ready', ?)
                """,
                [(*row[:6], row[5], row[6]) for row in rows],
            )
            connection.commit()
        try:
            phrase = self.client.get(
                "/api/search",
                params={"q": '"north gate"', "channel": "Medical"},
            )
            self.assertEqual(phrase.status_code, 200)
            self.assertEqual(phrase.json()["count"], 1)
            self.assertIn("⟦north gate⟧", phrase.json()["items"][0]["snippet"])

            prefix = self.client.get(
                "/api/search",
                params={"q": "medic*", "channel": "Medical"},
            )
            self.assertEqual(prefix.status_code, 200)
            self.assertEqual(prefix.json()["count"], 2)

            filtered = self.client.get(
                "/api/search",
                params={
                    "q": "medical",
                    "year": 2024,
                    "model": "mlx-community/whisper-medium-mlx",
                },
            )
            self.assertEqual(filtered.status_code, 200)
            self.assertEqual(filtered.json()["count"], 1)
            self.assertEqual(filtered.json()["items"][0]["recording_year"], 2024)

            first_page = self.client.get(
                "/api/search",
                params={"q": "medical", "sort": "recent", "limit": 1},
            ).json()
            self.assertEqual(len(first_page["items"]), 1)
            self.assertIsNotNone(first_page["next_cursor"])
            second_page = self.client.get(
                "/api/search",
                params={
                    "q": "medical",
                    "sort": "recent",
                    "limit": 1,
                    "cursor": first_page["next_cursor"],
                },
            )
            self.assertEqual(second_page.status_code, 200)
            self.assertNotEqual(
                first_page["items"][0]["id"],
                second_page.json()["items"][0]["id"],
            )
            self.assertEqual(
                self.client.get(
                    "/api/search",
                    params={"q": "medical", "cursor": "invalid"},
                ).status_code,
                400,
            )

            # A malformed time window must be reported, not silently applied as
            # a text comparison that quietly matches nothing.
            for parameter, value in (
                ("start", "NOTATIME"),
                ("start", "24:00"),
                ("start", "12:60"),
                ("start", "5"),
                ("end", "zz"),
            ):
                rejected = self.client.get(
                    "/api/search",
                    params={"q": "medical", parameter: value},
                )
                self.assertEqual(rejected.status_code, 400, f"{parameter}={value}")
                self.assertIn("time", rejected.json()["detail"])
            for parameter, value in (("start", "00:00"), ("end", "23:59")):
                accepted = self.client.get(
                    "/api/search",
                    params={"q": "medical", parameter: value},
                )
                self.assertEqual(accepted.status_code, 200, f"{parameter}={value}")

            target = filtered.json()["items"][0]
            note_update = self.client.patch(
                f"/api/transcripts/{target['id']}",
                json={
                    "notes": "handoff beacon phase three",
                    "version": target["version"],
                },
            )
            self.assertEqual(note_update.status_code, 200)
            notes_search = self.client.get(
                "/api/search",
                params={"q": '"handoff beacon"'},
            )
            self.assertEqual(notes_search.status_code, 200)
            self.assertEqual(notes_search.json()["count"], 1)

            years = self.client.get("/api/archive/years")
            self.assertEqual(years.status_code, 200)
            year_values = {item["year"] for item in years.json()["years"]}
            self.assertTrue({2024, 2025, 2026}.issubset(year_values))
            facets = self.client.get("/api/archive/facets").json()
            self.assertIn("Medical", {item["value"] for item in facets["channels"]})
        finally:
            with connect() as connection:
                connection.executemany(
                    "DELETE FROM transcripts WHERE filename = ?",
                    [(row[4],) for row in rows],
                )
                connection.commit()

    def test_phase_three_preferences_and_saved_searches_are_server_backed(self):
        search_name = "Medical North Gate"
        configuration = {
            "query": '"north gate"',
            "filters": {"channel": "Medical", "year": 2024},
            "sort": "relevance",
        }
        try:
            preferences = self.client.put(
                "/api/preferences",
                json={
                    "configuration": {
                        "search_sort": "recent",
                        "search_page_size": 25,
                        "default_search_filters": {"channel": "Medical"},
                    }
                },
            )
            self.assertEqual(preferences.status_code, 200)
            loaded_preferences = self.client.get("/api/preferences")
            self.assertEqual(
                loaded_preferences.json()["configuration"]["search_sort"],
                "recent",
            )
            self.assertEqual(
                self.client.put(
                    "/api/preferences",
                    json={"configuration": {"unsupported": True}},
                ).status_code,
                400,
            )

            saved = self.client.post(
                "/api/saved-searches",
                json={"name": search_name, "configuration": configuration},
            )
            self.assertEqual(saved.status_code, 200)
            listed = self.client.get("/api/saved-searches")
            self.assertIn(search_name, [item["name"] for item in listed.json()])
            self.assertEqual(
                self.client.delete(
                    f"/api/saved-searches/{saved.json()['id']}"
                ).status_code,
                200,
            )
        finally:
            with connect() as connection:
                connection.execute(
                    "DELETE FROM saved_searches WHERE name = ?",
                    (search_name,),
                )
                connection.execute(
                    "DELETE FROM user_preferences WHERE username = 'admin'"
                )
                connection.commit()

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
            readiness = fresh_client.get("/api/setup/status")
            self.assertEqual(readiness.status_code, 200)
            self.assertEqual(len(readiness.json()["models"]), 2)
            configured = fresh_client.post(
                "/api/setup/configure",
                json={
                    "source_dir": SOURCE_DIR,
                    "transcription_enabled": False,
                    "minimum_free_gb": 2,
                    "setup_step": 3,
                },
            )
            self.assertEqual(configured.status_code, 200)
            self.assertTrue(configured.json()["source"]["readable"])
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
        with connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT id, filename, transcript_text, timestamp, recorded_at,
                       quality_score, quality_reason, status
                FROM transcripts
                WHERE filename = 'Alpha/2026-07-25-09-59-55-Alpha.mp3'
                """
            ).fetchone()
        payload = dict(row)
        self.assertEqual(self.client.post("/api/new_transcript", json=payload).status_code, 403)
        delivered = self.client.post(
            "/api/new_transcript",
            json=payload,
            headers={"X-Radio-Internal-Token": "test-internal-token"},
        )
        self.assertEqual(delivered.status_code, 200)
        self.assertIn("event_id", delivered.json())

    def test_phase_four_alert_rules_inbox_events_and_preferences(self):
        filename = "Medical/2026-07-30-16-00-00-phase-four-alert.mp3"
        original_config = load_security_config()
        operator = TestClient(app, base_url="https://127.0.0.1")
        rule_id = None
        transcript_id = None
        try:
            created_rule = self.client.post(
                "/api/alert-rules",
                json={
                    "name": "Phase Four Distress Beacon",
                    "description": "Integration-only alert rule",
                    "severity": "critical",
                    "match_mode": "phrase",
                    "terms": ["phase four distress beacon"],
                    "exclusions": ["training"],
                    "channels": ["Medical"],
                    "minimum_quality": 0.5,
                    "cooldown_seconds": 0,
                    "requires_ack": True,
                    "escalation_seconds": 60,
                    "active": True,
                },
            )
            self.assertEqual(created_rule.status_code, 200)
            rule = created_rule.json()
            rule_id = rule["id"]

            test_match = self.client.post(
                f"/api/alert-rules/{rule_id}/test",
                json={
                    "transcript_text": "Phase four distress beacon at the west gate",
                    "channel": "Medical",
                    "quality_score": 0.95,
                },
            )
            self.assertEqual(test_match.status_code, 200)
            self.assertTrue(test_match.json()["matched"])
            excluded = self.client.post(
                f"/api/alert-rules/{rule_id}/test",
                json={
                    "transcript_text": "Phase four distress beacon training only",
                    "channel": "Medical",
                    "quality_score": 0.95,
                },
            )
            self.assertFalse(excluded.json()["matched"])

            with connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO transcripts(
                        timestamp, recorded_at, recording_year, channel,
                        filename, transcript_text, raw_transcript_text,
                        quality_score, quality_reason, status
                    ) VALUES (?, ?, 2026, 'Medical', ?, ?, ?, 0.95, '', 'ready')
                    """,
                    (
                        "2026-07-30T16:00:05",
                        "2026-07-30T16:00:00",
                        filename,
                        "Phase four distress beacon at the west gate",
                        "Phase four distress beacon at the west gate",
                    ),
                )
                transcript_id = cursor.lastrowid
                connection.commit()
            delivery_payload = {
                "id": transcript_id,
                "filename": filename,
                "transcript_text": "Phase four distress beacon at the west gate",
                "timestamp": "2026-07-30T16:00:05",
                "recorded_at": "2026-07-30T16:00:00",
                "quality_score": 0.95,
                "quality_reason": "",
                "status": "ready",
            }
            delivered = self.client.post(
                "/api/new_transcript",
                json=delivery_payload,
                headers={"X-Radio-Internal-Token": "test-internal-token"},
            )
            self.assertEqual(delivered.status_code, 200)
            self.assertEqual(delivered.json()["alerts_created"], 1)
            repeated = self.client.post(
                "/api/new_transcript",
                json=delivery_payload,
                headers={"X-Radio-Internal-Token": "test-internal-token"},
            )
            self.assertTrue(repeated.json()["duplicate"])
            self.assertEqual(repeated.json()["alerts_created"], 0)

            inbox = self.client.get(
                "/api/alerts",
                params={"status": "all"},
            )
            self.assertEqual(inbox.status_code, 200)
            alert = next(
                item
                for item in inbox.json()["items"]
                if item["rule_id"] == rule_id
                and item["transcript_id"] == transcript_id
            )
            self.assertEqual(alert["severity"], "critical")
            self.assertEqual(alert["status"], "open")
            self.assertIn("Phase Four Distress Beacon", alert["explanation"])

            self.assertEqual(
                self.client.post(
                    "/api/users",
                    json={
                        "username": "phase-four-alert-operator",
                        "display_name": "Alert Operator",
                        "role": "operator",
                        "password": "phase-four-alert-password",
                        "active": True,
                    },
                ).status_code,
                200,
            )
            self.assertEqual(
                operator.post(
                    "/api/login",
                    json={
                        "username": "phase-four-alert-operator",
                        "password": "phase-four-alert-password",
                    },
                ).status_code,
                200,
            )
            self.assertEqual(
                operator.patch(
                    f"/api/alerts/{alert['id']}",
                    json={
                        "status": "false_positive",
                        "version": alert["version"],
                    },
                ).status_code,
                403,
            )

            acknowledged = self.client.patch(
                f"/api/alerts/{alert['id']}",
                json={"status": "acknowledged", "version": alert["version"]},
            )
            self.assertEqual(acknowledged.status_code, 200)
            self.assertEqual(acknowledged.json()["acknowledged_by"], "admin")
            stale = self.client.patch(
                f"/api/alerts/{alert['id']}",
                json={"status": "resolved", "version": alert["version"]},
            )
            self.assertEqual(stale.status_code, 409)
            resolved = self.client.patch(
                f"/api/alerts/{alert['id']}",
                json={
                    "status": "resolved",
                    "resolution_note": "Response complete",
                    "version": acknowledged.json()["version"],
                },
            )
            self.assertEqual(resolved.status_code, 200)
            self.assertEqual(resolved.json()["status"], "resolved")

            events = self.client.get("/api/events", params={"after_id": 0})
            self.assertEqual(events.status_code, 200)
            event_types = {item["type"] for item in events.json()["items"]}
            self.assertTrue(
                {
                    "alert_rule.created",
                    "transcript.created",
                    "alert.created",
                    "alert.updated",
                }.issubset(event_types)
            )

            preferences = self.client.put(
                "/api/notification-preferences",
                json={
                    "configuration": {
                        "browser_enabled": True,
                        "minimum_severity": "urgent",
                        "sound_enabled": False,
                    }
                },
            )
            self.assertEqual(preferences.status_code, 200)
            self.assertEqual(
                self.client.get("/api/notification-preferences")
                .json()["configuration"]["minimum_severity"],
                "urgent",
            )
            self.assertEqual(
                self.client.put(
                    "/api/notification-preferences",
                    json={"configuration": {"unknown": True}},
                ).status_code,
                400,
            )
        finally:
            operator.close()
            save_security_config(original_config)
            with connect() as connection:
                if transcript_id is not None:
                    alert_ids = [
                        row["id"]
                        for row in connection.execute(
                            "SELECT id FROM alert_events WHERE transcript_id = ?",
                            (transcript_id,),
                        ).fetchall()
                    ]
                    for alert_id in alert_ids:
                        connection.execute(
                            "DELETE FROM alert_acknowledgements WHERE alert_id = ?",
                            (alert_id,),
                        )
                    connection.execute(
                        "DELETE FROM alert_events WHERE transcript_id = ?",
                        (transcript_id,),
                    )
                    connection.execute(
                        "DELETE FROM transcripts WHERE id = ?",
                        (transcript_id,),
                    )
                if rule_id is not None:
                    connection.execute(
                        "DELETE FROM alert_rules WHERE id = ?",
                        (rule_id,),
                    )
                connection.commit()

    def test_phase_four_unique_presence_and_live_mutation_replay(self):
        original_config = load_security_config()
        filename = "Operations/2026-07-30-17-00-00-live-collaboration.mp3"
        transcript_id = None
        operator = TestClient(app, base_url="https://127.0.0.1")

        def receive_type(socket, event_type, attempts=20):
            for _ in range(attempts):
                event = socket.receive_json()
                if event.get("type") == event_type:
                    return event
            self.fail(f"Did not receive {event_type}")

        try:
            self.assertEqual(
                self.client.post(
                    "/api/users",
                    json={
                        "username": "phase-four-operator",
                        "display_name": "Phase Four Operator",
                        "role": "operator",
                        "password": "phase-four-password",
                        "active": True,
                    },
                ).status_code,
                200,
            )
            self.assertEqual(
                operator.post(
                    "/api/login",
                    json={
                        "username": "phase-four-operator",
                        "password": "phase-four-password",
                    },
                ).status_code,
                200,
            )
            with connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO transcripts(
                        timestamp, recorded_at, recording_year, channel,
                        filename, transcript_text, raw_transcript_text,
                        quality_score, quality_reason, status
                    ) VALUES (
                        '2026-07-30T17:00:05', '2026-07-30T17:00:00',
                        2026, 'Operations', ?, 'routine collaboration marker',
                        'routine collaboration marker', 1.0, '', 'ready'
                    )
                    """,
                    (filename,),
                )
                transcript_id = cursor.lastrowid
                high_watermark = connection.execute(
                    "SELECT coalesce(max(id), 0) FROM events"
                ).fetchone()[0]
                connection.commit()

            with self.client.websocket_connect(
                f"wss://127.0.0.1/ws?after_event_id={high_watermark}"
            ) as admin_socket:
                first_presence = receive_type(admin_socket, "presence.changed")
                self.assertEqual(first_presence["payload"]["active_users"], 1)
                with operator.websocket_connect(
                    f"wss://127.0.0.1/ws?after_event_id={high_watermark}"
                ) as operator_socket:
                    operator_presence = receive_type(
                        operator_socket,
                        "presence.changed",
                    )
                    self.assertEqual(
                        operator_presence["payload"]["active_users"],
                        2,
                    )
                    presence = self.client.get("/api/presence").json()
                    self.assertEqual(presence["active_users"], 2)
                    self.assertEqual(len(presence["users"]), 2)

                    detail = self.client.get(
                        f"/api/transcripts/{transcript_id}"
                    ).json()
                    update = self.client.patch(
                        f"/api/transcripts/{transcript_id}",
                        json={
                            "notes": "shared live mutation",
                            "version": detail["version"],
                        },
                    )
                    self.assertEqual(update.status_code, 200)
                    admin_event = receive_type(
                        admin_socket,
                        "transcript.updated",
                    )
                    operator_event = receive_type(
                        operator_socket,
                        "transcript.updated",
                    )
                    self.assertEqual(
                        admin_event["payload"]["transcript"]["notes"],
                        "shared live mutation",
                    )
                    self.assertEqual(
                        operator_event["event_id"],
                        admin_event["event_id"],
                    )

            replay = operator.get(
                "/api/events",
                params={"after_id": high_watermark},
            )
            replayed = [
                event
                for event in replay.json()["items"]
                if event["type"] == "transcript.updated"
                and event["resource_id"] == transcript_id
            ]
            self.assertEqual(len(replayed), 1)
        finally:
            operator.close()
            save_security_config(original_config)
            if transcript_id is not None:
                with connect() as connection:
                    connection.execute(
                        "DELETE FROM transcript_versions WHERE transcript_id = ?",
                        (transcript_id,),
                    )
                    connection.execute(
                        "DELETE FROM transcripts WHERE id = ?",
                        (transcript_id,),
                    )
                    connection.commit()

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
            self.assertGreaterEqual(
                int(recent.headers["X-Radio-High-Watermark"]),
                max(row["id"] for row in recent_rows),
            )

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

    def test_archive_cursor_falls_back_to_import_time_for_legacy_rows(self):
        filenames = [
            f"Legacy/legacy-pagination-{index}.mp3"
            for index in range(3)
        ]
        try:
            with connect() as connection:
                connection.executemany(
                    """
                    INSERT INTO transcripts(
                        timestamp, recorded_at, filename, transcript_text,
                        raw_transcript_text, quality_score, quality_reason, status
                    ) VALUES (?, NULL, ?, ?, ?, 1.0, '', 'ready')
                    """,
                    [
                        (
                            f"2026-07-20T10:0{index}:00",
                            filename,
                            f"legacy-cursor-marker {index}",
                            f"legacy-cursor-marker {index}",
                        )
                        for index, filename in enumerate(filenames)
                    ],
                )
                connection.commit()

            recent = self.client.get(
                "/api/history",
                params={"q": "legacy-cursor-marker", "limit": 2},
            ).json()
            older = self.client.get(
                "/api/history",
                params={
                    "q": "legacy-cursor-marker",
                    "limit": 2,
                    "before_id": recent[0]["id"],
                },
            ).json()
            self.assertEqual(len(recent), 2)
            self.assertEqual(len(older), 1)
            self.assertTrue(
                {row["id"] for row in recent}.isdisjoint(
                    {row["id"] for row in older}
                )
            )
        finally:
            with connect() as connection:
                connection.executemany(
                    "DELETE FROM transcripts WHERE filename = ?",
                    [(filename,) for filename in filenames],
                )
                connection.commit()

    def test_export_streams_every_matching_row_without_legacy_cap(self):
        row_count = 2105
        filenames = [
            f"FullExport/full-export-{index:04d}.mp3"
            for index in range(row_count)
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
                            f"2026-07-22T{(index // 3600) % 24:02d}:"
                            f"{(index // 60) % 60:02d}:{index % 60:02d}",
                            f"2026-07-22T{(index // 3600) % 24:02d}:"
                            f"{(index // 60) % 60:02d}:{index % 60:02d}",
                            filename,
                            f"full-export-marker transmission {index}",
                            f"full-export-marker transmission {index}",
                        )
                        for index, filename in enumerate(filenames)
                    ],
                )
                connection.commit()

            count_response = self.client.get(
                "/api/export/count",
                params={"q": "full-export-marker"},
            )
            self.assertEqual(count_response.status_code, 200)
            self.assertEqual(count_response.json()["count"], row_count)
            export_high_watermark = count_response.json()["through_id"]

            with connect() as connection:
                connection.execute(
                    """
                    INSERT INTO transcripts(
                        timestamp, recorded_at, filename, transcript_text,
                        raw_transcript_text, quality_score, quality_reason, status
                    ) VALUES (?, ?, ?, ?, ?, 1.0, '', 'ready')
                    """,
                    (
                        "2026-07-22T23:59:59",
                        "2026-07-22T23:59:59",
                        "FullExport/after-count.mp3",
                        "full-export-marker arrived after count",
                        "full-export-marker arrived after count",
                    ),
                )
                connection.commit()

            response = self.client.get(
                "/api/export.csv",
                params={
                    "q": "full-export-marker",
                    "through_id": export_high_watermark,
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                int(response.headers["X-Radio-Export-Count"]),
                row_count,
            )
            self.assertEqual(
                int(response.headers["X-Radio-Export-Through-Id"]),
                export_high_watermark,
            )
            exported_rows = list(csv.reader(io.StringIO(response.text)))
            self.assertEqual(len(exported_rows), row_count + 1)
            self.assertEqual(exported_rows[0][0], "Recorded")
            self.assertIn("transmission 0", exported_rows[1][2])
            self.assertIn(
                f"transmission {row_count - 1}",
                exported_rows[-1][2],
            )
        finally:
            with connect() as connection:
                connection.execute(
                    "DELETE FROM transcripts WHERE filename = ?",
                    ("FullExport/after-count.mp3",),
                )
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

    def test_incident_workflow_handoff_health_and_diagnostics(self):
        transcript_id = self.client.get("/api/history").json()[0]["id"]
        created = self.client.post(
            "/api/incidents",
            json={
                "title": "North gate response",
                "severity": "urgent",
                "status": "active",
                "owner_username": "admin",
                "location": "North gate",
                "summary": "Coordinate medical access and gate control.",
                "tags": ["medical", "gate"],
            },
        )
        self.assertEqual(created.status_code, 200)
        incident = created.json()
        incident_id = incident["id"]
        try:
            linked = self.client.post(
                f"/api/incidents/{incident_id}/transcripts",
                json={"transcript_id": transcript_id},
            )
            self.assertEqual(linked.status_code, 200)
            self.assertEqual(linked.json()["transcript_count"], 1)

            task = self.client.post(
                f"/api/incidents/{incident_id}/tasks",
                json={"description": "Confirm responder arrival", "assigned_to": "admin"},
            )
            self.assertEqual(task.status_code, 200)
            completed = self.client.patch(
                f"/api/incident-tasks/{task.json()['id']}",
                json={"status": "done", "version": task.json()["version"]},
            )
            self.assertEqual(completed.status_code, 200)
            self.assertEqual(completed.json()["status"], "done")

            note = self.client.post(
                f"/api/incidents/{incident_id}/notes",
                json={"note": "Medic unit is approaching from the service road."},
            )
            self.assertEqual(note.status_code, 200)
            detail = self.client.get(f"/api/incidents/{incident_id}").json()
            self.assertEqual(len(detail["transcripts"]), 1)
            self.assertEqual(len(detail["tasks"]), 1)
            self.assertEqual(len(detail["notes"]), 1)

            update_payload = {
                "title": incident["title"],
                "severity": incident["severity"],
                "status": "stabilized",
                "owner_username": incident["owner_username"],
                "location": incident["location"],
                "summary": incident["summary"],
                "tags": incident["tags"],
                "started_at": incident["started_at"],
                "version": incident["version"],
            }
            updated = self.client.patch(
                f"/api/incidents/{incident_id}", json=update_payload
            )
            self.assertEqual(updated.status_code, 200)
            self.assertEqual(updated.json()["status"], "stabilized")
            stale = self.client.patch(
                f"/api/incidents/{incident_id}", json=update_payload
            )
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(stale.json()["current"]["version"], updated.json()["version"])

            handoff = self.client.get("/api/shift-handoff", params={"hours": 12})
            self.assertEqual(handoff.status_code, 200)
            self.assertTrue(
                any(item["id"] == incident_id for item in handoff.json()["incidents"])
            )
            handoff_text = self.client.get(
                "/api/shift-handoff", params={"hours": 12, "format": "text"}
            )
            self.assertIn("North gate response", handoff_text.text)

            health = self.client.get("/api/health-center")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(
                [item["id"] for item in health.json()["components"]],
                ["source", "sync", "transcription", "delivery", "storage"],
            )

            # Delivery runs inside the transcription worker, so pausing
            # transcription strands anything already finished. That must read as
            # a deliberate pause rather than sending an operator to troubleshoot
            # a dashboard server that is working fine.
            from backend.config import load_settings, save_settings
            from backend.database import connect as db_connect

            with db_connect() as connection:
                connection.execute(
                    """
                    UPDATE transcripts SET broadcast_pending = 1
                    WHERE id = (SELECT max(id) FROM transcripts WHERE status = 'ready')
                    """
                )
                connection.commit()
            previous = load_settings().get("transcription_enabled", True)
            save_settings({"transcription_enabled": False})
            try:
                paused = [
                    item
                    for item in self.client.get("/api/health-center").json()["components"]
                    if item["id"] == "delivery"
                ][0]
                self.assertEqual(paused["status"], "paused")
                self.assertGreaterEqual(paused["queue_depth"], 1)
                self.assertNotIn("Verify the dashboard server", paused["action"])
                self.assertIn("transcription", paused["summary"].lower())

                save_settings({"transcription_enabled": True})
                running = [
                    item
                    for item in self.client.get("/api/health-center").json()["components"]
                    if item["id"] == "delivery"
                ][0]
                self.assertEqual(running["status"], "caution")
                self.assertIn("Verify the dashboard server", running["action"])
            finally:
                save_settings({"transcription_enabled": previous})
                with db_connect() as connection:
                    connection.execute("UPDATE transcripts SET broadcast_pending = 0")
                    connection.commit()

            diagnostics = self.client.get("/api/diagnostics.zip")
            self.assertEqual(diagnostics.status_code, 200)
            with zipfile.ZipFile(io.BytesIO(diagnostics.content)) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {
                        "diagnostics.json",
                        "health-center.json",
                        "shift-handoff.txt",
                        "logs/server.log",
                        "logs/worker.log",
                        "logs/sync.log",
                    },
                )
                self.assertIn(
                    '"database_quick_check": "ok"',
                    archive.read("diagnostics.json").decode("utf-8"),
                )
                self.assertNotIn(
                    "medical requested at north gate",
                    archive.read("shift-handoff.txt").decode("utf-8"),
                )
        finally:
            with connect() as connection:
                connection.execute(
                    "DELETE FROM incident_notes WHERE incident_id = ?", (incident_id,)
                )
                connection.execute(
                    "DELETE FROM incident_tasks WHERE incident_id = ?", (incident_id,)
                )
                connection.execute(
                    "DELETE FROM incident_transcripts WHERE incident_id = ?", (incident_id,)
                )
                connection.execute("DELETE FROM incidents WHERE id = ?", (incident_id,))
                connection.commit()


    def test_phase_six_model_queue_glossary_and_manual_selection(self):
        with connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO transcripts(
                    timestamp, recorded_at, channel, filename, transcript_text,
                    raw_transcript_text, quality_score, quality_reason,
                    quality_metrics, status, transcription_model,
                    primary_quality_score, primary_quality_reason,
                    primary_quality_metrics, primary_status, primary_model,
                    retry_transcript_text, retry_model, retry_quality_score,
                    retry_quality_reason, retry_quality_metrics, retry_status,
                    retry_trigger_reason, selection_reason, selected_transcript
                ) VALUES (
                    '2026-08-01T10:00:00', '2026-08-01T09:59:55', 'Medical',
                    'Medical/phase-six-selection.wav',
                    'Clear message from the west checkpoint',
                    'hello hello hello hello hello', 0.95, '', '{}', 'ready',
                    'mlx-community/whisper-large-v3-mlx',
                    0.1, 'One word dominates the transcript', '{}', 'suspect',
                    'mlx-community/whisper-medium-mlx',
                    'Clear message from the west checkpoint',
                    'mlx-community/whisper-large-v3-mlx', 0.95, '', '{}',
                    'selected', 'Large V3 retry requested because repetition',
                    'Large V3 selected automatically', 'retry'
                )
                """
            )
            transcript_id = cursor.lastrowid
            connection.execute(
                """
                INSERT INTO transcript_segments(
                    transcript_id, result_kind, segment_index,
                    start_seconds, end_seconds, text, confidence
                ) VALUES (?, 'primary', 0, 0.0, 1.2, 'hello hello', 0.4)
                """,
                (transcript_id,),
            )
            connection.commit()

        models = self.client.get("/api/models/status")
        self.assertEqual(models.status_code, 200)
        self.assertEqual(
            {item["model_key"] for item in models.json()["models"]},
            {"medium", "large-v3"},
        )

        glossary = self.client.put(
            "/api/glossaries/Medical",
            json={"terms": ["Alpha One", "North Gate"], "language": "en"},
        )
        self.assertEqual(glossary.status_code, 200)
        self.assertEqual(glossary.json()["terms"], ["Alpha One", "North Gate"])
        self.assertIn(
            "Medical",
            self.client.get("/api/glossaries").json()["channels"],
        )

        detail = self.client.get(f"/api/transcripts/{transcript_id}").json()
        self.assertEqual(detail["segments"]["primary"][0]["start_seconds"], 0.0)
        selected = self.client.patch(
            f"/api/transcripts/{transcript_id}/selection",
            json={"selection": "primary", "version": detail["version"]},
        )
        self.assertEqual(selected.status_code, 200)
        self.assertEqual(selected.json()["selected_transcript"], "primary")
        self.assertIn("hello hello", selected.json()["transcript_text"])
        self.assertIn("selected manually", selected.json()["selection_reason"])
        stale = self.client.patch(
            f"/api/transcripts/{transcript_id}/selection",
            json={"selection": "retry", "version": detail["version"]},
        )
        self.assertEqual(stale.status_code, 409)

    def test_phase_seven_backup_verification_and_restore_drill(self):
        created = self.client.post("/api/backups")
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["status"], "verified")
        backup_id = created.json()["id"]
        verified = self.client.post(f"/api/backups/{backup_id}/verify")
        self.assertEqual(verified.status_code, 200)
        drill = self.client.post(f"/api/backups/{backup_id}/restore-drill")
        self.assertEqual(drill.status_code, 200)
        self.assertEqual(drill.json()["status"], "passed")
        self.assertGreater(drill.json()["transcript_count"], 0)
        analytics = self.client.get("/api/quality-analytics")
        self.assertEqual(analytics.status_code, 200)
        self.assertIn("evaluation_gate", analytics.json()["analytics"])
        normalized = self.client.put(
            "/api/transcription-profiles/Medical",
            json={
                "normalize_audio": True,
                "denoise_audio": False,
                "language_mode": "explicit",
                "experimental_enabled": False,
            },
        )
        self.assertEqual(normalized.status_code, 200)
        self.assertTrue(normalized.json()["normalize_audio"])
        gated = self.client.put(
            "/api/transcription-profiles/Medical",
            json={
                "normalize_audio": True,
                "denoise_audio": True,
                "language_mode": "detect",
                "experimental_enabled": True,
            },
        )
        self.assertEqual(gated.status_code, 400)


class NativeAppSignInTests(unittest.TestCase):
    """The Mac app signs itself in with a key only this account can read."""

    def setUp(self):
        self.client = TestClient(app, base_url="https://127.0.0.1")
        self.app_key = ensure_app_key()

    def test_app_key_is_accepted_without_any_password(self):
        response = self.client.get(
            "/api/me", cookies={"radio_app_key": self.app_key}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["auth_source"], "app")
        self.assertEqual(response.json()["role"], "admin")

    def test_browser_without_the_key_still_has_to_sign_in(self):
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        self.assertEqual(
            self.client.get("/api/me", cookies={"radio_app_key": "wrong"}).status_code,
            401,
        )

    def test_cross_site_requests_may_not_borrow_the_key(self):
        response = self.client.get(
            "/api/me",
            cookies={"radio_app_key": self.app_key},
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(response.status_code, 401)

    def test_the_login_page_states_the_real_session_length(self):
        save_settings({"session_lifetime_hours": 12})
        try:
            page = self.client.get("/login", headers={"Accept": "text/html"})
            self.assertEqual(page.status_code, 200)
            self.assertIn("Stays signed in for 12 hours", page.text)
            self.assertNotIn("{{SESSION_LIFETIME}}", page.text)
        finally:
            save_settings({"session_lifetime_hours": 720})
        page = self.client.get("/login", headers={"Accept": "text/html"})
        self.assertIn("Stays signed in for 1 month", page.text)

    def test_the_app_is_never_shown_the_login_page(self):
        response = self.client.get(
            "/login",
            cookies={"radio_app_key": self.app_key},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/")

    def test_signing_out_browsers_leaves_the_app_signed_in(self):
        browser = TestClient(app, base_url="https://127.0.0.1")
        self.assertEqual(
            browser.post(
                "/api/login",
                json={"username": "admin", "password": "admin-password"},
            ).status_code,
            200,
        )
        self.assertEqual(browser.get("/api/me").status_code, 200)

        reset = self.client.post(
            "/api/security/sign-out-browsers",
            cookies={"radio_app_key": self.app_key},
        )
        self.assertEqual(reset.status_code, 200)

        self.assertEqual(browser.get("/api/me").status_code, 401)
        self.assertEqual(
            self.client.get("/api/me", cookies={"radio_app_key": self.app_key}).status_code,
            200,
        )

    def test_automatic_sign_in_can_be_switched_off(self):
        save_settings({"app_auto_sign_in": False})
        try:
            response = self.client.get(
                "/api/me", cookies={"radio_app_key": self.app_key}
            )
            self.assertEqual(response.status_code, 401)
        finally:
            save_settings({"app_auto_sign_in": True})


class SessionLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app, base_url="https://127.0.0.1")

    def test_signing_in_issues_a_long_lived_cookie(self):
        response = self.client.post(
            "/api/login",
            json={"username": "admin", "password": "admin-password"},
        )
        self.assertEqual(response.status_code, 200)
        cookie_header = response.headers["set-cookie"]
        self.assertIn("Max-Age=", cookie_header)
        max_age = int(cookie_header.split("Max-Age=")[1].split(";")[0])
        self.assertGreaterEqual(max_age, 24 * 3600)

    def test_a_session_past_half_its_life_is_renewed_in_place(self):
        user = find_user("admin")
        # Two hours of a four-hour session remain, so the next request renews it.
        save_settings({"session_lifetime_hours": 4})
        try:
            token = create_session_token(user, lifetime_seconds=2 * 3600)
            response = self.client.get("/api/me", cookies={"radio_session": token})
            self.assertEqual(response.status_code, 200)
            self.assertIn("set-cookie", response.headers)
            self.assertIn("radio_session=", response.headers["set-cookie"])
        finally:
            save_settings({"session_lifetime_hours": 720})

    def test_a_fresh_session_is_left_alone(self):
        user = find_user("admin")
        save_settings({"session_lifetime_hours": 4})
        try:
            token = create_session_token(user)
            response = self.client.get("/api/me", cookies={"radio_session": token})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("set-cookie", response.headers)
        finally:
            save_settings({"session_lifetime_hours": 720})


class SettingsApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app, base_url="https://127.0.0.1")
        self.assertEqual(
            self.client.post(
                "/api/login",
                json={"username": "admin", "password": "admin-password"},
            ).status_code,
            200,
        )

    def test_settings_describe_every_group_with_current_values(self):
        response = self.client.get("/api/settings")
        self.assertEqual(response.status_code, 200)
        document = response.json()
        group_ids = {group["id"] for group in document["groups"]}
        self.assertEqual(
            group_ids, {"recording", "transcription", "access", "storage"}
        )
        keys = {
            field["key"]
            for group in document["groups"]
            for field in group["fields"]
        }
        self.assertIn("source_dir", keys)
        self.assertIn("session_lifetime_hours", keys)
        self.assertIn("minimum_free_bytes", keys)
        self.assertIn("dashboard_url", document["service"])
        self.assertIn("database", document["paths"])

    def test_saving_a_value_stores_it_and_reports_the_restart_scope(self):
        response = self.client.put(
            "/api/settings", json={"values": {"sync_poll_seconds": 25, "port": 8443}}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["restart_required"], "services")
        self.assertEqual(load_settings()["sync_poll_seconds"], 25)
        self.assertEqual(load_settings()["port"], 8443)
        restored = self.client.put(
            "/api/settings", json={"values": {"port": 8000}}
        )
        self.assertEqual(restored.status_code, 200)

    def test_values_outside_the_safe_range_are_refused(self):
        response = self.client.put(
            "/api/settings", json={"values": {"sync_poll_seconds": 1}}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("at least", response.json()["detail"])
        unknown = self.client.put("/api/settings", json={"values": {"nope": 1}})
        self.assertEqual(unknown.status_code, 400)

    def test_an_unreadable_recording_folder_is_refused(self):
        response = self.client.put(
            "/api/settings",
            json={"values": {"source_dir": "/definitely/not/mounted"}},
        )
        self.assertEqual(response.status_code, 400)

    def test_a_value_pinned_by_the_environment_cannot_be_changed(self):
        os.environ["RADIO_SYNC_BATCH_SIZE"] = "77"
        try:
            document = self.client.get("/api/settings").json()
            field = next(
                field
                for group in document["groups"]
                for field in group["fields"]
                if field["key"] == "sync_batch_size"
            )
            self.assertTrue(field["managed_by_environment"])
            self.assertEqual(field["value"], 77)
            response = self.client.put(
                "/api/settings", json={"values": {"sync_batch_size": 10}}
            )
            self.assertEqual(response.status_code, 400)
        finally:
            del os.environ["RADIO_SYNC_BATCH_SIZE"]

    def test_only_administrators_may_read_or_change_settings(self):
        self.client.post(
            "/api/users",
            json={
                "username": "viewer-settings",
                "display_name": "Viewer",
                "role": "viewer",
                "password": "viewer-password-1",
                "active": True,
            },
        )
        viewer = TestClient(app, base_url="https://127.0.0.1")
        viewer.post(
            "/api/login",
            json={"username": "viewer-settings", "password": "viewer-password-1"},
        )
        self.assertEqual(viewer.get("/api/settings").status_code, 403)
        self.assertEqual(
            viewer.put("/api/settings", json={"values": {"port": 9000}}).status_code,
            403,
        )


class AlertEscalationTests(unittest.TestCase):
    """An alert nobody answers has to become visibly different."""

    def test_an_unanswered_alert_escalates_once_and_an_answered_one_does_not(self):
        stamp = secrets.token_hex(4)
        overdue = (datetime.now() - timedelta(minutes=5)).isoformat()
        with connect() as connection:
            rule_id = connection.execute(
                """
                INSERT INTO alert_rules(
                    name, severity, match_mode, terms_json, exclusions_json,
                    channel_scope_json, active, cooldown_seconds,
                    escalation_seconds, requires_ack, created_by, updated_by,
                    created_at, updated_at, version
                ) VALUES (?, 'critical', 'word', '["escalate"]', '[]', '[]', 1, 0,
                          60, 1, 'test', 'test', '2026-08-01T00:00:00',
                          '2026-08-01T00:00:00', 1)
                """,
                (f"Escalation rule {stamp}",),
            ).lastrowid
            alert_ids = {}
            for label, status in (("open", "open"), ("acknowledged", "acknowledged")):
                transcript_id = connection.execute(
                    """
                    INSERT INTO transcripts(
                        timestamp, recorded_at, channel, filename, transcript_text,
                        raw_transcript_text, quality_score, quality_reason, status
                    ) VALUES ('2026-08-02T02:00:00', '2026-08-02T02:00:00', 'Medical',
                              ?, 'escalate now', 'escalate now', 1.0, '', 'ready')
                    """,
                    (f"Escalate/{stamp}-{label}.mp3",),
                ).lastrowid
                alert_ids[label] = connection.execute(
                    """
                    INSERT INTO alert_events(
                        transcript_id, rule_id, severity, matched_text, explanation,
                        status, version, created_at, updated_at
                    ) VALUES (?, ?, 'critical', 'escalate', 'test', ?, 1, ?, ?)
                    """,
                    (transcript_id, rule_id, status, overdue, overdue),
                ).lastrowid
            connection.commit()

        try:
            events = escalate_overdue_alerts()
            escalated_ids = {event["resource_id"] for event in events}
            self.assertIn(alert_ids["open"], escalated_ids)
            self.assertNotIn(
                alert_ids["acknowledged"],
                escalated_ids,
                "an acknowledged alert has already been answered",
            )
            self.assertEqual(
                [
                    event
                    for event in escalate_overdue_alerts()
                    if event["resource_id"] in alert_ids.values()
                ],
                [],
                "escalation must happen once, not on every sweep",
            )
            with connect(read_only=True) as connection:
                row = connection.execute(
                    "SELECT escalated_at, status FROM alert_events WHERE id = ?",
                    (alert_ids["open"],),
                ).fetchone()
            self.assertIsNotNone(row["escalated_at"])
            self.assertEqual(row["status"], "open", "escalation is not a resolution")
        finally:
            with connect() as connection:
                connection.execute(
                    "DELETE FROM alert_events WHERE rule_id = ?", (rule_id,)
                )
                connection.execute("DELETE FROM alert_rules WHERE id = ?", (rule_id,))
                connection.execute(
                    "DELETE FROM transcripts WHERE filename LIKE ?",
                    (f"Escalate/{stamp}-%",),
                )
                connection.commit()


class DiagnosticsRedactionTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app, base_url="https://127.0.0.1")
        self.assertEqual(
            self.client.post(
                "/api/login",
                json={"username": "admin", "password": "admin-password"},
            ).status_code,
            200,
        )

    def test_no_file_in_the_bundle_carries_a_home_directory_path(self):
        response = self.client.get("/api/diagnostics.zip")
        self.assertEqual(response.status_code, 200)
        home = os.path.expanduser("~")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = archive.namelist()
            self.assertIn("health-center.json", names)
            for name in names:
                content = archive.read(name).decode("utf-8", "replace")
                self.assertNotIn(
                    home, content, f"{name} leaks the home directory path"
                )


class AlertPaginationTests(unittest.TestCase):
    """Paging the inbox must reach every alert, not just the loudest ones."""

    def setUp(self):
        self.client = TestClient(app, base_url="https://127.0.0.1")
        self.assertEqual(
            self.client.post(
                "/api/login",
                json={"username": "admin", "password": "admin-password"},
            ).status_code,
            200,
        )

    def test_lower_severity_alerts_are_still_reachable_after_the_first_page(self):
        stamp = secrets.token_hex(4)
        with connect() as connection:
            rule_id = connection.execute(
                """
                INSERT INTO alert_rules(
                    name, severity, match_mode, terms_json, exclusions_json,
                    channel_scope_json, active, cooldown_seconds, created_by,
                    updated_by, created_at, updated_at, version
                ) VALUES (?, 'caution', 'word', '["paging"]', '[]', '[]', 1, 0,
                          'test', 'test', '2026-08-01T00:00:00',
                          '2026-08-01T00:00:00', 1)
                """,
                (f"Pagination rule {stamp}",),
            ).lastrowid
            # Criticals first, then cautions with higher ids: exactly the shape
            # that an id-only cursor skipped straight past.
            expected = []
            for index in range(6):
                severity = "critical" if index < 3 else "caution"
                transcript_id = connection.execute(
                    """
                    INSERT INTO transcripts(
                        timestamp, recorded_at, channel, filename, transcript_text,
                        raw_transcript_text, quality_score, quality_reason, status
                    ) VALUES ('2026-08-02T10:00:00', '2026-08-02T09:59:00', 'Paging',
                              ?, 'paging test', 'paging test', 1.0, '', 'ready')
                    """,
                    (f"Paging/{stamp}-{index}.mp3",),
                ).lastrowid
                alert_id = connection.execute(
                    """
                    INSERT INTO alert_events(
                        transcript_id, rule_id, severity, matched_text, explanation,
                        status, version, created_at, updated_at
                    ) VALUES (?, ?, ?, 'paging', 'test', 'open', 1,
                              '2026-08-02T10:00:00', '2026-08-02T10:00:00')
                    """,
                    (transcript_id, rule_id, severity),
                ).lastrowid
                expected.append(alert_id)
            connection.commit()

        seen = []
        parameters = {"status": "open", "limit": 2}
        for _ in range(10):
            page = self.client.get("/api/alerts", params=parameters).json()
            seen.extend(item["id"] for item in page["items"])
            if not page.get("next_before_id"):
                break
            parameters = {
                "status": "open",
                "limit": 2,
                "before_id": page["next_before_id"],
                "before_rank": page["next_before_rank"],
            }

        try:
            self.assertEqual(len(seen), len(set(seen)), "no alert may be served twice")
            for alert_id in expected:
                self.assertIn(
                    alert_id, seen, "every alert must be reachable by paging"
                )
        finally:
            # The archive is shared with the other suites in this file.
            with connect() as connection:
                connection.execute(
                    "DELETE FROM alert_events WHERE rule_id = ?", (rule_id,)
                )
                connection.execute("DELETE FROM alert_rules WHERE id = ?", (rule_id,))
                connection.execute(
                    "DELETE FROM transcripts WHERE filename LIKE ?",
                    (f"Paging/{stamp}-%",),
                )
                connection.commit()


class BackupRestoreTests(unittest.TestCase):
    """A backup is only useful if it can be put back."""

    def setUp(self):
        self.client = TestClient(app, base_url="https://127.0.0.1")
        self.assertEqual(
            self.client.post(
                "/api/login",
                json={"username": "admin", "password": "admin-password"},
            ).status_code,
            200,
        )

    def test_a_queued_restore_replaces_the_archive_and_keeps_the_old_one(self):
        marker = f"restore-marker-{secrets.token_hex(4)}"
        with connect() as connection:
            connection.execute(
                """
                INSERT INTO transcripts(
                    timestamp, recorded_at, filename, transcript_text,
                    raw_transcript_text, quality_score, quality_reason, status
                ) VALUES ('2026-08-01T10:00:00', '2026-08-01T09:59:00', ?, ?, ?,
                          1.0, '', 'ready')
                """,
                (f"Restore/{marker}.mp3", marker, marker),
            )
            connection.commit()

        backup = self.client.post("/api/backups").json()

        # Something happens after the backup that the operator wants undone.
        with connect() as connection:
            connection.execute(
                "DELETE FROM transcripts WHERE filename = ?", (f"Restore/{marker}.mp3",)
            )
            connection.commit()
        with connect(read_only=True) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM transcripts WHERE filename = ?",
                    (f"Restore/{marker}.mp3",),
                ).fetchone()[0],
                0,
            )

        request_restore(backup["id"], "admin")
        self.assertIsNotNone(pending_restore())
        result = apply_pending_restore(log=lambda message: None)

        self.assertEqual(result["status"], "restored")
        self.assertTrue(os.path.isfile(result["previous_database"]))
        self.assertIsNone(pending_restore(), "the request must not run twice")
        with connect(read_only=True) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT transcript_text FROM transcripts WHERE filename = ?",
                    (f"Restore/{marker}.mp3",),
                ).fetchone()[0],
                marker,
            )

    def test_a_corrupted_backup_is_refused_and_the_archive_is_untouched(self):
        backup = self.client.post("/api/backups").json()
        request_restore(backup["id"], "admin")
        queued = pending_restore()
        with open(
            os.path.join(queued["backup_path"], "radio-command-center.db"), "r+b"
        ) as handle:
            handle.seek(0)
            handle.write(b"corrupted-header")

        before = os.path.getsize(DB_NAME)
        result = apply_pending_restore(log=lambda message: None)

        self.assertEqual(result["status"], "failed")
        self.assertIn("checksum", result["error"].lower())
        self.assertEqual(os.path.getsize(DB_NAME), before)
        # A failing restore clears itself instead of trapping every startup.
        self.assertIsNone(pending_restore())


class MaintenanceTests(unittest.TestCase):
    def test_old_history_is_pruned_and_recent_history_is_kept(self):
        old = (datetime.now() - timedelta(days=400)).isoformat()
        recent = datetime.now().isoformat()
        with connect() as connection:
            connection.execute(
                """
                INSERT INTO events(event_type, resource_type, payload_json, created_at)
                VALUES ('test.old', 'test', '{}', ?), ('test.new', 'test', '{}', ?)
                """,
                (old, recent),
            )
            connection.execute(
                """
                INSERT INTO audit_log(timestamp, username, action)
                VALUES (?, 'admin', 'test.old'), (?, 'admin', 'test.new')
                """,
                (old, recent),
            )
            connection.commit()

        save_settings({"event_retention_days": 45, "audit_retention_days": 365})
        try:
            prune_history()
        finally:
            save_settings({"event_retention_days": 45, "audit_retention_days": 730})

        with connect(read_only=True) as connection:
            events = {
                row["event_type"]
                for row in connection.execute(
                    "SELECT event_type FROM events WHERE event_type LIKE 'test.%'"
                )
            }
            actions = {
                row["action"]
                for row in connection.execute(
                    "SELECT action FROM audit_log WHERE action LIKE 'test.%'"
                )
            }
        self.assertEqual(events, {"test.new"})
        self.assertEqual(actions, {"test.new"})


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        TEST_ROOT.cleanup()
