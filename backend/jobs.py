from datetime import datetime, timedelta

from backend.database import connect


ACTIVE_JOB_STATUSES = {"queued", "running", "retry_wait"}


def _now(value=None):
    return value or datetime.now()


def job_to_dict(row):
    return dict(row) if row is not None else None


def enqueue_job(
    job_type,
    relative_path,
    *,
    transcript_id=None,
    priority=1,
    max_attempts=20,
    model="",
    scheduled_at=None,
):
    now = _now()
    scheduled = scheduled_at or now
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO transcription_jobs(
                job_type, relative_path, transcript_id, priority, status,
                attempt_count, max_attempts, scheduled_at, model,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?)
            ON CONFLICT(job_type, relative_path) DO UPDATE SET
                transcript_id = coalesce(excluded.transcript_id, transcription_jobs.transcript_id),
                priority = min(excluded.priority, transcription_jobs.priority),
                max_attempts = excluded.max_attempts,
                model = excluded.model,
                scheduled_at = CASE
                    WHEN transcription_jobs.status IN ('failed', 'cancelled')
                    THEN excluded.scheduled_at ELSE transcription_jobs.scheduled_at
                END,
                status = CASE
                    WHEN transcription_jobs.status IN ('failed', 'cancelled')
                    THEN 'queued' ELSE transcription_jobs.status
                END,
                attempt_count = CASE
                    WHEN transcription_jobs.status IN ('failed', 'cancelled')
                    THEN 0 ELSE transcription_jobs.attempt_count
                END,
                completed_at = CASE
                    WHEN transcription_jobs.status IN ('failed', 'cancelled')
                    THEN NULL ELSE transcription_jobs.completed_at
                END,
                last_error = CASE
                    WHEN transcription_jobs.status IN ('failed', 'cancelled')
                    THEN '' ELSE transcription_jobs.last_error
                END,
                updated_at = excluded.updated_at
            """,
            (
                job_type,
                relative_path,
                transcript_id,
                int(priority),
                int(max_attempts),
                scheduled.isoformat(),
                model,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        row = connection.execute(
            """
            SELECT * FROM transcription_jobs
            WHERE job_type = ? AND relative_path = ?
            """,
            (job_type, relative_path),
        ).fetchone()
        connection.commit()
    return job_to_dict(row)


def due_jobs(limit=100, now=None):
    current = _now(now).isoformat()
    with connect(read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT * FROM transcription_jobs
            WHERE status IN ('queued', 'retry_wait') AND scheduled_at <= ?
            ORDER BY priority ASC, scheduled_at ASC, id ASC
            LIMIT ?
            """,
            (current, int(limit)),
        ).fetchall()
    return [job_to_dict(row) for row in rows]


def claim_job(job_id, now=None):
    current = _now(now).isoformat()
    with connect() as connection:
        cursor = connection.execute(
            """
            UPDATE transcription_jobs
            SET status = 'running', attempt_count = attempt_count + 1,
                started_at = ?, updated_at = ?, last_error = ''
            WHERE id = ?
              AND status IN ('queued', 'retry_wait')
              AND scheduled_at <= ?
            """,
            (current, current, int(job_id), current),
        )
        row = connection.execute(
            "SELECT * FROM transcription_jobs WHERE id = ?",
            (int(job_id),),
        ).fetchone()
        connection.commit()
    return job_to_dict(row) if cursor.rowcount == 1 else None


def complete_job(job_id, transcript_id=None, now=None):
    current = _now(now).isoformat()
    with connect() as connection:
        connection.execute(
            """
            UPDATE transcription_jobs
            SET status = 'completed', transcript_id = coalesce(?, transcript_id),
                completed_at = ?, updated_at = ?, last_error = ''
            WHERE id = ?
            """,
            (transcript_id, current, current, int(job_id)),
        )
        connection.commit()


def fail_or_reschedule_job(job_id, error, delay_seconds=10, now=None):
    current = _now(now)
    scheduled = current + timedelta(seconds=max(0, float(delay_seconds)))
    with connect() as connection:
        row = connection.execute(
            "SELECT attempt_count, max_attempts FROM transcription_jobs WHERE id = ?",
            (int(job_id),),
        ).fetchone()
        if row is None:
            return None
        exhausted = row["attempt_count"] >= row["max_attempts"]
        connection.execute(
            """
            UPDATE transcription_jobs
            SET status = ?, scheduled_at = ?, completed_at = ?,
                last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                "failed" if exhausted else "retry_wait",
                scheduled.isoformat(),
                current.isoformat() if exhausted else None,
                str(error)[:1000],
                current.isoformat(),
                int(job_id),
            ),
        )
        updated = connection.execute(
            "SELECT * FROM transcription_jobs WHERE id = ?",
            (int(job_id),),
        ).fetchone()
        connection.commit()
    return job_to_dict(updated)


def defer_job(job_id, delay_seconds=5, now=None):
    current = _now(now)
    scheduled = current + timedelta(seconds=max(0, float(delay_seconds)))
    with connect() as connection:
        connection.execute(
            """
            UPDATE transcription_jobs
            SET status = 'retry_wait', scheduled_at = ?, started_at = NULL,
                attempt_count = max(0, attempt_count - 1), updated_at = ?
            WHERE id = ?
            """,
            (scheduled.isoformat(), current.isoformat(), int(job_id)),
        )
        connection.commit()


def reclaim_stale_jobs(active_job_ids=(), older_than_seconds=0, now=None):
    """Return jobs left mid-flight by a stopped worker to the queue.

    A claimed job is marked 'running', and nothing else ever moves it out of
    that state. If the worker is killed while transcribing - a crash, a restart,
    a power cut - the recording it was working on would otherwise stay 'running'
    forever: never dispatched again, and never noticed, because the file exists
    and the queue looks idle.

    The attempt count is deliberately left as it was, so a recording that
    reliably takes the worker down with it still retires after max_attempts
    instead of restarting the process forever.
    """
    current = _now(now)
    cutoff = (current - timedelta(seconds=max(0, float(older_than_seconds)))).isoformat()
    exclusion = ""
    parameters = [
        current.isoformat(),
        current.isoformat(),
        cutoff,
    ]
    active = [int(value) for value in active_job_ids]
    if active:
        exclusion = f"AND id NOT IN ({','.join('?' * len(active))})"
        parameters.extend(active)
    with connect() as connection:
        cursor = connection.execute(
            f"""
            UPDATE transcription_jobs
            SET status = 'retry_wait',
                scheduled_at = ?,
                started_at = NULL,
                updated_at = ?,
                last_error = 'Requeued after the transcription worker stopped'
            WHERE status = 'running'
              AND coalesce(started_at, updated_at) <= ?
              {exclusion}
            """,
            parameters,
        )
        connection.commit()
    return cursor.rowcount


def has_live_primary_work(excluding_job_id=None):
    parameters = []
    exclusion = ""
    if excluding_job_id is not None:
        exclusion = "AND id != ?"
        parameters.append(int(excluding_job_id))
    with connect(read_only=True) as connection:
        row = connection.execute(
            f"""
            SELECT 1 FROM transcription_jobs
            WHERE job_type = 'primary' AND priority = 0
              AND status IN ('queued', 'running', 'retry_wait')
              {exclusion}
            LIMIT 1
            """,
            parameters,
        ).fetchone()
    return row is not None


def queue_summary():
    with connect(read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT job_type, status, count(*) AS count
            FROM transcription_jobs
            GROUP BY job_type, status
            """
        ).fetchall()
    summary = {
        "primary": {"queued": 0, "running": 0, "retry_wait": 0, "failed": 0},
        "quality_retry": {"queued": 0, "running": 0, "retry_wait": 0, "failed": 0},
    }
    for row in rows:
        summary.setdefault(row["job_type"], {})[row["status"]] = row["count"]
    return summary
