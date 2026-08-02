import json


INCIDENT_SEVERITIES = {"informational", "caution", "urgent", "critical"}
INCIDENT_STATUSES = {"monitoring", "active", "stabilized", "resolved"}
INCIDENT_TASK_STATUSES = {"open", "in_progress", "done", "cancelled"}


def _decode_tags(value):
    try:
        tags = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(tag) for tag in tags if str(tag).strip()]


def incident_row_to_dict(row):
    keys = set(row.keys())
    return {
        "id": row["id"],
        "title": row["title"],
        "severity": row["severity"],
        "status": row["status"],
        "owner_username": row["owner_username"],
        "location": row["location"] or "",
        "summary": row["summary"] or "",
        "tags": _decode_tags(row["tags_json"]),
        "started_at": row["started_at"],
        "resolved_at": row["resolved_at"],
        "version": row["version"],
        "created_by": row["created_by"],
        "updated_by": row["updated_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "transcript_count": (
            row["transcript_count"] if "transcript_count" in keys else 0
        ),
        "open_task_count": (
            row["open_task_count"] if "open_task_count" in keys else 0
        ),
    }


def task_row_to_dict(row):
    return {
        "id": row["id"],
        "incident_id": row["incident_id"],
        "description": row["description"],
        "status": row["status"],
        "assigned_to": row["assigned_to"],
        "due_at": row["due_at"],
        "version": row["version"],
        "created_by": row["created_by"],
        "updated_by": row["updated_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def note_row_to_dict(row):
    return {
        "id": row["id"],
        "incident_id": row["incident_id"],
        "note": row["note"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }


INCIDENT_SUMMARY_SELECT = """
    SELECT i.*,
           (SELECT count(*) FROM incident_transcripts it
            WHERE it.incident_id = i.id) AS transcript_count,
           (SELECT count(*) FROM incident_tasks task
            WHERE task.incident_id = i.id
              AND task.status IN ('open', 'in_progress')) AS open_task_count
    FROM incidents i
"""


def get_incident(connection, incident_id):
    row = connection.execute(
        f"{INCIDENT_SUMMARY_SELECT} WHERE i.id = ?",
        (incident_id,),
    ).fetchone()
    return incident_row_to_dict(row) if row is not None else None


def incident_detail(connection, incident_id, transcript_converter):
    incident = get_incident(connection, incident_id)
    if incident is None:
        return None
    transcript_rows = connection.execute(
        """
        SELECT t.*, it.added_by, it.added_at
        FROM incident_transcripts it
        JOIN transcripts t ON t.id = it.transcript_id
        WHERE it.incident_id = ?
        ORDER BY coalesce(t.recorded_at, t.timestamp) ASC, t.id ASC
        """,
        (incident_id,),
    ).fetchall()
    incident["transcripts"] = []
    for row in transcript_rows:
        transcript = transcript_converter(row)
        transcript["added_by"] = row["added_by"]
        transcript["added_at"] = row["added_at"]
        incident["transcripts"].append(transcript)
    task_rows = connection.execute(
        "SELECT * FROM incident_tasks WHERE incident_id = ? ORDER BY id ASC",
        (incident_id,),
    ).fetchall()
    incident["tasks"] = [task_row_to_dict(row) for row in task_rows]
    note_rows = connection.execute(
        "SELECT * FROM incident_notes WHERE incident_id = ? ORDER BY id DESC",
        (incident_id,),
    ).fetchall()
    incident["notes"] = [note_row_to_dict(row) for row in note_rows]
    return incident
