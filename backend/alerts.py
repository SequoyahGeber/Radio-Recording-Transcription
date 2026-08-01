import json
import re
from datetime import datetime, timedelta

from backend.events import record_event


ALERT_SEVERITIES = {"informational", "caution", "urgent", "critical"}
ALERT_STATUSES = {"open", "acknowledged", "resolved", "false_positive"}
ALERT_MATCH_MODES = {"whole_word", "prefix", "phrase"}

DEFAULT_ALERT_RULES = (
    {
        "slug": "critical-medical",
        "name": "Critical medical emergency",
        "description": "Immediate life-safety language requiring acknowledgement.",
        "severity": "critical",
        "match_mode": "phrase",
        "terms": ["not breathing", "cardiac arrest", "unconscious person"],
        "cooldown_seconds": 30,
        "requires_ack": True,
        "escalation_seconds": 60,
    },
    {
        "slug": "critical-security",
        "name": "Critical security threat",
        "description": "Weapons, lockdowns, or immediate violent threats.",
        "severity": "critical",
        "match_mode": "whole_word",
        "terms": ["weapon", "gun", "knife", "lockdown", "active shooter"],
        "cooldown_seconds": 30,
        "requires_ack": True,
        "escalation_seconds": 60,
    },
    {
        "slug": "missing-person",
        "name": "Missing person",
        "description": "Missing or lost child/person reports.",
        "severity": "urgent",
        "match_mode": "phrase",
        "terms": ["missing child", "lost child", "missing person"],
        "cooldown_seconds": 90,
        "requires_ack": True,
        "escalation_seconds": 180,
    },
    {
        "slug": "medical-response",
        "name": "Medical response",
        "description": "Medical response language requiring operator attention.",
        "severity": "urgent",
        "match_mode": "whole_word",
        "terms": ["medical", "medic", "ambulance", "chest pain"],
        "cooldown_seconds": 45,
        "requires_ack": True,
        "escalation_seconds": 180,
    },
    {
        "slug": "safety-assistance",
        "name": "Safety assistance",
        "description": "Broad safety language retained as caution, not critical.",
        "severity": "caution",
        "match_mode": "whole_word",
        "terms": ["help", "injury", "fight", "assault", "breach"],
        "cooldown_seconds": 60,
        "requires_ack": False,
        "escalation_seconds": 0,
    },
)


def _decode_list(value):
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def rule_row_to_dict(row):
    return {
        "id": row["id"],
        "slug": row["slug"],
        "name": row["name"],
        "description": row["description"] or "",
        "severity": row["severity"],
        "match_mode": row["match_mode"],
        "terms": _decode_list(row["terms_json"]),
        "exclusions": _decode_list(row["exclusions_json"]),
        "channels": _decode_list(row["channel_scope_json"]),
        "start_time": row["start_time"] or "",
        "end_time": row["end_time"] or "",
        "minimum_quality": row["minimum_quality"],
        "cooldown_seconds": row["cooldown_seconds"],
        "sound": row["sound"] or "",
        "requires_ack": bool(row["requires_ack"]),
        "escalation_seconds": row["escalation_seconds"],
        "active": bool(row["active"]),
        "is_default": bool(row["is_default"]),
        "version": row["version"],
        "created_by": row["created_by"],
        "updated_by": row["updated_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def seed_default_alert_rules(connection):
    now = datetime.now().isoformat()
    for rule in DEFAULT_ALERT_RULES:
        connection.execute(
            """
            INSERT OR IGNORE INTO alert_rules(
                slug, name, description, severity, match_mode,
                terms_json, exclusions_json, channel_scope_json,
                minimum_quality, cooldown_seconds, sound, requires_ack,
                escalation_seconds, active, is_default, version,
                created_by, updated_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, '[]', '[]', 0.0, ?, ?, ?, ?,
                      1, 1, 1, 'system', 'system', ?, ?)
            """,
            (
                rule["slug"],
                rule["name"],
                rule["description"],
                rule["severity"],
                rule["match_mode"],
                json.dumps(rule["terms"]),
                rule["cooldown_seconds"],
                rule["severity"],
                int(rule["requires_ack"]),
                rule["escalation_seconds"],
                now,
                now,
            ),
        )


def _term_pattern(term, match_mode):
    escaped = re.escape(term.strip())
    if match_mode == "prefix":
        return re.compile(rf"(?<!\w){escaped}\w*", re.IGNORECASE)
    return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)


def rule_matches(rule, transcript):
    text = str(transcript.get("transcript_text") or "")
    if not text:
        return []
    quality_score = float(transcript.get("quality_score") or 0)
    if quality_score < float(rule.get("minimum_quality") or 0):
        return []
    scoped_channels = {item.casefold() for item in rule.get("channels", [])}
    channel = str(transcript.get("channel") or "").casefold()
    if scoped_channels and channel not in scoped_channels:
        return []
    recorded_at = str(
        transcript.get("recorded_at") or transcript.get("timestamp") or ""
    )
    recorded_time = recorded_at[11:16] if len(recorded_at) >= 16 else ""
    start_time = rule.get("start_time") or ""
    end_time = rule.get("end_time") or ""
    if recorded_time and start_time and end_time:
        in_window = (
            start_time <= recorded_time <= end_time
            if start_time <= end_time
            else recorded_time >= start_time or recorded_time <= end_time
        )
        if not in_window:
            return []
    elif recorded_time and start_time and recorded_time < start_time:
        return []
    elif recorded_time and end_time and recorded_time > end_time:
        return []

    for exclusion in rule.get("exclusions", []):
        if _term_pattern(exclusion, "phrase").search(text):
            return []

    mode = rule.get("match_mode", "whole_word")
    matches = []
    for term in rule.get("terms", []):
        found = _term_pattern(term, mode).search(text)
        if found:
            matches.append(found.group(0))
    return list(dict.fromkeys(matches))


def alert_row_to_dict(row):
    keys = set(row.keys())
    return {
        "id": row["id"],
        "transcript_id": row["transcript_id"],
        "rule_id": row["rule_id"],
        "rule_name": row["rule_name"] if "rule_name" in keys else "",
        "severity": row["severity"],
        "matched_text": row["matched_text"],
        "explanation": row["explanation"],
        "status": row["status"],
        "assigned_to": row["assigned_to"],
        "acknowledged_by": row["acknowledged_by"],
        "acknowledged_at": row["acknowledged_at"],
        "resolved_by": row["resolved_by"],
        "resolved_at": row["resolved_at"],
        "resolution_note": row["resolution_note"] or "",
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "channel": row["channel"] if "channel" in keys else None,
        "recorded_at": row["recorded_at"] if "recorded_at" in keys else None,
        "transcript_text": (
            row["transcript_text"] if "transcript_text" in keys else None
        ),
        "requires_ack": (
            bool(row["requires_ack"]) if "requires_ack" in keys else False
        ),
        "escalation_seconds": (
            row["escalation_seconds"] if "escalation_seconds" in keys else 0
        ),
    }


def evaluate_transcript_alerts(connection, transcript, actor="system"):
    rows = connection.execute(
        """
        SELECT *
        FROM alert_rules
        WHERE active = 1
        ORDER BY
            CASE severity
                WHEN 'critical' THEN 4
                WHEN 'urgent' THEN 3
                WHEN 'caution' THEN 2
                ELSE 1
            END DESC,
            id ASC
        """
    ).fetchall()
    now = datetime.now()
    created_alerts = []
    created_events = []
    for row in rows:
        rule = rule_row_to_dict(row)
        matches = rule_matches(rule, transcript)
        if not matches:
            continue
        matched_text = matches[0]
        cooldown = max(0, int(rule["cooldown_seconds"] or 0))
        if cooldown:
            cutoff = (now - timedelta(seconds=cooldown)).isoformat()
            duplicate = connection.execute(
                """
                SELECT 1
                FROM alert_events
                WHERE rule_id = ? AND lower(matched_text) = lower(?)
                  AND created_at >= ? AND status != 'false_positive'
                LIMIT 1
                """,
                (rule["id"], matched_text, cutoff),
            ).fetchone()
            if duplicate:
                continue
        explanation = (
            f"{rule['severity'].title()} because rule {rule['name']} "
            f"matched “{matched_text}”."
        )
        timestamp = now.isoformat()
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO alert_events(
                transcript_id, rule_id, severity, matched_text,
                explanation, status, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'open', 1, ?, ?)
            """,
            (
                transcript["id"],
                rule["id"],
                rule["severity"],
                matched_text,
                explanation,
                timestamp,
                timestamp,
            ),
        )
        if cursor.rowcount != 1:
            continue
        alert_id = cursor.lastrowid
        alert = {
            "id": alert_id,
            "transcript_id": transcript["id"],
            "rule_id": rule["id"],
            "rule_name": rule["name"],
            "severity": rule["severity"],
            "matched_text": matched_text,
            "explanation": explanation,
            "status": "open",
            "assigned_to": None,
            "acknowledged_by": None,
            "acknowledged_at": None,
            "resolved_by": None,
            "resolved_at": None,
            "resolution_note": "",
            "version": 1,
            "created_at": timestamp,
            "updated_at": timestamp,
            "channel": transcript.get("channel"),
            "recorded_at": transcript.get("recorded_at"),
            "transcript_text": transcript.get("transcript_text"),
            "requires_ack": rule["requires_ack"],
            "escalation_seconds": rule["escalation_seconds"],
        }
        event, _ = record_event(
            connection,
            "alert.created",
            resource_type="alert",
            resource_id=alert_id,
            actor=actor,
            payload={"alert": alert},
            dedupe_key=f"alert.created:{alert_id}",
            created_at=timestamp,
        )
        created_alerts.append(alert)
        created_events.append(event)
    return created_alerts, created_events
