import base64
import csv
import io
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


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        TEST_ROOT.cleanup()
