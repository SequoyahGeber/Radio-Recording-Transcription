#!/usr/bin/env python3
"""Create disposable, realistic dashboard data for browser regression tests."""

import argparse
import json
import os
import secrets
import shutil
import sys
import wave
from datetime import datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_MARKER = ".radio-command-center-browser-fixture"
TEST_USERNAME = "phase0-admin"
TEST_PASSWORD = "phase0-browser-password"
CHANNELS = (
    "Dispatch",
    "Medical",
    "Security",
    "Parking",
    "Operations",
    "Shuttle",
    "Gate North",
    "Gate South",
)
MESSAGES = (
    "Copy, team is moving to the north gate for the scheduled handoff.",
    "Medical requested for a guest reporting chest pain near the main stage.",
    "Security confirms the service road is clear and the gate is secured.",
    "Parking lot three is at capacity; redirect arrivals to the east field.",
    "Operations needs two additional barricades delivered to the loading dock.",
    "Shuttle seven is departing the campground with twelve passengers.",
    "Gate North has located the missing child and is reuniting the family.",
    "Gate South reports normal flow with no queue outside the entrance.",
    "Medic team is assessing dehydration and requesting fresh water.",
    "Dispatch copies the priority call and assigns the nearest available unit.",
    "Supervisor requests a radio check from all active channel leads.",
    "A vehicle is blocking the fire lane beside the production compound.",
)


def configure_environment(root):
    root = Path(root).expanduser().resolve()
    values = {
        "RADIO_DATA_DIR": root / "data",
        "RADIO_SOURCE_DIR": root / "source",
        "RADIO_AUDIO_DIR": root / "audio",
        "RADIO_DB_PATH": root / "data" / "dashboard-fixture.db",
        "RADIO_SECURITY_DIR": root / "security",
        "RADIO_RUNTIME_DIR": root / "runtime",
        "RADIO_LOG_DIR": root / "logs",
        "RADIO_MODEL_DIR": root / "models",
        "RADIO_SETTINGS_PATH": root / "settings.json",
        "RADIO_RECORDING_YEAR": "2026",
        "RADIO_MODEL_SIZE": "medium",
        "RADIO_TRANSCRIPTION_ENGINE": "mlx",
        "RADIO_HOST": os.environ.get("RADIO_BROWSER_TEST_HOST", "127.0.0.1"),
        "RADIO_PORT": os.environ.get("RADIO_BROWSER_TEST_PORT", "8765"),
    }
    for name, value in values.items():
        os.environ[name] = str(value)
    return root


def reset_fixture_root(root):
    root = Path(root).resolve()
    if root.exists():
        marker = root / FIXTURE_MARKER
        if not marker.is_file():
            raise RuntimeError(
                f"Refusing to replace unmarked fixture directory: {root}"
            )
        shutil.rmtree(root)
    root.mkdir(parents=True)
    (root / FIXTURE_MARKER).write_text("disposable browser fixture\n", encoding="utf-8")


def write_silent_wave(path, duration_seconds=0.08):
    path.parent.mkdir(parents=True, exist_ok=True)
    frame_rate = 8_000
    frames = b"\x00\x00" * int(frame_rate * duration_seconds)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(frame_rate)
        output.writeframes(frames)


def create_fixture(root, transcript_count=176):
    root = configure_environment(root)
    reset_fixture_root(root)
    for name in ("source", "audio", "data", "security", "runtime", "logs", "models"):
        (root / name).mkdir(parents=True, exist_ok=True)

    from backend.alerts import evaluate_transcript_alerts
    from backend.database import connect
    from backend.security import hash_password, save_security_config

    salt, password_digest = hash_password(TEST_PASSWORD)
    save_security_config(
        {
            "users": [
                {
                    "username": TEST_USERNAME,
                    "display_name": "Phase Zero Administrator",
                    "role": "admin",
                    "active": True,
                    "password_salt": salt,
                    "password_hash": password_digest,
                }
            ],
            "session_secret": secrets.token_urlsafe(48),
            "internal_token": "phase-zero-internal-token",
        }
    )

    now = datetime.now().replace(microsecond=0)
    rows = []
    for index in range(transcript_count):
        channel = CHANNELS[index % len(CHANNELS)]
        recorded_at = now - timedelta(seconds=(transcript_count - index) * 38)
        if index < 12:
            recorded_at = recorded_at.replace(year=2024)
        elif index < 24:
            recorded_at = recorded_at.replace(year=2025)
        completed_at = recorded_at + timedelta(seconds=7)
        message = MESSAGES[index % len(MESSAGES)]
        status = "suspect" if index % 29 == 0 else "ready"
        quality_reason = (
            "Low average log probability; retained for supervisor review"
            if status == "suspect"
            else ""
        )
        quality_score = 0.28 if status == "suspect" else round(0.82 + (index % 15) / 100, 2)
        reviewed = index % 7 == 0
        bookmarked = index % 13 == 0
        notes = (
            "Include in shift handoff; follow-up confirmed."
            if index % 17 == 0
            else ""
        )
        corrected = index % 31 == 0 and status == "ready"
        retry_status = "selected" if index % 37 == 0 else None
        retry_text = (
            f"{message} Large V3 clarified the callsign."
            if retry_status
            else None
        )
        safe_channel = channel.lower().replace(" ", "-")
        filename = (
            f"{channel}/{recorded_at:%Y-%m-%d-%H-%M-%S}-{safe_channel}.wav"
        )
        write_silent_wave(root / "audio" / filename)
        rows.append(
            (
                completed_at.isoformat(),
                recorded_at.isoformat(),
                recorded_at.year,
                channel,
                filename,
                message,
                message if not corrected else f"Uncorrected audio text: {message}",
                quality_score,
                quality_reason,
                json.dumps(
                    {
                        "avg_logprob": round(-0.12 - (index % 9) / 100, 2),
                        "no_speech_prob": round((index % 5) / 100, 2),
                    },
                    separators=(",", ":"),
                ),
                status,
                int(reviewed or corrected),
                "corrected" if corrected else "confirmed" if reviewed else "unreviewed",
                TEST_USERNAME if reviewed or corrected else None,
                completed_at.isoformat() if reviewed or corrected else None,
                "Routine fixture review complete." if reviewed else "",
                1,
                int(bookmarked),
                notes,
                TEST_USERNAME if corrected else None,
                completed_at.isoformat() if corrected else None,
                "mlx-community/whisper-medium-mlx",
                retry_text,
                "mlx-community/whisper-large-v3-mlx" if retry_text else None,
                0.96 if retry_text else None,
                "" if retry_text else None,
                "{}" if retry_text else None,
                retry_status,
                completed_at.isoformat() if retry_text else None,
            )
        )

    with connect() as connection:
        connection.executemany(
            """
            INSERT INTO transcripts(
                timestamp, recorded_at, recording_year, channel,
                filename, transcript_text,
                raw_transcript_text, quality_score, quality_reason,
                quality_metrics, status, reviewed, review_state, reviewed_by,
                reviewed_at, review_resolution, version, bookmarked, notes,
                corrected_by, corrected_at, transcription_model,
                retry_transcript_text, retry_model, retry_quality_score,
                retry_quality_reason, retry_quality_metrics, retry_status,
                retry_attempted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        alert_source_rows = connection.execute(
            """
            SELECT id, timestamp, recorded_at, channel, transcript_text,
                   quality_score
            FROM transcripts
            WHERE status = 'ready'
            ORDER BY id
            """
        ).fetchall()
        alert_count = 0
        for alert_source in alert_source_rows:
            alerts, _ = evaluate_transcript_alerts(
                connection,
                dict(alert_source),
                actor="fixture",
            )
            alert_count += len(alerts)
        heartbeat_time = now.isoformat()
        connection.executemany(
            """
            INSERT INTO service_heartbeats(service, last_seen, status, details)
            VALUES (?, ?, ?, ?)
            """,
            (
                (
                    "worker",
                    heartbeat_time,
                    "online",
                    json.dumps(
                        {
                            "queue_depth": 4,
                            "rescue_queue_depth": 1,
                            "latest_filename": rows[-1][2],
                        }
                    ),
                ),
                (
                    "sync",
                    heartbeat_time,
                    "online",
                    json.dumps(
                        {
                            "source_mounted": True,
                            "copied": 12,
                            "failed": 0,
                        }
                    ),
                ),
            ),
        )
        connection.commit()

    log_lines = {
        "server.log": (
            "INFO - Phase Zero fixture dashboard started\n"
            "WARNING - Sample warning for console filtering\n"
        ),
        "worker.log": "INFO - Medium model ready; rescue queue depth=1\n",
        "sync.log": "INFO - Sync pass complete: copied=12 failed=0\n",
    }
    for filename, content in log_lines.items():
        (root / "logs" / filename).write_text(
            f"{now.isoformat()} - {content}",
            encoding="utf-8",
        )

    summary = {
        "root": str(root),
        "database": os.environ["RADIO_DB_PATH"],
        "username": TEST_USERNAME,
        "password": TEST_PASSWORD,
        "transcripts": transcript_count,
        "channels": list(CHANNELS),
        "alerts": alert_count,
    }
    (root / "fixture-summary.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--transcripts", type=int, default=176)
    arguments = parser.parse_args()
    summary = create_fixture(arguments.root, max(16, arguments.transcripts))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    main()
