from datetime import datetime

from backend.database import connect


def quality_analytics():
    with connect(read_only=True) as connection:
        totals = connection.execute(
            """
            SELECT
                count(*) AS total,
                sum(CASE WHEN review_state != 'unreviewed' THEN 1 ELSE 0 END) AS reviewed,
                sum(CASE WHEN retry_model IS NOT NULL THEN 1 ELSE 0 END) AS rescued,
                sum(CASE WHEN selected_transcript = 'retry' THEN 1 ELSE 0 END) AS retry_selected,
                sum(CASE WHEN primary_status = 'suspect'
                              AND selected_transcript = 'primary'
                              AND review_state IN ('confirmed', 'corrected')
                         THEN 1 ELSE 0 END) AS false_quarantine,
                sum(CASE WHEN primary_status = 'ready' AND review_state = 'dismissed'
                         THEN 1 ELSE 0 END) AS missed_garbage,
                avg(CASE WHEN retry_quality_score IS NOT NULL
                         THEN retry_quality_score - primary_quality_score END) AS rescue_improvement
            FROM transcripts
            """
        ).fetchone()
        channels = connection.execute(
            """
            SELECT channel, count(*) AS total,
                   sum(CASE WHEN review_state != 'unreviewed' THEN 1 ELSE 0 END) AS reviewed,
                   avg(primary_quality_score) AS medium_quality,
                   avg(CASE WHEN retry_quality_score IS NOT NULL
                       THEN retry_quality_score - primary_quality_score END) AS rescue_improvement,
                   sum(CASE WHEN primary_status = 'suspect' THEN 1 ELSE 0 END) AS suspect
            FROM transcripts
            WHERE channel IS NOT NULL AND channel != ''
            GROUP BY channel
            ORDER BY total DESC, lower(channel)
            """
        ).fetchall()
        jobs = connection.execute(
            """
            SELECT job_type,
                   avg((julianday(completed_at) - julianday(started_at)) * 86400.0) AS seconds,
                   count(*) AS completed
            FROM transcription_jobs
            WHERE status = 'completed' AND started_at IS NOT NULL
            GROUP BY job_type
            """
        ).fetchall()
    reviewed = totals["reviewed"] or 0
    false_rate = (totals["false_quarantine"] or 0) / reviewed if reviewed else None
    missed_rate = (totals["missed_garbage"] or 0) / reviewed if reviewed else None
    return {
        "totals": dict(totals),
        "false_quarantine_rate": round(false_rate, 4) if false_rate is not None else None,
        "missed_garbage_rate": round(missed_rate, 4) if missed_rate is not None else None,
        "channels": [dict(row) for row in channels],
        "processing": [dict(row) for row in jobs],
        "evaluation_gate": {
            "eligible": reviewed >= 50,
            "reviewed_required": 50,
            "reviewed_available": reviewed,
            "explanation": (
                "Representative reviewed audio is sufficient for controlled experiments."
                if reviewed >= 50
                else f"Review {50 - reviewed} more real transmissions before enabling experimental denoise or language detection."
            ),
        },
        "generated_at": datetime.now().isoformat(),
    }


def list_profiles():
    with connect(read_only=True) as connection:
        rows = connection.execute(
            "SELECT * FROM channel_transcription_profiles ORDER BY lower(channel)"
        ).fetchall()
    return [
        {
            **dict(row),
            "normalize_audio": bool(row["normalize_audio"]),
            "denoise_audio": bool(row["denoise_audio"]),
            "experimental_enabled": bool(row["experimental_enabled"]),
        }
        for row in rows
    ]


def save_profile(channel, payload, username):
    channel = " ".join(str(channel).strip().split())[:160]
    if not channel:
        raise ValueError("Channel is required")
    experimental = bool(payload.get("experimental_enabled"))
    if experimental and not quality_analytics()["evaluation_gate"]["eligible"]:
        raise ValueError("The real-audio evaluation gate has not been met")
    language_mode = str(payload.get("language_mode") or "explicit")
    if language_mode not in {"explicit", "detect"}:
        raise ValueError("Invalid language mode")
    now = datetime.now().isoformat()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO channel_transcription_profiles(
                channel, normalize_audio, denoise_audio, language_mode,
                experimental_enabled, updated_by, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel) DO UPDATE SET
                normalize_audio = excluded.normalize_audio,
                denoise_audio = excluded.denoise_audio,
                language_mode = excluded.language_mode,
                experimental_enabled = excluded.experimental_enabled,
                version = channel_transcription_profiles.version + 1,
                updated_by = excluded.updated_by,
                updated_at = excluded.updated_at
            """,
            (
                channel,
                int(bool(payload.get("normalize_audio"))),
                int(bool(payload.get("denoise_audio")) and experimental),
                language_mode,
                int(experimental),
                username,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM channel_transcription_profiles WHERE channel = ?",
            (channel,),
        ).fetchone()
        connection.commit()
    return {
        **dict(row),
        "normalize_audio": bool(row["normalize_audio"]),
        "denoise_audio": bool(row["denoise_audio"]),
        "experimental_enabled": bool(row["experimental_enabled"]),
    }


def profile_for_channel(channel):
    with connect(read_only=True) as connection:
        row = connection.execute(
            "SELECT * FROM channel_transcription_profiles WHERE channel = ?",
            (channel,),
        ).fetchone()
    return dict(row) if row else {
        "normalize_audio": 0,
        "denoise_audio": 0,
        "language_mode": "explicit",
        "experimental_enabled": 0,
    }
