import json
from datetime import datetime


def event_row_to_dict(row):
    return {
        "event_id": row["id"],
        "type": row["event_type"],
        "resource_type": row["resource_type"],
        "resource_id": row["resource_id"],
        "actor": row["actor_username"] or None,
        "payload": json.loads(row["payload_json"] or "{}"),
        "created_at": row["created_at"],
    }


def record_event(
    connection,
    event_type,
    *,
    resource_type="",
    resource_id=None,
    actor="",
    payload=None,
    dedupe_key=None,
    created_at=None,
):
    timestamp = created_at or datetime.now().isoformat()
    encoded_payload = json.dumps(
        payload or {},
        separators=(",", ":"),
        sort_keys=True,
    )
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO events(
            event_type, resource_type, resource_id, actor_username,
            payload_json, dedupe_key, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_type,
            resource_type,
            resource_id,
            actor or None,
            encoded_payload,
            dedupe_key,
            timestamp,
        ),
    )
    created = cursor.rowcount == 1
    if created:
        event_id = cursor.lastrowid
    elif dedupe_key:
        existing = connection.execute(
            "SELECT id FROM events WHERE dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()
        event_id = existing["id"]
    else:
        raise RuntimeError("Event could not be recorded")
    row = connection.execute(
        """
        SELECT id, event_type, resource_type, resource_id, actor_username,
               payload_json, created_at
        FROM events
        WHERE id = ?
        """,
        (event_id,),
    ).fetchone()
    return event_row_to_dict(row), created


def replay_events(connection, after_id=0, limit=500):
    rows = connection.execute(
        """
        SELECT id, event_type, resource_type, resource_id, actor_username,
               payload_json, created_at
        FROM events
        WHERE id > ?
        ORDER BY id ASC
        LIMIT ?
        """,
        (max(0, int(after_id)), max(1, min(int(limit), 1000))),
    ).fetchall()
    return [event_row_to_dict(row) for row in rows]
