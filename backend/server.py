import csv
import io
import json
import logging
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
import zlib
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import List, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.alerts import (
    ALERT_MATCH_MODES,
    ALERT_SEVERITIES,
    ALERT_STATUSES,
    alert_row_to_dict,
    evaluate_transcript_alerts,
    rule_matches,
    rule_row_to_dict,
)
from backend.analytics import list_profiles, quality_analytics, save_profile
from backend.backups import (
    create_backup,
    list_backups,
    restore_drill,
    verify_backup,
)
from backend.maintenance import start_maintenance_thread
from backend.settings_api import apply_settings, settings_document
from backend.config import (
    AUDIO_DIR,
    DB_NAME,
    LOG_DIR,
    PROJECT_ROOT,
    RADIO_HOST,
    RADIO_PORT,
    RECORDING_SOURCE_DIR,
    load_settings,
    number_setting,
    save_settings,
)
from backend.database import audit, connect, initialize_database
from backend.events import record_event, replay_events
from backend.glossaries import list_glossaries, save_glossary
from backend.incidents import (
    INCIDENT_SEVERITIES,
    INCIDENT_STATUSES,
    INCIDENT_TASK_STATUSES,
    INCIDENT_SUMMARY_SELECT,
    get_incident,
    incident_detail,
    incident_row_to_dict,
    task_row_to_dict,
)
from backend.security import (
    APP_KEY_COOKIE,
    APP_KEY_HEADER,
    SESSION_COOKIE,
    app_session,
    app_sign_in_enabled,
    authenticate_user,
    create_initial_admin,
    create_session_token,
    find_user,
    list_users,
    role_allows,
    rotate_session_secret,
    session_lifetime_seconds,
    setup_required,
    upsert_user,
    validate_session_token,
    verify_internal_token,
)
from backend.search import search_transcripts
from backend.readiness import readiness_status, validate_source
from backend.jobs import queue_summary
from backend.model_downloads import (
    all_model_statuses,
    control_download,
    start_download,
)
from backend.transcript_quality import assess_transcript, has_meaningful_transcript


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

RADIO_MODEL_SIZE = os.environ.get("RADIO_MODEL_SIZE", "medium")
RADIO_ENGINE = os.environ.get("RADIO_TRANSCRIPTION_ENGINE", "mlx")
ALLOWED_HOSTS = list(
    dict.fromkeys([RADIO_HOST, "127.0.0.1", "localhost", os.environ.get("RADIO_HOSTNAME", "")])
)
ALLOWED_HOSTS = [host for host in ALLOWED_HOSTS if host]
if "0.0.0.0" in ALLOWED_HOSTS or "::" in ALLOWED_HOSTS:
    # Serving every interface means the Host header can legitimately be any
    # address this Mac answers to, so host filtering cannot narrow it further.
    ALLOWED_HOSTS = ["*"]
HEARTBEAT_STALE_SECONDS = 45
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; media-src 'self'; connect-src 'self' wss:; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)

initialize_database()
app = FastAPI(
    title="Radio Command Center",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)


@app.on_event("startup")
def start_background_maintenance():
    if os.environ.get("RADIO_DISABLE_AUTOMATIC_BACKUP") == "1":
        return
    # Backups, history pruning, log cleanup, and WAL checkpoints keep running
    # for as long as the dashboard does, not just once at startup.
    start_maintenance_thread(logger)


class ConnectionManager:
    def __init__(self):
        self.connections = {}

    @property
    def active_connections(self):
        return list(self.connections)

    @property
    def active_user_count(self):
        return len({details["username"] for details in self.connections.values()})

    def presence_payload(self):
        usernames = {details["username"] for details in self.connections.values()}
        return {
            "event_id": None,
            "type": "presence.changed",
            "resource_type": "presence",
            "resource_id": None,
            "actor": None,
            "payload": {
                "active_users": len(usernames),
                "active_connections": len(self.connections),
            },
            "created_at": datetime.now().isoformat(),
        }

    async def connect(self, websocket: WebSocket, session):
        await websocket.accept()
        self.connections[websocket] = {
            "username": session["u"],
            "display_name": session.get("display_name", session["u"]),
            "role": session["r"],
            "connected_at": datetime.now().isoformat(),
            "last_seen": datetime.now().isoformat(),
        }

    def disconnect(self, websocket: WebSocket):
        self.connections.pop(websocket, None)

    def touch(self, websocket: WebSocket):
        if websocket in self.connections:
            self.connections[websocket]["last_seen"] = datetime.now().isoformat()

    async def broadcast(self, payload):
        message = json.dumps(payload)
        dead_connections = []
        for connection in list(self.connections):
            try:
                await connection.send_text(message)
            except Exception as exc:
                logger.warning("Removing stale dashboard connection: %s", exc)
                dead_connections.append(connection)
        for connection in dead_connections:
            self.disconnect(connection)

    async def broadcast_presence(self):
        await self.broadcast(self.presence_payload())


manager = ConnectionManager()
login_failures = defaultdict(deque)
login_lock = threading.Lock()
stats_lock = threading.Lock()
stats_cache = {"at": 0.0, "payload": None}
LOG_FILE_NAMES = {
    "server": "server.log",
    "worker": "worker.log",
    "sync": "sync.log",
}
LOG_TIMESTAMP_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+)"
    r"(?:\s+-\s+(?P<level>INFO|WARNING|ERROR|CRITICAL)\s+-\s+)?"
)
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class TranscriptPayload(BaseModel):
    id: Optional[int] = None
    filename: str
    transcript_text: str
    timestamp: str
    recorded_at: Optional[str] = None
    quality_score: float = 1.0
    quality_reason: str = ""
    status: str = "ready"


class LoginPayload(BaseModel):
    username: str
    password: str


class SetupPayload(BaseModel):
    display_name: str
    username: str
    password: str


class SetupConfigurationPayload(BaseModel):
    source_dir: str
    transcription_enabled: bool = True
    minimum_free_gb: int = 10
    setup_step: int = 2


class SettingsPayload(BaseModel):
    values: dict


class SourceCheckPayload(BaseModel):
    path: str


class TranscriptUpdatePayload(BaseModel):
    reviewed: Optional[bool] = None
    review_state: Optional[str] = None
    review_resolution: Optional[str] = None
    version: Optional[int] = None
    bookmarked: Optional[bool] = None
    notes: Optional[str] = None
    transcript_text: Optional[str] = None


class TranscriptSelectionPayload(BaseModel):
    selection: str
    version: int


class GlossaryPayload(BaseModel):
    terms: List[str] = Field(default_factory=list)
    language: str = "en"
    version: Optional[int] = None


class ModelControlPayload(BaseModel):
    action: str


class TranscriptionProfilePayload(BaseModel):
    normalize_audio: bool = False
    denoise_audio: bool = False
    language_mode: str = "explicit"
    experimental_enabled: bool = False


class WorkspacePayload(BaseModel):
    name: str
    configuration: dict
    is_shared: bool = False


class PreferencesPayload(BaseModel):
    configuration: dict


class SavedSearchPayload(BaseModel):
    name: str
    configuration: dict


class AlertRulePayload(BaseModel):
    name: str
    description: str = ""
    severity: str = "caution"
    match_mode: str = "whole_word"
    terms: List[str]
    exclusions: List[str] = Field(default_factory=list)
    channels: List[str] = Field(default_factory=list)
    start_time: str = ""
    end_time: str = ""
    minimum_quality: float = 0.0
    cooldown_seconds: int = 60
    sound: str = ""
    requires_ack: bool = False
    escalation_seconds: int = 0
    active: bool = True
    version: Optional[int] = None


class AlertUpdatePayload(BaseModel):
    status: Optional[str] = None
    assigned_to: Optional[str] = None
    resolution_note: Optional[str] = None
    version: int


class AlertTestPayload(BaseModel):
    transcript_text: str
    channel: str = ""
    quality_score: float = 1.0
    recorded_at: Optional[str] = None


class NotificationPreferencesPayload(BaseModel):
    configuration: dict


class IncidentPayload(BaseModel):
    title: str
    severity: str = "informational"
    status: str = "monitoring"
    owner_username: Optional[str] = None
    location: str = ""
    summary: str = ""
    tags: List[str] = Field(default_factory=list)
    started_at: Optional[str] = None
    version: Optional[int] = None


class IncidentTranscriptPayload(BaseModel):
    transcript_id: int


class IncidentTaskPayload(BaseModel):
    description: str
    assigned_to: Optional[str] = None
    due_at: Optional[str] = None


class IncidentTaskUpdatePayload(BaseModel):
    description: Optional[str] = None
    status: Optional[str] = None
    assigned_to: Optional[str] = None
    due_at: Optional[str] = None
    version: int


class IncidentNotePayload(BaseModel):
    note: str


class UserPayload(BaseModel):
    username: str
    display_name: str = ""
    role: str = "viewer"
    password: Optional[str] = None
    active: bool = True


REVIEW_STATES = {
    "unreviewed",
    "in_review",
    "confirmed",
    "corrected",
    "dismissed",
}
TRANSCRIPT_DETAIL_COLUMNS = """
    id, timestamp, recorded_at, recording_year, channel, filename,
    transcript_text,
    raw_transcript_text, quality_score, quality_reason, quality_metrics,
    status, reviewed, review_state, reviewed_by, reviewed_at,
    review_resolution, version, bookmarked, notes, corrected_by,
    corrected_at, transcription_model, retry_transcript_text, retry_model,
    retry_quality_score, retry_quality_reason, retry_quality_metrics,
    retry_status, retry_attempted_at, primary_quality_score,
    primary_quality_reason, primary_quality_metrics, primary_status,
    primary_model, retry_trigger_reason, selection_reason,
    selected_transcript
"""
WORKSPACE_ALLOWED_KEYS = {
    "visible_feeds",
    "feed_order",
    "focused_feed",
    "view_mode",
    "filters",
    "compact",
    "alerts_visible",
}
PREFERENCE_ALLOWED_KEYS = {
    "search_sort",
    "search_page_size",
    "default_search_filters",
    "last_workspace_id",
}
SAVED_SEARCH_ALLOWED_KEYS = {"query", "filters", "sort"}
NOTIFICATION_PREFERENCE_ALLOWED_KEYS = {
    "browser_enabled",
    "minimum_severity",
    "sound_enabled",
    "critical_sound",
    "quiet_start",
    "quiet_end",
}
ALERT_DETAIL_SELECT = """
    ae.id, ae.transcript_id, ae.rule_id, ar.name AS rule_name,
    ae.severity, ae.matched_text, ae.explanation, ae.status,
    ae.assigned_to, ae.acknowledged_by, ae.acknowledged_at,
    ae.resolved_by, ae.resolved_at, ae.resolution_note, ae.version,
    ae.created_at, ae.updated_at, t.channel, t.recorded_at,
    t.transcript_text, ar.requires_ack, ar.escalation_seconds
"""


def secure_headers(response):
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )
    response.headers["Strict-Transport-Security"] = "max-age=31536000"
    if response.headers.get("content-type", "").startswith(("text/html", "application/json")):
        response.headers["Cache-Control"] = "no-store"
    return response


def presented_app_key(headers, cookies):
    """Read the native app's key from a request, ignoring cross-site traffic.

    Only the Mac app can hold this key, because only a process running as the
    Mac account can read security.json. Refusing cross-site requests keeps a
    page the app was navigated to from borrowing the key with a background fetch.
    """
    if headers.get("sec-fetch-site", "").lower() == "cross-site":
        return None
    return headers.get(APP_KEY_HEADER.lower()) or cookies.get(APP_KEY_COOKIE)


def get_session(request):
    cached = getattr(request.state, "radio_session", None)
    if cached is not None:
        return cached or None
    session = validate_session_token(request.cookies.get(SESSION_COOKIE))
    if not session:
        session = app_session(presented_app_key(request.headers, request.cookies))
    request.state.radio_session = session or False
    return session


def require_role(request, minimum_role):
    session = get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not role_allows(session, minimum_role):
        raise HTTPException(status_code=403, detail="Your profile does not have clearance")
    return session


def wants_html(request):
    return "text/html" in request.headers.get("accept", "")


def set_session_cookie(response, token):
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=session_lifetime_seconds(),
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return response


def renew_session_if_needed(request, response):
    """Extend a browser session that is more than halfway through its life.

    Without this the dashboard signs an operator out on a fixed schedule even
    while they are actively using it.
    """
    session = getattr(request.state, "radio_session", None)
    if not session or session.get("via") == "app":
        return response
    expires_at = int(session.get("exp", 0))
    lifetime = session_lifetime_seconds()
    if expires_at - int(time.time()) > lifetime // 2:
        return response
    user = find_user(session.get("u", ""))
    if not user or not user.get("active", True):
        return response
    return set_session_cookie(response, create_session_token(user))


@app.middleware("http")
async def authentication_and_headers(request: Request, call_next):
    path = request.url.path
    is_public = path == "/login" or path == "/api/login" or path.startswith("/static/")
    is_setup_public = path == "/setup" or path.startswith("/api/setup")
    is_internal = path in {"/api/new_transcript", "/api/internal/heartbeat"}

    if is_internal:
        if not verify_internal_token(request.headers.get("X-Radio-Internal-Token")):
            return secure_headers(JSONResponse({"detail": "Forbidden"}, status_code=403))
    elif setup_required() and not get_session(request):
        if not is_setup_public and not path.startswith("/static/"):
            if wants_html(request) and not path.startswith("/api/"):
                return secure_headers(RedirectResponse("/setup", status_code=303))
            return secure_headers(
                JSONResponse(
                    {"detail": "Administrator setup is required."},
                    status_code=409,
                )
            )
    elif not is_public:
        session = get_session(request)
        if not session:
            if wants_html(request) and not path.startswith("/api/"):
                return secure_headers(RedirectResponse("/login", status_code=303))
            return secure_headers(
                JSONResponse({"detail": "Authentication required"}, status_code=401)
            )
        if path.startswith("/audio/") and not role_allows(session, "operator"):
            return secure_headers(
                JSONResponse({"detail": "Audio clearance required"}, status_code=403)
            )

    response = await call_next(request)
    return secure_headers(renew_session_if_needed(request, response))


def client_key(request):
    return request.client.host if request.client else "unknown"


def login_window_seconds():
    return 60 * number_setting(
        "login_window_minutes", default=15, minimum=1, maximum=1440
    )


def maximum_login_failures():
    return number_setting("login_max_failures", default=5, minimum=3, maximum=50)


def login_is_limited(key):
    cutoff = time.time() - login_window_seconds()
    with login_lock:
        # Drop expired attempts for every address so this never grows without
        # bound on an installation that is reachable from a network.
        for address in list(login_failures):
            attempts = login_failures[address]
            while attempts and attempts[0] < cutoff:
                attempts.popleft()
            if not attempts and address != key:
                del login_failures[address]
        return len(login_failures[key]) >= maximum_login_failures()


def record_login_failure(key):
    with login_lock:
        login_failures[key].append(time.time())


def clear_login_failures(key):
    with login_lock:
        login_failures.pop(key, None)


def service_snapshot():
    """What the services are doing right now, for the settings screen."""
    dashboard_host = "127.0.0.1" if RADIO_HOST in {"0.0.0.0", "::"} else RADIO_HOST
    heartbeats = read_heartbeats()
    return {
        "host": RADIO_HOST,
        "port": RADIO_PORT,
        "dashboard_url": f"https://{dashboard_host}:{RADIO_PORT}",
        "network_dashboard_url": (
            f"https://{local_network_address()}:{RADIO_PORT}"
            if RADIO_HOST in {"0.0.0.0", "::"}
            else ""
        ),
        "engine": RADIO_ENGINE,
        "model_size": RADIO_MODEL_SIZE,
        "source_directory": RECORDING_SOURCE_DIR,
        "components": {
            name: {
                "status": details.get("status"),
                "last_seen": details.get("last_seen"),
            }
            for name, details in heartbeats.items()
        },
    }


def local_network_address():
    """Best-effort LAN address other devices can use to reach this Mac."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packets are sent; this only asks the routing table which local
        # address would be used to reach the network.
        probe.connect(("192.0.2.1", 9))
        return probe.getsockname()[0]
    except OSError:
        return socket.gethostname()
    finally:
        probe.close()


def schedule_service_restart(delay_seconds=2):
    """Restart the supervisor from outside this process.

    The dashboard is a child of the supervisor, so it cannot restart the group
    itself; a detached helper does it a moment after the response is sent.
    """
    control_script = os.path.join(PROJECT_ROOT, "scripts", "service_control.py")
    command = (
        f"sleep {int(delay_seconds)}; "
        f"exec {shlex.quote(sys.executable)} {shlex.quote(control_script)} restart"
    )
    subprocess.Popen(
        ["/bin/sh", "-c", command],
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def read_html(filename):
    path = os.path.join(PROJECT_ROOT, "frontend", filename)
    if not os.path.exists(path):
        return HTMLResponse("<h1>Interface file not found.</h1>", status_code=404)
    with open(path, "r", encoding="utf-8") as handle:
        return HTMLResponse(handle.read())


def read_heartbeats():
    with connect(read_only=True) as connection:
        rows = connection.execute(
            "SELECT service, last_seen, status, details FROM service_heartbeats"
        ).fetchall()
    now = datetime.now()
    result = {}
    for row in rows:
        try:
            age = max(0, (now - datetime.fromisoformat(row["last_seen"])).total_seconds())
        except (TypeError, ValueError):
            age = None
        result[row["service"]] = {
            "status": row["status"],
            "last_seen": row["last_seen"],
            "age_seconds": round(age, 1) if age is not None else None,
            "stale": age is None or age > HEARTBEAT_STALE_SECONDS,
            "details": json.loads(row["details"] or "{}"),
        }
    return result


def build_stats():
    now = datetime.now()
    window_start = now - timedelta(minutes=15)
    with connect(read_only=True) as connection:
        row = connection.execute(
            """
            SELECT count(*) AS processed,
                   sum(CASE WHEN status = 'blank' THEN 1 ELSE 0 END) AS blank,
                   sum(CASE WHEN status = 'suspect' THEN 1 ELSE 0 END) AS suspect,
                   sum(CASE WHEN broadcast_pending = 1 THEN 1 ELSE 0 END) AS pending,
                   max(timestamp) AS latest
            FROM transcripts
            """
        ).fetchone()
        recent_count = connection.execute(
            "SELECT count(*) FROM transcripts WHERE timestamp >= ?",
            (window_start.isoformat(),),
        ).fetchone()[0]
        synced_count = connection.execute(
            "SELECT count(*) FROM synced_files"
        ).fetchone()[0]

    processed = row["processed"] or 0
    heartbeats = read_heartbeats()
    sync_details = heartbeats.get("sync", {}).get("details", {})
    source_count = int(sync_details.get("source_files") or 0)
    total_audio = max(processed, synced_count, source_count)
    backlog = max(0, total_audio - processed)
    rate_per_minute = recent_count / 15
    eta_minutes = backlog / rate_per_minute if rate_per_minute > 0 else None
    transcription_enabled = bool(
        load_settings().get("transcription_enabled", True)
    )
    required_services = (
        ("worker", "sync") if transcription_enabled else ("sync",)
    )
    degraded = any(
        name not in heartbeats
        or heartbeats[name]["stale"]
        or heartbeats[name]["status"] not in {"online", "idle"}
        for name in required_services
    )
    return {
        "status": "degraded" if degraded else "online",
        "active_clients": manager.active_user_count,
        "active_connections": len(manager.active_connections),
        "model": RADIO_MODEL_SIZE,
        "retry_model": "large-v3",
        "transcription_enabled": transcription_enabled,
        "engine": RADIO_ENGINE,
        "recordings": total_audio,
        "processed": processed,
        "blank_ignored": row["blank"] or 0,
        "suspect": row["suspect"] or 0,
        "pending_delivery": row["pending"] or 0,
        "backlog": backlog,
        "rate_per_minute": round(rate_per_minute, 1),
        "eta_minutes": round(eta_minutes) if eta_minutes is not None else None,
        "latest_completed_at": row["latest"],
        "source_directory": RECORDING_SOURCE_DIR,
        "source_mounted": os.path.isdir(RECORDING_SOURCE_DIR),
        "services": heartbeats,
    }


def transcript_row_to_dict(row):
    row_keys = set(row.keys())
    review_state = (
        row["review_state"]
        if "review_state" in row_keys and row["review_state"]
        else ("confirmed" if row["reviewed"] else "unreviewed")
    )
    payload = {
        "id": row["id"],
        "timestamp": row["timestamp"],
        "recorded_at": row["recorded_at"],
        "recording_year": (
            row["recording_year"] if "recording_year" in row_keys else None
        ),
        "channel": row["channel"] if "channel" in row_keys else None,
        "filename": row["filename"],
        "transcript_text": row["transcript_text"] or "",
        "quality_score": row["quality_score"],
        "quality_reason": row["quality_reason"] or "",
        "status": row["status"],
        "reviewed": review_state in {"confirmed", "corrected"},
        "review_state": review_state,
        "reviewed_by": row["reviewed_by"] if "reviewed_by" in row_keys else None,
        "reviewed_at": row["reviewed_at"] if "reviewed_at" in row_keys else None,
        "review_resolution": (
            row["review_resolution"] or ""
            if "review_resolution" in row_keys
            else ""
        ),
        "version": row["version"] if "version" in row_keys else 1,
        "bookmarked": bool(row["bookmarked"]),
        "notes": row["notes"] or "",
        "corrected_by": row["corrected_by"],
        "corrected_at": row["corrected_at"],
        "transcription_model": row["transcription_model"],
        "retry_status": row["retry_status"],
    }
    for column in (
        "raw_transcript_text",
        "quality_metrics",
        "retry_transcript_text",
        "retry_model",
        "retry_quality_score",
        "retry_quality_reason",
        "retry_quality_metrics",
        "retry_attempted_at",
        "primary_quality_score",
        "primary_quality_reason",
        "primary_quality_metrics",
        "primary_status",
        "primary_model",
        "retry_trigger_reason",
        "selection_reason",
        "selected_transcript",
    ):
        if column in row_keys:
            payload[column] = row[column]
    if "snippet" in row_keys:
        payload["snippet"] = row["snippet"] or ""
    if "rank_score" in row_keys:
        payload["rank_score"] = row["rank_score"]
    return payload


def transcript_filter(
    query="",
    channel="",
    after_id=None,
    before_id=None,
    date_value="",
    start_time="",
    end_time="",
    include_suspect=False,
    bookmarked=False,
    through_id=None,
):
    clauses = ["status != 'blank'"]
    parameters = []
    if not include_suspect:
        clauses.append("status = 'ready'")
    if after_id is not None:
        clauses.append("id > ?")
        parameters.append(after_id)
    if before_id is not None:
        clauses.append(
            """
            (
                coalesce(recorded_at, timestamp) < (
                    SELECT coalesce(recorded_at, timestamp)
                    FROM transcripts WHERE id = ?
                )
                OR (
                    coalesce(recorded_at, timestamp) = (
                        SELECT coalesce(recorded_at, timestamp)
                        FROM transcripts WHERE id = ?
                    )
                    AND id < ?
                )
            )
            """
        )
        parameters.extend([before_id, before_id, before_id])
    if query:
        like = f"%{query.lower()}%"
        clauses.append(
            """
            (lower(coalesce(transcript_text, '')) LIKE ?
             OR lower(filename) LIKE ?
             OR lower(coalesce(notes, '')) LIKE ?)
            """
        )
        parameters.extend([like, like, like])
    if channel:
        clauses.append("channel = ?")
        parameters.append(channel)
    effective_time = "coalesce(recorded_at, timestamp)"
    if date_value:
        clauses.append(f"substr({effective_time}, 1, 10) = ?")
        parameters.append(date_value)
    if start_time and end_time and start_time > end_time:
        clauses.append(
            f"(substr({effective_time}, 12, 5) >= ? OR "
            f"substr({effective_time}, 12, 5) <= ?)"
        )
        parameters.extend([start_time, end_time])
    else:
        if start_time:
            clauses.append(f"substr({effective_time}, 12, 5) >= ?")
            parameters.append(start_time)
        if end_time:
            clauses.append(f"substr({effective_time}, 12, 5) <= ?")
            parameters.append(end_time)
    if bookmarked:
        clauses.append("bookmarked = 1")
    if through_id is not None:
        clauses.append("id <= ?")
        parameters.append(through_id)
    return " AND ".join(clauses), parameters


def query_transcripts(
    query="",
    channel="",
    after_id=None,
    before_id=None,
    limit=500,
    date_value="",
    start_time="",
    end_time="",
    include_suspect=False,
    bookmarked=False,
    through_id=None,
):
    where_clause, parameters = transcript_filter(
        query=query,
        channel=channel,
        after_id=after_id,
        before_id=before_id,
        date_value=date_value,
        start_time=start_time,
        end_time=end_time,
        include_suspect=include_suspect,
        bookmarked=bookmarked,
        through_id=through_id,
    )

    order_by = (
        "id ASC"
        if after_id is not None
        else "coalesce(recorded_at, timestamp) DESC, id DESC"
    )
    parameters.append(max(1, min(limit, 2000)))
    sql = f"""
        SELECT id, timestamp, recorded_at, recording_year, channel,
               filename, transcript_text,
               quality_score, quality_reason, status, reviewed, bookmarked,
               notes, corrected_by, corrected_at, transcription_model,
               retry_status, review_state, reviewed_by, reviewed_at,
               review_resolution, version
        FROM transcripts
        WHERE {where_clause}
        ORDER BY {order_by}
        LIMIT ?
    """
    with connect(read_only=True) as connection:
        rows = connection.execute(sql, parameters).fetchall()
    if after_id is None:
        rows = list(reversed(rows))
    return [transcript_row_to_dict(row) for row in rows]


def export_query_parts(
    query="",
    date_value="",
    start_time="",
    end_time="",
    bookmarked=False,
    through_id=None,
):
    return transcript_filter(
        query=query,
        date_value=date_value,
        start_time=start_time,
        end_time=end_time,
        include_suspect=True,
        bookmarked=bookmarked,
        through_id=through_id,
    )


def count_export_rows(**filters):
    where_clause, parameters = export_query_parts(**filters)
    with connect(read_only=True) as connection:
        return connection.execute(
            f"SELECT count(*) FROM transcripts WHERE {where_clause}",
            parameters,
        ).fetchone()[0]


def stream_export_csv(**filters):
    where_clause, parameters = export_query_parts(**filters)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Recorded",
            "Channel/File",
            "Transcript",
            "Status",
            "Reviewed",
            "Bookmarked",
            "Notes",
        ]
    )
    yield output.getvalue()

    with connect(read_only=True) as connection:
        cursor = connection.execute(
            f"""
            SELECT timestamp, recorded_at, filename, transcript_text,
                   status, reviewed, bookmarked, notes
            FROM transcripts
            WHERE {where_clause}
            ORDER BY coalesce(recorded_at, timestamp) ASC, id ASC
            """,
            parameters,
        )
        while rows := cursor.fetchmany(500):
            output.seek(0)
            output.truncate(0)
            for row in rows:
                writer.writerow(
                    [
                        row["recorded_at"] or row["timestamp"],
                        row["filename"],
                        row["transcript_text"] or "",
                        row["status"],
                        bool(row["reviewed"]),
                        bool(row["bookmarked"]),
                        row["notes"] or "",
                    ]
                )
            yield output.getvalue()


def read_log_tail(path, line_limit, byte_limit=512 * 1024):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            chunks = []
            newline_count = 0
            bytes_read = 0
            while position > 0 and newline_count <= line_limit and bytes_read < byte_limit:
                chunk_size = min(16 * 1024, position, byte_limit - bytes_read)
                position -= chunk_size
                handle.seek(position)
                chunk = handle.read(chunk_size)
                chunks.append(chunk)
                bytes_read += len(chunk)
                newline_count += chunk.count(b"\n")
    except OSError:
        return []
    content = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
    return content.splitlines()[-line_limit:]


def infer_log_level(line):
    lowered = line.lower()
    if any(word in lowered for word in ("critical", "traceback", "exception", "error", "failed")):
        return "error"
    if any(word in lowered for word in ("warning", "warn", "unavailable", "paused", "waiting")):
        return "warning"
    return "info"


def console_log_entries(service, line_limit):
    services = list(LOG_FILE_NAMES) if service == "all" else [service]
    entries = []
    for source in services:
        path = os.path.join(LOG_DIR, LOG_FILE_NAMES[source])
        try:
            fallback_time = os.path.getmtime(path)
        except OSError:
            fallback_time = time.time()
        for line_number, raw_line in enumerate(read_log_tail(path, line_limit)):
            message = ANSI_ESCAPE_PATTERN.sub("", raw_line).strip()
            if not message:
                continue
            match = LOG_TIMESTAMP_PATTERN.match(message)
            timestamp = (
                match.group("timestamp").replace(" ", "T")
                if match
                else datetime.fromtimestamp(
                    fallback_time + line_number / 1_000_000
                ).isoformat()
            )
            explicit_level = match.group("level").lower() if match and match.group("level") else ""
            level = infer_log_level(message) if not explicit_level else explicit_level
            signature = zlib.crc32(
                f"{source}:{line_number}:{message}".encode("utf-8", errors="replace")
            )
            entries.append(
                {
                    "id": f"{source}-{signature:08x}",
                    "timestamp": timestamp,
                    "source": source,
                    "level": "error" if level == "critical" else level,
                    "message": message[:4000],
                }
            )
    entries.sort(key=lambda entry: (entry["timestamp"], entry["source"], entry["id"]))
    return entries[-line_limit:]


def _heartbeat_problem(heartbeat):
    if heartbeat is None:
        return "No heartbeat has been recorded"
    details = heartbeat.get("details") or {}
    return str(details.get("error") or details.get("reason") or "")[:500]


def build_health_center():
    now = datetime.now()
    heartbeats = read_heartbeats()
    with connect(read_only=True) as connection:
        transcript_stats = connection.execute(
            """
            SELECT count(*) AS processed,
                   sum(CASE WHEN broadcast_pending = 1 THEN 1 ELSE 0 END) AS pending,
                   min(CASE WHEN broadcast_pending = 1 THEN timestamp END) AS oldest_pending,
                   max(CASE WHEN status = 'ready' THEN timestamp END) AS latest_ready,
                   max(CASE WHEN broadcast_pending = 0 AND status = 'ready'
                            THEN timestamp END) AS latest_delivered,
                   max(last_broadcast_error) AS delivery_error
            FROM transcripts
            """
        ).fetchone()
        sync_stats = connection.execute(
            """
            SELECT count(*) AS synced,
                   max(synced_at) AS latest_synced,
                   coalesce(sum(source_size), 0) AS synced_bytes
            FROM synced_files
            """
        ).fetchone()
        recent_count = connection.execute(
            "SELECT count(*) FROM transcripts WHERE timestamp >= ?",
            ((now - timedelta(minutes=15)).isoformat(),),
        ).fetchone()[0]

    sync_heartbeat = heartbeats.get("sync")
    worker_heartbeat = heartbeats.get("worker")
    sync_details = (sync_heartbeat or {}).get("details", {})
    worker_details = (worker_heartbeat or {}).get("details", {})
    source_mounted = os.path.isdir(RECORDING_SOURCE_DIR)
    if not source_mounted:
        source_status = "critical"
        source_summary = "Recording source is unavailable"
    elif sync_heartbeat is None or sync_heartbeat["stale"]:
        source_status = "caution"
        source_summary = "Source is mounted, but its sync heartbeat is stale"
    else:
        source_status = "healthy"
        source_summary = "Recording source is reachable"

    if sync_heartbeat is None or sync_heartbeat["stale"]:
        sync_status = "critical"
        sync_summary = "Recording sync is not reporting"
    elif sync_heartbeat["status"] in {"error"}:
        sync_status = "critical"
        sync_summary = "Recording sync needs intervention"
    elif sync_heartbeat["status"] in {"paused", "waiting"}:
        sync_status = "caution"
        sync_summary = "Recording sync is paused or waiting"
    else:
        sync_status = "healthy"
        sync_summary = "Recording sync is current"

    transcription_enabled = bool(load_settings().get("transcription_enabled", True))
    queue_depth = int(worker_details.get("queue_depth") or 0)
    rescue_depth = int(worker_details.get("rescue_queue_depth") or 0)
    if not transcription_enabled:
        transcription_status = "paused"
        transcription_summary = "Transcription is intentionally paused"
    elif worker_heartbeat is None or worker_heartbeat["stale"]:
        transcription_status = "critical"
        transcription_summary = "Transcription worker is not reporting"
    elif worker_heartbeat["status"] == "error":
        transcription_status = "critical"
        transcription_summary = "Transcription worker reported an error"
    elif queue_depth >= 100:
        transcription_status = "caution"
        transcription_summary = "The live transcription queue is growing"
    else:
        transcription_status = "healthy"
        transcription_summary = "Transcription worker is ready"

    pending_delivery = int(transcript_stats["pending"] or 0)
    # Delivery runs inside the transcription worker, so pausing transcription
    # also stops it. Anything already finished then waits by design rather than
    # because the dashboard server is unwell.
    if pending_delivery and not transcription_enabled:
        delivery_status = "paused"
        delivery_summary = (
            "Delivery is paused with transcription; finished transcripts are "
            "saved and resume delivery when transcription restarts"
        )
        delivery_action = (
            "Start transcription when you are ready; no transcripts are lost "
            "while it is paused."
        )
    elif pending_delivery >= 20:
        delivery_status = "critical"
        delivery_summary = "Dashboard delivery has a significant backlog"
        delivery_action = (
            "Verify the dashboard server, then allow durable delivery retries "
            "to catch up."
        )
    elif pending_delivery:
        delivery_status = "caution"
        delivery_summary = "Some transcripts are waiting for delivery"
        delivery_action = (
            "Verify the dashboard server, then allow durable delivery retries "
            "to catch up."
        )
    else:
        delivery_status = "healthy"
        delivery_summary = "Dashboard delivery is caught up"
        delivery_action = (
            "Verify the dashboard server, then allow durable delivery retries "
            "to catch up."
        )

    try:
        disk = shutil.disk_usage(AUDIO_DIR)
        free_ratio = disk.free / disk.total if disk.total else 0
        if disk.free < 1 * 1024**3:
            storage_status = "critical"
            storage_summary = "Less than 1 GB of recording storage remains"
        elif disk.free < 5 * 1024**3 or free_ratio < 0.05:
            storage_status = "caution"
            storage_summary = "Recording storage is approaching its safety reserve"
        else:
            storage_status = "healthy"
            storage_summary = "Recording storage has adequate free space"
        storage_metrics = {
            "free_bytes": disk.free,
            "used_bytes": disk.used,
            "total_bytes": disk.total,
            "free_percent": round(free_ratio * 100, 1),
        }
    except OSError as exc:
        storage_status = "critical"
        storage_summary = "Recording storage cannot be inspected"
        storage_metrics = {"error": str(exc)[:500]}

    components = [
        {
            "id": "source",
            "label": "Source",
            "status": source_status,
            "summary": source_summary,
            "last_success_at": sync_stats["latest_synced"],
            "heartbeat_age_seconds": (sync_heartbeat or {}).get("age_seconds"),
            "queue_depth": int(sync_details.get("source_files") or 0),
            "last_error": _heartbeat_problem(sync_heartbeat),
            "action": "Reconnect or select the recording source, then verify read access.",
            "console_service": "sync",
            "metrics": {
                "mounted": source_mounted,
                "synced_files": sync_stats["synced"],
                "source_files": sync_details.get("source_files"),
            },
        },
        {
            "id": "sync",
            "label": "Sync",
            "status": sync_status,
            "summary": sync_summary,
            "last_success_at": sync_stats["latest_synced"],
            "heartbeat_age_seconds": (sync_heartbeat or {}).get("age_seconds"),
            "queue_depth": max(
                0,
                int(sync_details.get("source_files") or 0)
                - int(sync_stats["synced"] or 0),
            ),
            "last_error": _heartbeat_problem(sync_heartbeat),
            "action": "Inspect sync events and retry after the source and destination are writable.",
            "console_service": "sync",
            "metrics": {
                "copied_this_pass": sync_details.get("copied_this_pass", sync_details.get("copied", 0)),
                "failed_this_pass": sync_details.get("failed", 0),
                "synced_bytes": sync_stats["synced_bytes"],
            },
        },
        {
            "id": "transcription",
            "label": "Transcription",
            "status": transcription_status,
            "summary": transcription_summary,
            "last_success_at": transcript_stats["latest_ready"],
            "heartbeat_age_seconds": (worker_heartbeat or {}).get("age_seconds"),
            "queue_depth": queue_depth,
            "last_error": _heartbeat_problem(worker_heartbeat),
            "action": "Inspect worker events; restart only after active work is safely clear.",
            "console_service": "worker",
            "metrics": {
                "processed": transcript_stats["processed"],
                "rescue_queue_depth": rescue_depth,
                "throughput_per_minute": round(recent_count / 15, 1),
                "model": worker_details.get("model", RADIO_MODEL_SIZE),
            },
        },
        {
            "id": "delivery",
            "label": "Delivery",
            "status": delivery_status,
            "summary": delivery_summary,
            "last_success_at": transcript_stats["latest_delivered"],
            "heartbeat_age_seconds": (worker_heartbeat or {}).get("age_seconds"),
            "queue_depth": pending_delivery,
            "last_error": str(transcript_stats["delivery_error"] or "")[:500],
            "action": delivery_action,
            "console_service": "server",
            "metrics": {"oldest_waiting_at": transcript_stats["oldest_pending"]},
        },
        {
            "id": "storage",
            "label": "Storage",
            "status": storage_status,
            "summary": storage_summary,
            "last_success_at": now.isoformat(),
            "heartbeat_age_seconds": None,
            "queue_depth": None,
            "last_error": str(storage_metrics.get("error") or ""),
            "action": "Free or extend recording storage before the safety reserve is reached.",
            "console_service": "sync",
            "metrics": storage_metrics,
        },
    ]
    overall = "healthy"
    if any(component["status"] == "critical" for component in components):
        overall = "critical"
    elif any(component["status"] in {"caution", "paused"} for component in components):
        overall = "caution"
    return {"status": overall, "generated_at": now.isoformat(), "components": components}


def _redact_diagnostic_text(value):
    text_value = str(value or "")
    for path, replacement in (
        (PROJECT_ROOT, "<project>"),
        (AUDIO_DIR, "<recordings>"),
        (RECORDING_SOURCE_DIR, "<source>"),
        (os.path.expanduser("~"), "<home>"),
    ):
        if path:
            text_value = text_value.replace(path, replacement)
    text_value = re.sub(
        r"(?i)(password|token|secret|authorization)(\s*[:=]\s*)[^\s,;]+",
        r"\1\2<redacted>",
        text_value,
    )
    return text_value


def build_shift_handoff(hours=12):
    generated_at = datetime.now()
    cutoff = (generated_at - timedelta(hours=hours)).isoformat()
    with connect(read_only=True) as connection:
        incident_rows = connection.execute(
            f"""
            {INCIDENT_SUMMARY_SELECT}
            WHERE i.status != 'resolved'
            ORDER BY CASE i.severity
                WHEN 'critical' THEN 4 WHEN 'urgent' THEN 3
                WHEN 'caution' THEN 2 ELSE 1 END DESC,
                i.updated_at DESC
            """
        ).fetchall()
        alert_rows = connection.execute(
            f"""
            SELECT {ALERT_DETAIL_SELECT}
            FROM alert_events ae
            JOIN alert_rules ar ON ar.id = ae.rule_id
            JOIN transcripts t ON t.id = ae.transcript_id
            WHERE ae.status IN ('open', 'acknowledged')
            ORDER BY CASE ae.severity
                WHEN 'critical' THEN 4 WHEN 'urgent' THEN 3
                WHEN 'caution' THEN 2 ELSE 1 END DESC,
                ae.created_at DESC
            LIMIT 100
            """
        ).fetchall()
        bookmark_rows = connection.execute(
            f"""
            SELECT {TRANSCRIPT_DETAIL_COLUMNS}
            FROM transcripts
            WHERE bookmarked = 1
              AND coalesce(recorded_at, timestamp) >= ?
            ORDER BY coalesce(recorded_at, timestamp) DESC, id DESC
            LIMIT 100
            """,
            (cutoff,),
        ).fetchall()
    health = build_health_center()
    return {
        "generated_at": generated_at.isoformat(),
        "window_hours": hours,
        "incidents": [incident_row_to_dict(row) for row in incident_rows],
        "alerts": [alert_row_to_dict(row) for row in alert_rows],
        "bookmarks": [transcript_row_to_dict(row) for row in bookmark_rows],
        "system_issues": [
            component for component in health["components"]
            if component["status"] != "healthy"
        ],
        "counts": {
            "open_incidents": len(incident_rows),
            "active_alerts": len(alert_rows),
            "recent_bookmarks": len(bookmark_rows),
            "system_issues": sum(
                component["status"] != "healthy"
                for component in health["components"]
            ),
        },
    }


def format_shift_handoff(report):
    lines = [
        "RADIO COMMAND CENTER SHIFT HANDOFF",
        f"Generated: {report['generated_at']}",
        f"Window: {report['window_hours']} hours",
        "",
        f"OPEN INCIDENTS ({report['counts']['open_incidents']})",
    ]
    for incident in report["incidents"]:
        lines.append(
            f"- [{incident['severity'].upper()}] {incident['title']} "
            f"({incident['status']}; owner: {incident['owner_username'] or 'unassigned'}; "
            f"{incident['open_task_count']} open tasks)"
        )
    lines.extend(["", f"ACTIVE ALERTS ({report['counts']['active_alerts']})"])
    for alert in report["alerts"]:
        lines.append(
            f"- [{alert['severity'].upper()}] {alert['rule_name']} on "
            f"{alert['channel'] or 'unknown channel'} ({alert['status']})"
        )
    lines.extend(["", f"RECENT BOOKMARKS ({report['counts']['recent_bookmarks']})"])
    for transcript in report["bookmarks"]:
        excerpt = " ".join(transcript["transcript_text"].split())[:140]
        lines.append(f"- {transcript['channel'] or 'Unknown'}: {excerpt}")
    lines.extend(["", f"SYSTEM ISSUES ({report['counts']['system_issues']})"])
    for component in report["system_issues"]:
        lines.append(f"- {component['label']}: {component['summary']}")
    if len(lines) == 8:
        lines.append("- No outstanding handoff items.")
    return "\n".join(lines) + "\n"


app.mount("/static", StaticFiles(directory=os.path.join(PROJECT_ROOT, "frontend")), name="static")
app.mount("/audio", StaticFiles(directory=AUDIO_DIR), name="audio")


@app.get("/login")
async def login_page(request: Request):
    # The native Mac app authenticates itself, so it is never shown this page.
    if get_session(request):
        return RedirectResponse("/", status_code=303)
    if setup_required():
        return RedirectResponse("/setup", status_code=303)
    return read_html("login.html")


@app.get("/setup")
async def setup_page():
    if not setup_required():
        return RedirectResponse("/login", status_code=303)
    return read_html("setup.html")


@app.get("/api/setup/status")
def setup_status():
    if not setup_required():
        raise HTTPException(status_code=409, detail="Initial setup is complete")
    return readiness_status()


@app.post("/api/setup/configure")
def configure_initial_setup(payload: SetupConfigurationPayload):
    if not setup_required():
        raise HTTPException(status_code=409, detail="Initial setup is complete")
    source = validate_source(payload.source_dir)
    if not source["readable"]:
        raise HTTPException(status_code=400, detail=source["error"])
    minimum_free_gb = max(2, min(500, int(payload.minimum_free_gb)))
    save_settings(
        {
            "source_dir": source["path"],
            "transcription_enabled": bool(payload.transcription_enabled),
            "minimum_free_bytes": minimum_free_gb * 1024**3,
            "setup_step": max(1, min(7, int(payload.setup_step))),
        }
    )
    return readiness_status()


@app.post("/api/setup/models/{model_key}")
def setup_model_download(model_key: str):
    if not setup_required():
        raise HTTPException(status_code=409, detail="Initial setup is complete")
    if model_key != "medium":
        raise HTTPException(status_code=400, detail="Only Medium is required during setup")
    try:
        return start_download(model_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/setup")
async def initial_setup(payload: SetupPayload):
    try:
        user = create_initial_admin(
            payload.username,
            payload.display_name,
            payload.password,
        )
    except ValueError as exc:
        status_code = 409 if "already been completed" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    save_settings({"setup_step": 8, "setup_completed_at": datetime.now().isoformat()})
    return set_session_cookie(
        JSONResponse({"status": "success"}),
        create_session_token(user),
    )


@app.post("/api/login")
async def login(payload: LoginPayload, request: Request):
    key = client_key(request)
    if login_is_limited(key):
        return JSONResponse(
            {
                "detail": "Too many failed attempts. Try again in "
                f"{login_window_seconds() // 60} minutes."
            },
            status_code=429,
        )
    user = authenticate_user(payload.username, payload.password)
    if not user:
        record_login_failure(key)
        return JSONResponse({"detail": "Invalid username or password."}, status_code=401)
    clear_login_failures(key)
    return set_session_cookie(
        JSONResponse({"status": "success"}),
        create_session_token(user),
    )


@app.post("/api/logout")
async def logout():
    response = JSONResponse({"status": "success"})
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True)
    return response


@app.get("/")
async def get_dashboard():
    return read_html("index.html")


@app.get("/api/me")
def get_current_profile(request: Request):
    session = require_role(request, "viewer")
    return {
        "username": session["u"],
        "display_name": session.get("display_name", session["u"]),
        "role": session["r"],
        # "app" means this dashboard is running inside the native Mac app and
        # signed itself in; the browser dashboard reports "session" instead.
        "auth_source": session.get("via", "session"),
        "setup_pending": setup_required(),
        "permissions": {
            "audio": role_allows(session, "operator"),
            "review": role_allows(session, "operator"),
            "correct": role_allows(session, "supervisor"),
            "export": role_allows(session, "supervisor"),
            "profiles": role_allows(session, "admin"),
            "suspect": role_allows(session, "supervisor"),
            "console": role_allows(session, "admin"),
            "alerts": role_allows(session, "viewer"),
            "acknowledge_alerts": role_allows(session, "operator"),
            "manage_alert_rules": role_allows(session, "supervisor"),
            "incidents": role_allows(session, "viewer"),
            "manage_incidents": role_allows(session, "operator"),
            "diagnostics": role_allows(session, "admin"),
        },
    }


@app.get("/api/health")
def health_check():
    return {
        "status": build_stats()["status"],
        "database_exists": os.path.exists(DB_NAME),
        "source_mounted": os.path.isdir(RECORDING_SOURCE_DIR),
    }


@app.get("/api/readiness")
def deployment_readiness(request: Request):
    require_role(request, "admin")
    result = readiness_status()
    for check in result["checks"]:
        if check["id"] == "administrator":
            check["ready"] = True
    return result


@app.get("/api/settings")
def get_settings(request: Request):
    require_role(request, "admin")
    document = settings_document()
    document["service"] = service_snapshot()
    return document


@app.put("/api/settings")
def update_settings(payload: SettingsPayload, request: Request):
    session = require_role(request, "admin")
    try:
        result = apply_settings(payload.values)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(
        session["u"],
        "settings.updated",
        details={"keys": sorted(payload.values)},
    )
    document = settings_document()
    document["service"] = service_snapshot()
    document["restart_required"] = result["restart_required"]
    document["warnings"] = result["warnings"]
    return document


@app.post("/api/settings/source-check")
def check_source_folder(payload: SourceCheckPayload, request: Request):
    require_role(request, "admin")
    return validate_source(payload.path)


@app.post("/api/security/sign-out-browsers")
def sign_out_every_browser(request: Request):
    session = require_role(request, "admin")
    reset_at = rotate_session_secret()
    audit(session["u"], "security.sessions_reset")
    detail = (
        "Every browser has been signed out. This Mac app stays signed in."
        if session.get("via") == "app"
        else "Every browser has been signed out, including this one."
    )
    return {"status": "success", "reset_at": reset_at, "detail": detail}


@app.post("/api/services/restart")
def restart_services(request: Request):
    session = require_role(request, "admin")
    audit(session["u"], "services.restart")
    try:
        schedule_service_restart()
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"The services could not be restarted: {exc}",
        ) from exc
    return {
        "status": "restarting",
        "detail": "Services are restarting. The dashboard reconnects automatically.",
    }


@app.get("/api/stats")
def get_stats():
    with stats_lock:
        if stats_cache["payload"] is not None and time.time() - stats_cache["at"] < 10:
            return stats_cache["payload"]
        payload = build_stats()
        stats_cache.update({"at": time.time(), "payload": payload})
        return payload


@app.get("/api/health-center")
def get_health_center(request: Request):
    require_role(request, "viewer")
    return build_health_center()


@app.get("/api/shift-handoff")
def get_shift_handoff(
    request: Request,
    hours: int = 12,
    format: str = "json",
):
    require_role(request, "viewer")
    bounded_hours = max(1, min(int(hours), 72))
    report = build_shift_handoff(bounded_hours)
    if format == "text":
        return StreamingResponse(
            iter([format_shift_handoff(report)]),
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": "attachment; filename=radio-shift-handoff.txt"
            },
        )
    if format != "json":
        raise HTTPException(status_code=400, detail="Unsupported handoff format")
    return report


@app.get("/api/diagnostics.zip")
def export_diagnostics(request: Request):
    session = require_role(request, "admin")
    generated_at = datetime.now()
    health = build_health_center()
    handoff = build_shift_handoff(12)
    with connect(read_only=True) as connection:
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        counts = {}
        for table in (
            "transcripts",
            "events",
            "alert_events",
            "incidents",
            "incident_tasks",
        ):
            counts[table] = connection.execute(
                f"SELECT count(*) FROM {table}"
            ).fetchone()[0]
    diagnostic_summary = {
        "generated_at": generated_at.isoformat(),
        "generated_by": session["u"],
        "application": app.title,
        "engine": RADIO_ENGINE,
        "model": RADIO_MODEL_SIZE,
        "database_quick_check": integrity,
        "record_counts": counts,
    }
    diagnostic_handoff = {
        **handoff,
        "bookmarks": [
            {**bookmark, "transcript_text": "<redacted transmission>"}
            for bookmark in handoff["bookmarks"]
        ],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "diagnostics.json",
            json.dumps(diagnostic_summary, indent=2, sort_keys=True),
        )
        archive.writestr(
            "health-center.json",
            json.dumps(health, indent=2, sort_keys=True),
        )
        archive.writestr(
            "shift-handoff.txt",
            _redact_diagnostic_text(format_shift_handoff(diagnostic_handoff)),
        )
        for service in LOG_FILE_NAMES:
            entries = console_log_entries(service, 300)
            lines = [
                _redact_diagnostic_text(
                    f"{entry['timestamp']} {entry['level'].upper()} {entry['message']}"
                )
                for entry in entries
            ]
            archive.writestr(f"logs/{service}.log", "\n".join(lines) + "\n")
    audit(session["u"], "export_diagnostics", details={"generated_at": generated_at.isoformat()})
    buffer.seek(0)
    filename = f"radio-diagnostics-{generated_at:%Y%m%d-%H%M%S}.zip"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/api/console")
def get_console(
    request: Request,
    service: str = "all",
    lines: int = 250,
):
    require_role(request, "admin")
    if service not in {"all", *LOG_FILE_NAMES.keys()}:
        raise HTTPException(status_code=400, detail="Unknown console service")
    return {
        "entries": console_log_entries(service, max(20, min(lines, 500))),
        "services": list(LOG_FILE_NAMES),
    }


@app.get("/api/history")
def get_history(
    request: Request,
    q: str = Query("", max_length=200),
    channel: str = Query("", max_length=160),
    after_id: Optional[int] = None,
    before_id: Optional[int] = None,
    limit: int = 500,
    date: str = "",
    start: str = "",
    end: str = "",
    include_suspect: bool = False,
    bookmarked: bool = False,
):
    session = require_role(request, "viewer")
    if after_id is not None and before_id is not None:
        raise HTTPException(
            status_code=400,
            detail="Choose either newer or older archive pagination",
        )
    include_suspect = include_suspect and role_allows(session, "supervisor")
    with connect(read_only=True) as connection:
        high_watermark = connection.execute(
            "SELECT coalesce(max(id), 0) FROM transcripts"
        ).fetchone()[0]
        event_high_watermark = connection.execute(
            "SELECT coalesce(max(id), 0) FROM events"
        ).fetchone()[0]
    rows = query_transcripts(
        query=q.strip(),
        channel=channel.strip(),
        after_id=after_id,
        before_id=before_id,
        limit=limit,
        date_value=date,
        start_time=start,
        end_time=end,
        include_suspect=include_suspect,
        bookmarked=bookmarked,
    )
    return JSONResponse(
        rows,
        headers={
            "X-Radio-High-Watermark": str(high_watermark),
            "X-Radio-Event-Watermark": str(event_high_watermark),
        },
    )


@app.get("/api/search")
def search_archive(
    request: Request,
    q: str = Query(..., min_length=1, max_length=300),
    cursor: str = Query("", max_length=500),
    limit: int = 50,
    sort: str = "relevance",
    channel: str = Query("", max_length=160),
    year: Optional[int] = None,
    date_from: str = "",
    date_to: str = "",
    start: str = "",
    end: str = "",
    status: str = "",
    review_state: str = "",
    reviewer: str = Query("", max_length=80),
    bookmarked: Optional[bool] = None,
    model: str = Query("", max_length=200),
):
    session = require_role(request, "viewer")
    include_suspect = status == "suspect" and role_allows(session, "supervisor")
    try:
        with connect(read_only=True) as connection:
            result = search_transcripts(
                connection,
                query=q,
                cursor=cursor,
                limit=limit,
                sort=sort,
                channel=channel.strip(),
                year=year,
                date_from=date_from,
                date_to=date_to,
                start_time=start,
                end_time=end,
                status=status,
                review_state=review_state,
                reviewer=reviewer.strip(),
                bookmarked=bookmarked,
                model=model.strip(),
                include_suspect=include_suspect,
            )
    except (ValueError, sqlite3.OperationalError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "items": [transcript_row_to_dict(row) for row in result["rows"]],
        "count": result["count"],
        "next_cursor": result["next_cursor"],
        "elapsed_ms": result["elapsed_ms"],
        "parsed_query": result["parsed_query"],
        "sort": result["sort"],
    }


@app.get("/api/archive/facets")
def archive_facets(request: Request):
    session = require_role(request, "viewer")
    routine_status = (
        "status != 'blank'"
        if role_allows(session, "supervisor")
        else "status = 'ready'"
    )
    with connect(read_only=True) as connection:
        channels = connection.execute(
            f"""
            SELECT channel AS value, count(*) AS count
            FROM transcripts
            WHERE {routine_status} AND channel IS NOT NULL AND channel != ''
            GROUP BY channel
            ORDER BY lower(channel)
            """
        ).fetchall()
        years = connection.execute(
            f"""
            SELECT recording_year AS value, count(*) AS count
            FROM transcripts
            WHERE {routine_status} AND recording_year IS NOT NULL
            GROUP BY recording_year
            ORDER BY recording_year DESC
            """
        ).fetchall()
        models = connection.execute(
            f"""
            SELECT transcription_model AS value, count(*) AS count
            FROM transcripts
            WHERE {routine_status}
              AND transcription_model IS NOT NULL
              AND transcription_model != ''
            GROUP BY transcription_model
            ORDER BY lower(transcription_model)
            """
        ).fetchall()
        reviewers = connection.execute(
            f"""
            SELECT reviewed_by AS value, count(*) AS count
            FROM transcripts
            WHERE {routine_status}
              AND reviewed_by IS NOT NULL
              AND reviewed_by != ''
            GROUP BY reviewed_by
            ORDER BY lower(reviewed_by)
            """
        ).fetchall()
        total = connection.execute(
            f"SELECT count(*) FROM transcripts WHERE {routine_status}"
        ).fetchone()[0]
    facet_payload = lambda rows: [
        {"value": row["value"], "count": row["count"]} for row in rows
    ]
    return {
        "total": total,
        "channels": facet_payload(channels),
        "years": facet_payload(years),
        "models": facet_payload(models),
        "reviewers": facet_payload(reviewers),
    }


@app.get("/api/archive/years")
def archive_years(request: Request):
    require_role(request, "viewer")
    with connect(read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT recording_year AS year,
                   count(*) AS transcript_count,
                   min(coalesce(recorded_at, timestamp)) AS first_recording,
                   max(coalesce(recorded_at, timestamp)) AS last_recording
            FROM transcripts
            WHERE status != 'blank' AND recording_year IS NOT NULL
            GROUP BY recording_year
            ORDER BY recording_year
            """
        ).fetchall()
        imports = connection.execute(
            """
            SELECT source_count, imported_count, imported_at
            FROM archive_imports
            ORDER BY imported_at
            """
        ).fetchall()
    years = [dict(row) for row in rows]
    return {
        "years": years,
        "total": sum(row["transcript_count"] for row in years),
        "imports": [
            {
                "source_count": row["source_count"],
                "imported_count": row["imported_count"],
                "imported_at": row["imported_at"],
            }
            for row in imports
        ],
    }


@app.get("/api/export.csv")
def export_csv(
    request: Request,
    q: str = Query("", max_length=200),
    date: str = "",
    start: str = "",
    end: str = "",
    bookmarked: bool = False,
    through_id: Optional[int] = None,
):
    session = require_role(request, "supervisor")
    if through_id is None:
        with connect(read_only=True) as connection:
            through_id = connection.execute(
                "SELECT coalesce(max(id), 0) FROM transcripts"
            ).fetchone()[0]
    filters = {
        "query": q.strip(),
        "date_value": date,
        "start_time": start,
        "end_time": end,
        "bookmarked": bookmarked,
        "through_id": through_id,
    }
    row_count = count_export_rows(**filters)
    audit(session["u"], "export", details={"rows": row_count, "query": q})
    filename = f"radio-transcripts-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    return StreamingResponse(
        stream_export_csv(**filters),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Radio-Export-Count": str(row_count),
            "X-Radio-Export-Through-Id": str(through_id),
        },
    )


@app.get("/api/export/count")
def export_count(
    request: Request,
    q: str = Query("", max_length=200),
    date: str = "",
    start: str = "",
    end: str = "",
    bookmarked: bool = False,
):
    require_role(request, "supervisor")
    with connect(read_only=True) as connection:
        through_id = connection.execute(
            "SELECT coalesce(max(id), 0) FROM transcripts"
        ).fetchone()[0]
    row_count = count_export_rows(
        query=q.strip(),
        date_value=date,
        start_time=start,
        end_time=end,
        bookmarked=bookmarked,
        through_id=through_id,
    )
    return {"count": row_count, "through_id": through_id}


@app.get("/api/models/status")
def model_status(request: Request):
    require_role(request, "viewer")
    return {"models": all_model_statuses(), "queue": queue_summary()}


@app.get("/api/quality-analytics")
def get_quality_analytics(request: Request):
    require_role(request, "supervisor")
    return {"analytics": quality_analytics(), "profiles": list_profiles()}


@app.put("/api/transcription-profiles/{channel}")
def update_transcription_profile(
    channel: str,
    payload: TranscriptionProfilePayload,
    request: Request,
):
    session = require_role(request, "admin")
    try:
        profile = save_profile(channel, payload.model_dump(), session["u"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(session["u"], "save_transcription_profile", details={"channel": channel})
    return profile


@app.get("/api/backups")
def backup_history(request: Request):
    require_role(request, "admin")
    return {"backups": list_backups()}


@app.post("/api/backups")
def make_backup(request: Request):
    session = require_role(request, "admin")
    try:
        backup = create_backup(session["u"])
    except Exception as exc:
        logger.exception("Backup failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    audit(session["u"], "create_backup", details={"backup_id": backup["id"]})
    return backup


@app.post("/api/backups/{backup_id}/verify")
def check_backup(backup_id: int, request: Request):
    session = require_role(request, "admin")
    try:
        backup = verify_backup(backup_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit(session["u"], "verify_backup", details={"backup_id": backup_id})
    return backup


@app.post("/api/backups/{backup_id}/restore-drill")
def check_restore(backup_id: int, request: Request):
    session = require_role(request, "admin")
    try:
        result = restore_drill(backup_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit(session["u"], "restore_drill", details={"backup_id": backup_id})
    return result


@app.post("/api/models/{model_key}/download")
def download_model(model_key: str, request: Request):
    session = require_role(request, "admin")
    try:
        result = start_download(model_key)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    audit(session["u"], "start_model_download", details={"model": model_key})
    return result


@app.patch("/api/models/{model_key}/download")
def update_model_download(
    model_key: str,
    payload: ModelControlPayload,
    request: Request,
):
    session = require_role(request, "admin")
    try:
        result = control_download(model_key, payload.action)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(
        session["u"],
        "control_model_download",
        details={"model": model_key, "action": payload.action},
    )
    return result


@app.get("/api/transcription/queue")
def transcription_queue_status(request: Request):
    require_role(request, "operator")
    return queue_summary()


@app.get("/api/glossaries")
def get_glossaries(request: Request):
    require_role(request, "operator")
    with connect(read_only=True) as connection:
        channels = [
            row["channel"]
            for row in connection.execute(
                """
                SELECT DISTINCT channel FROM transcripts
                WHERE channel IS NOT NULL AND channel != ''
                ORDER BY lower(channel)
                """
            ).fetchall()
        ]
    return {"glossaries": list_glossaries(), "channels": channels}


@app.put("/api/glossaries/{channel}")
def update_glossary(channel: str, payload: GlossaryPayload, request: Request):
    session = require_role(request, "supervisor")
    try:
        result = save_glossary(
            channel,
            payload.terms,
            payload.language,
            session["u"],
            payload.version,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit(session["u"], "save_channel_glossary", details={"channel": channel})
    return result


@app.get("/api/transcripts/{transcript_id}")
def get_transcript_detail(transcript_id: int, request: Request):
    require_role(request, "viewer")
    with connect(read_only=True) as connection:
        row = connection.execute(
            f"SELECT {TRANSCRIPT_DETAIL_COLUMNS} FROM transcripts WHERE id = ?",
            (transcript_id,),
        ).fetchone()
        history_rows = connection.execute(
            """
            SELECT version, changed_at, changed_by, change_type,
                   before_json, after_json
            FROM transcript_versions
            WHERE transcript_id = ?
            ORDER BY id DESC
            LIMIT 25
            """,
            (transcript_id,),
        ).fetchall()
        segment_rows = connection.execute(
            """
            SELECT result_kind, segment_index, start_seconds, end_seconds,
                   text, confidence
            FROM transcript_segments
            WHERE transcript_id = ?
            ORDER BY result_kind, segment_index
            """,
            (transcript_id,),
        ).fetchall()
    if row is None:
        raise HTTPException(status_code=404, detail="Transmission not found")
    result = transcript_row_to_dict(row)
    result["history"] = [
        {
            "version": history["version"],
            "changed_at": history["changed_at"],
            "changed_by": history["changed_by"],
            "change_type": history["change_type"],
            "before": json.loads(history["before_json"]),
            "after": json.loads(history["after_json"]),
        }
        for history in history_rows
    ]
    result["segments"] = {"primary": [], "retry": []}
    for segment in segment_rows:
        result["segments"].setdefault(segment["result_kind"], []).append(dict(segment))
    return result


@app.patch("/api/transcripts/{transcript_id}/selection")
async def select_transcript_result(
    transcript_id: int,
    payload: TranscriptSelectionPayload,
    request: Request,
):
    session = require_role(request, "supervisor")
    if payload.selection not in {"primary", "retry"}:
        raise HTTPException(status_code=400, detail="Choose Medium or Large V3")
    now = datetime.now().isoformat()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        before_row = connection.execute(
            f"SELECT {TRANSCRIPT_DETAIL_COLUMNS} FROM transcripts WHERE id = ?",
            (transcript_id,),
        ).fetchone()
        if before_row is None:
            raise HTTPException(status_code=404, detail="Transmission not found")
        if payload.version != before_row["version"]:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "This transmission changed in another session",
                    "current": transcript_row_to_dict(before_row),
                },
            )
        if payload.selection == "retry" and not (before_row["retry_transcript_text"] or "").strip():
            raise HTTPException(status_code=400, detail="No Large V3 result is available")

        if payload.selection == "primary":
            text = before_row["raw_transcript_text"] or before_row["transcript_text"]
            score = before_row["primary_quality_score"]
            reason = before_row["primary_quality_reason"] or ""
            metrics = before_row["primary_quality_metrics"] or "{}"
            status = before_row["primary_status"] or assess_transcript(text)["status"]
            model = before_row["primary_model"] or before_row["transcription_model"]
            selection_reason = f"Medium selected manually by {session['u']}"
        else:
            text = before_row["retry_transcript_text"]
            score = before_row["retry_quality_score"]
            reason = before_row["retry_quality_reason"] or ""
            metrics = before_row["retry_quality_metrics"] or "{}"
            status = assess_transcript(text)["status"]
            model = before_row["retry_model"]
            selection_reason = f"Large V3 selected manually by {session['u']}"
        connection.execute(
            """
            UPDATE transcripts
            SET transcript_text = ?, quality_score = ?, quality_reason = ?,
                quality_metrics = ?, status = ?, transcription_model = ?,
                selected_transcript = ?, selection_reason = ?,
                retry_status = CASE
                    WHEN ? = 'retry' THEN 'selected' ELSE 'kept_primary'
                END,
                broadcast_pending = CASE WHEN ? = 'ready' THEN 1 ELSE broadcast_pending END,
                version = version + 1
            WHERE id = ?
            """,
            (
                text,
                score,
                reason,
                metrics,
                status,
                model,
                payload.selection,
                selection_reason,
                payload.selection,
                status,
                transcript_id,
            ),
        )
        row = connection.execute(
            f"SELECT {TRANSCRIPT_DETAIL_COLUMNS} FROM transcripts WHERE id = ?",
            (transcript_id,),
        ).fetchone()
        before_payload = transcript_row_to_dict(before_row)
        after_payload = transcript_row_to_dict(row)
        connection.execute(
            """
            INSERT INTO transcript_versions(
                transcript_id, version, changed_at, changed_by,
                change_type, before_json, after_json
            ) VALUES (?, ?, ?, ?, 'model_selection', ?, ?)
            """,
            (
                transcript_id,
                row["version"],
                now,
                session["u"],
                json.dumps(before_payload, separators=(",", ":"), sort_keys=True),
                json.dumps(after_payload, separators=(",", ":"), sort_keys=True),
            ),
        )
        event, _ = record_event(
            connection,
            "transcript.updated",
            resource_type="transcript",
            resource_id=transcript_id,
            actor=session["u"],
            payload={"transcript": after_payload, "change_type": "model_selection"},
        )
        connection.commit()
    audit(
        session["u"],
        "select_transcript_model",
        transcript_id,
        {"selection": payload.selection},
    )
    await manager.broadcast(event)
    return after_payload


@app.patch("/api/transcripts/{transcript_id}")
async def update_transcript(
    transcript_id: int,
    payload: TranscriptUpdatePayload,
    request: Request,
):
    session = require_role(request, "operator")
    if payload.version is None:
        raise HTTPException(
            status_code=428,
            detail="Transcript version is required for collaborative updates",
        )
    updates = []
    parameters = []
    action_details = {}
    now = datetime.now().isoformat()

    requested_review_state = payload.review_state
    if requested_review_state is None and payload.reviewed is not None:
        requested_review_state = "confirmed" if payload.reviewed else "unreviewed"
    if requested_review_state is not None:
        if requested_review_state not in REVIEW_STATES:
            raise HTTPException(status_code=400, detail="Invalid review state")
        updates.extend(
            [
                "review_state = ?",
                "reviewed = ?",
                "reviewed_by = ?",
                "reviewed_at = ?",
            ]
        )
        parameters.extend(
            [
                requested_review_state,
                int(requested_review_state in {"confirmed", "corrected"}),
                session["u"] if requested_review_state != "unreviewed" else None,
                now if requested_review_state != "unreviewed" else None,
            ]
        )
        action_details["review_state"] = requested_review_state
    if payload.review_resolution is not None:
        updates.append("review_resolution = ?")
        parameters.append(payload.review_resolution.strip()[:1000])
        action_details["review_resolution_updated"] = True
    if payload.bookmarked is not None:
        updates.append("bookmarked = ?")
        parameters.append(int(payload.bookmarked))
        action_details["bookmarked"] = payload.bookmarked
    if payload.notes is not None:
        updates.append("notes = ?")
        parameters.append(payload.notes.strip()[:4000])
        action_details["notes_updated"] = True
    if payload.transcript_text is not None:
        if not role_allows(session, "supervisor"):
            raise HTTPException(status_code=403, detail="Supervisor clearance required")
        corrected = payload.transcript_text.strip()
        if not corrected:
            raise HTTPException(status_code=400, detail="Correction cannot be blank")
        updates.extend(
            [
                "transcript_text = ?",
                "corrected_by = ?",
                "corrected_at = ?",
                "status = 'ready'",
                "quality_reason = ''",
                "quality_score = 1.0",
                "review_state = 'corrected'",
                "reviewed = 1",
                "reviewed_by = ?",
                "reviewed_at = ?",
            ]
        )
        parameters.extend([corrected, session["u"], now, session["u"], now])
        action_details["corrected"] = True
    if not updates:
        raise HTTPException(status_code=400, detail="No changes supplied")

    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        before_row = connection.execute(
            f"SELECT {TRANSCRIPT_DETAIL_COLUMNS} FROM transcripts WHERE id = ?",
            (transcript_id,),
        ).fetchone()
        if before_row is None:
            raise HTTPException(status_code=404, detail="Transmission not found")
        if payload.version is not None and payload.version != before_row["version"]:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "This transmission changed in another session",
                    "current": transcript_row_to_dict(before_row),
                },
            )

        parameters.append(transcript_id)
        connection.execute(
            f"""
            UPDATE transcripts
            SET {', '.join(updates)}, version = version + 1
            WHERE id = ?
            """,
            parameters,
        )
        row = connection.execute(
            f"SELECT {TRANSCRIPT_DETAIL_COLUMNS} FROM transcripts WHERE id = ?",
            (transcript_id,),
        ).fetchone()
        before_payload = transcript_row_to_dict(before_row)
        after_payload = transcript_row_to_dict(row)
        change_type = (
            "correction"
            if payload.transcript_text is not None
            else "review"
            if requested_review_state is not None
            else "annotation"
        )
        connection.execute(
            """
            INSERT INTO transcript_versions(
                transcript_id, version, changed_at, changed_by,
                change_type, before_json, after_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transcript_id,
                row["version"],
                now,
                session["u"],
                change_type,
                json.dumps(before_payload, separators=(",", ":"), sort_keys=True),
                json.dumps(after_payload, separators=(",", ":"), sort_keys=True),
            ),
        )
        event, _ = record_event(
            connection,
            "transcript.updated",
            resource_type="transcript",
            resource_id=transcript_id,
            actor=session["u"],
            payload={
                "transcript": after_payload,
                "change_type": change_type,
                "previous_version": before_row["version"],
            },
        )
        connection.commit()
    audit(session["u"], "update_transcript", transcript_id, action_details)
    await manager.broadcast(event)
    return transcript_row_to_dict(row)


def workspace_row_to_dict(row):
    return {
        "id": row["id"],
        "owner_username": row["owner_username"],
        "name": row["name"],
        "configuration": json.loads(row["configuration"]),
        "is_shared": bool(row["is_shared"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def validate_workspace(payload):
    name = payload.name.strip()
    if not 1 <= len(name) <= 64:
        raise HTTPException(status_code=400, detail="Workspace name must be 1–64 characters")
    unknown_keys = set(payload.configuration) - WORKSPACE_ALLOWED_KEYS
    if unknown_keys:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported workspace settings: {', '.join(sorted(unknown_keys))}",
        )
    encoded = json.dumps(payload.configuration, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > 50_000:
        raise HTTPException(status_code=400, detail="Workspace configuration is too large")
    return name, encoded


@app.get("/api/workspaces")
def get_workspaces(request: Request):
    session = require_role(request, "viewer")
    with connect(read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT id, owner_username, name, configuration, is_shared,
                   created_at, updated_at
            FROM saved_workspaces
            WHERE owner_username = ? OR is_shared = 1
            ORDER BY is_shared DESC, lower(name), id
            """,
            (session["u"],),
        ).fetchall()
    return [workspace_row_to_dict(row) for row in rows]


@app.post("/api/workspaces")
def save_workspace(payload: WorkspacePayload, request: Request):
    session = require_role(request, "viewer")
    if payload.is_shared and not role_allows(session, "supervisor"):
        raise HTTPException(status_code=403, detail="Supervisor clearance required")
    name, configuration = validate_workspace(payload)
    now = datetime.now().isoformat()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO saved_workspaces(
                owner_username, name, configuration, is_shared,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_username, name) DO UPDATE SET
                configuration = excluded.configuration,
                is_shared = excluded.is_shared,
                updated_at = excluded.updated_at
            """,
            (session["u"], name, configuration, int(payload.is_shared), now, now),
        )
        row = connection.execute(
            """
            SELECT id, owner_username, name, configuration, is_shared,
                   created_at, updated_at
            FROM saved_workspaces
            WHERE owner_username = ? AND name = ?
            """,
            (session["u"], name),
        ).fetchone()
        connection.commit()
    audit(
        session["u"],
        "save_workspace",
        details={"workspace_id": row["id"], "shared": payload.is_shared},
    )
    return workspace_row_to_dict(row)


@app.delete("/api/workspaces/{workspace_id}")
def delete_workspace(workspace_id: int, request: Request):
    session = require_role(request, "viewer")
    with connect() as connection:
        row = connection.execute(
            "SELECT owner_username, name FROM saved_workspaces WHERE id = ?",
            (workspace_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Workspace not found")
        if row["owner_username"] != session["u"] and not role_allows(session, "admin"):
            raise HTTPException(status_code=403, detail="Only the owner can delete this workspace")
        connection.execute("DELETE FROM saved_workspaces WHERE id = ?", (workspace_id,))
        connection.commit()
    audit(session["u"], "delete_workspace", details={"workspace_id": workspace_id})
    return {"ok": True}


def validate_configuration(configuration, allowed_keys, label, max_bytes=50_000):
    unknown_keys = set(configuration) - allowed_keys
    if unknown_keys:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported {label} settings: {', '.join(sorted(unknown_keys))}",
        )
    encoded = json.dumps(configuration, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > max_bytes:
        raise HTTPException(status_code=400, detail=f"{label.title()} configuration is too large")
    return encoded


@app.get("/api/preferences")
def get_preferences(request: Request):
    session = require_role(request, "viewer")
    with connect(read_only=True) as connection:
        row = connection.execute(
            """
            SELECT configuration, updated_at
            FROM user_preferences
            WHERE username = ?
            """,
            (session["u"],),
        ).fetchone()
    return {
        "configuration": json.loads(row["configuration"]) if row else {},
        "updated_at": row["updated_at"] if row else None,
    }


@app.put("/api/preferences")
def save_preferences(payload: PreferencesPayload, request: Request):
    session = require_role(request, "viewer")
    configuration = validate_configuration(
        payload.configuration,
        PREFERENCE_ALLOWED_KEYS,
        "preference",
    )
    now = datetime.now().isoformat()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO user_preferences(username, configuration, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                configuration = excluded.configuration,
                updated_at = excluded.updated_at
            """,
            (session["u"], configuration, now),
        )
        connection.commit()
    return {"configuration": json.loads(configuration), "updated_at": now}


def saved_search_row_to_dict(row):
    return {
        "id": row["id"],
        "owner_username": row["owner_username"],
        "name": row["name"],
        "configuration": json.loads(row["configuration"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@app.get("/api/saved-searches")
def get_saved_searches(request: Request):
    session = require_role(request, "viewer")
    with connect(read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT id, owner_username, name, configuration, created_at, updated_at
            FROM saved_searches
            WHERE owner_username = ?
            ORDER BY lower(name), id
            """,
            (session["u"],),
        ).fetchall()
    return [saved_search_row_to_dict(row) for row in rows]


@app.post("/api/saved-searches")
def save_search(payload: SavedSearchPayload, request: Request):
    session = require_role(request, "viewer")
    name = payload.name.strip()
    if not 1 <= len(name) <= 64:
        raise HTTPException(status_code=400, detail="Saved search name must be 1–64 characters")
    configuration = validate_configuration(
        payload.configuration,
        SAVED_SEARCH_ALLOWED_KEYS,
        "saved search",
    )
    parsed_configuration = json.loads(configuration)
    query = str(parsed_configuration.get("query", "")).strip()
    if not 1 <= len(query) <= 300:
        raise HTTPException(status_code=400, detail="Saved search requires a query")
    if parsed_configuration.get("sort", "relevance") not in {"relevance", "recent"}:
        raise HTTPException(status_code=400, detail="Invalid saved search sort")
    if not isinstance(parsed_configuration.get("filters", {}), dict):
        raise HTTPException(status_code=400, detail="Saved search filters must be an object")
    now = datetime.now().isoformat()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO saved_searches(
                owner_username, name, configuration, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(owner_username, name) DO UPDATE SET
                configuration = excluded.configuration,
                updated_at = excluded.updated_at
            """,
            (session["u"], name, configuration, now, now),
        )
        row = connection.execute(
            """
            SELECT id, owner_username, name, configuration, created_at, updated_at
            FROM saved_searches
            WHERE owner_username = ? AND name = ?
            """,
            (session["u"], name),
        ).fetchone()
        connection.commit()
    return saved_search_row_to_dict(row)


@app.delete("/api/saved-searches/{search_id}")
def delete_saved_search(search_id: int, request: Request):
    session = require_role(request, "viewer")
    with connect() as connection:
        row = connection.execute(
            """
            SELECT owner_username
            FROM saved_searches
            WHERE id = ?
            """,
            (search_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Saved search not found")
        if row["owner_username"] != session["u"]:
            raise HTTPException(status_code=403, detail="Only the owner can delete this saved search")
        connection.execute("DELETE FROM saved_searches WHERE id = ?", (search_id,))
        connection.commit()
    return {"ok": True}


def validate_alert_rule_payload(payload):
    name = payload.name.strip()
    if not 1 <= len(name) <= 80:
        raise HTTPException(status_code=400, detail="Rule name must be 1–80 characters")
    if payload.severity not in ALERT_SEVERITIES:
        raise HTTPException(status_code=400, detail="Invalid alert severity")
    if payload.match_mode not in ALERT_MATCH_MODES:
        raise HTTPException(status_code=400, detail="Invalid alert match mode")

    def normalized_values(values, label, maximum=32):
        result = []
        for value in values:
            cleaned = str(value).strip().lower()
            if not cleaned:
                continue
            if len(cleaned) > 80:
                raise HTTPException(
                    status_code=400,
                    detail=f"{label} entries must be 80 characters or fewer",
                )
            if cleaned not in result:
                result.append(cleaned)
        if len(result) > maximum:
            raise HTTPException(
                status_code=400,
                detail=f"{label} supports at most {maximum} entries",
            )
        return result

    terms = normalized_values(payload.terms, "Terms")
    if not terms:
        raise HTTPException(status_code=400, detail="At least one alert term is required")
    exclusions = normalized_values(payload.exclusions, "Exclusions")
    channels = [str(value).strip()[:160] for value in payload.channels if str(value).strip()]
    channels = list(dict.fromkeys(channels))
    if len(channels) > 32:
        raise HTTPException(status_code=400, detail="Channel scope supports at most 32 entries")
    for value, label in (
        (payload.start_time, "start time"),
        (payload.end_time, "end time"),
    ):
        if value and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise HTTPException(status_code=400, detail=f"Invalid {label}")
    if not 0 <= payload.minimum_quality <= 1:
        raise HTTPException(
            status_code=400,
            detail="Minimum quality must be between 0 and 1",
        )
    if not 0 <= payload.cooldown_seconds <= 86_400:
        raise HTTPException(
            status_code=400,
            detail="Cooldown must be between 0 and 86400 seconds",
        )
    if not 0 <= payload.escalation_seconds <= 86_400:
        raise HTTPException(
            status_code=400,
            detail="Escalation must be between 0 and 86400 seconds",
        )
    return {
        "name": name,
        "description": payload.description.strip()[:1000],
        "severity": payload.severity,
        "match_mode": payload.match_mode,
        "terms_json": json.dumps(terms, separators=(",", ":")),
        "exclusions_json": json.dumps(exclusions, separators=(",", ":")),
        "channel_scope_json": json.dumps(channels, separators=(",", ":")),
        "start_time": payload.start_time or None,
        "end_time": payload.end_time or None,
        "minimum_quality": payload.minimum_quality,
        "cooldown_seconds": payload.cooldown_seconds,
        "sound": payload.sound.strip()[:80],
        "requires_ack": int(payload.requires_ack),
        "escalation_seconds": payload.escalation_seconds,
        "active": int(payload.active),
    }


@app.get("/api/events")
def get_events(
    request: Request,
    after_id: int = 0,
    limit: int = 500,
):
    require_role(request, "viewer")
    if after_id < 0:
        raise HTTPException(status_code=400, detail="Invalid event cursor")
    with connect(read_only=True) as connection:
        events = replay_events(connection, after_id=after_id, limit=limit)
        high_watermark = connection.execute(
            "SELECT coalesce(max(id), 0) FROM events"
        ).fetchone()[0]
    return {"items": events, "high_watermark": high_watermark}


@app.get("/api/presence")
def get_presence(request: Request):
    session = require_role(request, "viewer")
    result = manager.presence_payload()["payload"]
    if role_allows(session, "supervisor"):
        users = {}
        for details in manager.connections.values():
            users[details["username"]] = {
                "username": details["username"],
                "display_name": details["display_name"],
                "role": details["role"],
                "connected_at": details["connected_at"],
                "last_seen": details["last_seen"],
            }
        result = {**result, "users": sorted(users.values(), key=lambda user: user["display_name"])}
    return result


@app.get("/api/alert-assignees")
def get_alert_assignees(request: Request):
    require_role(request, "viewer")
    return [
        user
        for user in list_users()
        if user["active"] and user["role"] in {"operator", "supervisor", "admin"}
    ]


@app.get("/api/alert-rules")
def get_alert_rules(request: Request):
    session = require_role(request, "viewer")
    with connect(read_only=True) as connection:
        if role_allows(session, "supervisor"):
            rows = connection.execute(
                "SELECT * FROM alert_rules ORDER BY active DESC, severity, lower(name)"
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT *
                FROM alert_rules
                WHERE active = 1
                ORDER BY severity, lower(name)
                """
            ).fetchall()
    return [rule_row_to_dict(row) for row in rows]


@app.post("/api/alert-rules")
async def create_alert_rule(payload: AlertRulePayload, request: Request):
    session = require_role(request, "supervisor")
    values = validate_alert_rule_payload(payload)
    now = datetime.now().isoformat()
    with connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO alert_rules(
                slug, name, description, severity, match_mode,
                terms_json, exclusions_json, channel_scope_json,
                start_time, end_time, minimum_quality, cooldown_seconds,
                sound, requires_ack, escalation_seconds, active, is_default,
                version, created_by, updated_by, created_at, updated_at
            ) VALUES (
                NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0,
                1, ?, ?, ?, ?
            )
            """,
            (
                values["name"],
                values["description"],
                values["severity"],
                values["match_mode"],
                values["terms_json"],
                values["exclusions_json"],
                values["channel_scope_json"],
                values["start_time"],
                values["end_time"],
                values["minimum_quality"],
                values["cooldown_seconds"],
                values["sound"],
                values["requires_ack"],
                values["escalation_seconds"],
                values["active"],
                session["u"],
                session["u"],
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM alert_rules WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        rule = rule_row_to_dict(row)
        event, _ = record_event(
            connection,
            "alert_rule.created",
            resource_type="alert_rule",
            resource_id=rule["id"],
            actor=session["u"],
            payload={"rule": rule},
        )
        connection.commit()
    audit(session["u"], "create_alert_rule", details={"rule_id": rule["id"]})
    await manager.broadcast(event)
    return rule


@app.put("/api/alert-rules/{rule_id}")
async def update_alert_rule(
    rule_id: int,
    payload: AlertRulePayload,
    request: Request,
):
    session = require_role(request, "supervisor")
    if payload.version is None:
        raise HTTPException(status_code=400, detail="Rule version is required")
    values = validate_alert_rule_payload(payload)
    now = datetime.now().isoformat()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT * FROM alert_rules WHERE id = ?",
            (rule_id,),
        ).fetchone()
        if current is None:
            raise HTTPException(status_code=404, detail="Alert rule not found")
        if current["version"] != payload.version:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "This alert rule changed in another session",
                    "current": rule_row_to_dict(current),
                },
            )
        connection.execute(
            """
            UPDATE alert_rules
            SET name = ?, description = ?, severity = ?, match_mode = ?,
                terms_json = ?, exclusions_json = ?, channel_scope_json = ?,
                start_time = ?, end_time = ?, minimum_quality = ?,
                cooldown_seconds = ?, sound = ?, requires_ack = ?,
                escalation_seconds = ?, active = ?, updated_by = ?,
                updated_at = ?, version = version + 1
            WHERE id = ?
            """,
            (
                values["name"],
                values["description"],
                values["severity"],
                values["match_mode"],
                values["terms_json"],
                values["exclusions_json"],
                values["channel_scope_json"],
                values["start_time"],
                values["end_time"],
                values["minimum_quality"],
                values["cooldown_seconds"],
                values["sound"],
                values["requires_ack"],
                values["escalation_seconds"],
                values["active"],
                session["u"],
                now,
                rule_id,
            ),
        )
        row = connection.execute(
            "SELECT * FROM alert_rules WHERE id = ?",
            (rule_id,),
        ).fetchone()
        rule = rule_row_to_dict(row)
        event, _ = record_event(
            connection,
            "alert_rule.updated",
            resource_type="alert_rule",
            resource_id=rule_id,
            actor=session["u"],
            payload={"rule": rule, "previous_version": current["version"]},
        )
        connection.commit()
    audit(session["u"], "update_alert_rule", details={"rule_id": rule_id})
    await manager.broadcast(event)
    return rule


@app.post("/api/alert-rules/{rule_id}/test")
def test_alert_rule(
    rule_id: int,
    payload: AlertTestPayload,
    request: Request,
):
    require_role(request, "supervisor")
    with connect(read_only=True) as connection:
        row = connection.execute(
            "SELECT * FROM alert_rules WHERE id = ?",
            (rule_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Alert rule not found")
    rule = rule_row_to_dict(row)
    matches = rule_matches(
        rule,
        {
            "transcript_text": payload.transcript_text,
            "channel": payload.channel,
            "quality_score": payload.quality_score,
            "recorded_at": payload.recorded_at or datetime.now().isoformat(),
        },
    )
    return {
        "matched": bool(matches),
        "matches": matches,
        "explanation": (
            f"{rule['severity'].title()} because rule {rule['name']} matched "
            f"“{matches[0]}”."
            if matches
            else "This sample does not match the rule."
        ),
    }


def alert_filters(status, severity, assigned_to, include_suspect):
    clauses = ["1 = 1"]
    parameters = []
    if status == "active":
        clauses.append("ae.status IN ('open', 'acknowledged')")
    elif status and status != "all":
        if status not in ALERT_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid alert status")
        clauses.append("ae.status = ?")
        parameters.append(status)
    if severity:
        if severity not in ALERT_SEVERITIES:
            raise HTTPException(status_code=400, detail="Invalid alert severity")
        clauses.append("ae.severity = ?")
        parameters.append(severity)
    if assigned_to:
        clauses.append("ae.assigned_to = ?")
        parameters.append(assigned_to[:80])
    if not include_suspect:
        clauses.append("t.status = 'ready'")
    else:
        clauses.append("t.status != 'blank'")
    return clauses, parameters


@app.get("/api/alerts")
def get_alerts(
    request: Request,
    status: str = "active",
    severity: str = "",
    assigned_to: str = Query("", max_length=80),
    before_id: Optional[int] = None,
    limit: int = 100,
):
    session = require_role(request, "viewer")
    clauses, parameters = alert_filters(
        status,
        severity,
        assigned_to.strip(),
        role_allows(session, "supervisor"),
    )
    if before_id is not None:
        clauses.append("ae.id < ?")
        parameters.append(before_id)
    where_clause = " AND ".join(clauses)
    result_limit = max(1, min(int(limit), 200))
    with connect(read_only=True) as connection:
        count = connection.execute(
            f"""
            SELECT count(*)
            FROM alert_events ae
            JOIN alert_rules ar ON ar.id = ae.rule_id
            JOIN transcripts t ON t.id = ae.transcript_id
            WHERE {where_clause}
            """,
            parameters,
        ).fetchone()[0]
        rows = connection.execute(
            f"""
            SELECT {ALERT_DETAIL_SELECT}
            FROM alert_events ae
            JOIN alert_rules ar ON ar.id = ae.rule_id
            JOIN transcripts t ON t.id = ae.transcript_id
            WHERE {where_clause}
            ORDER BY
                CASE ae.severity
                    WHEN 'critical' THEN 4
                    WHEN 'urgent' THEN 3
                    WHEN 'caution' THEN 2
                    ELSE 1
                END DESC,
                ae.id DESC
            LIMIT ?
            """,
            [*parameters, result_limit],
        ).fetchall()
        active_status_clause = (
            "t.status != 'blank'"
            if role_allows(session, "supervisor")
            else "t.status = 'ready'"
        )
        active_count = connection.execute(
            f"""
            SELECT count(*)
            FROM alert_events ae
            JOIN transcripts t ON t.id = ae.transcript_id
            WHERE ae.status IN ('open', 'acknowledged')
              AND {active_status_clause}
            """
        ).fetchone()[0]
    return {
        "items": [alert_row_to_dict(row) for row in rows],
        "count": count,
        "active_count": active_count,
        "next_before_id": rows[-1]["id"] if len(rows) == result_limit else None,
    }


@app.get("/api/alerts/summary")
def get_alert_summary(request: Request):
    session = require_role(request, "viewer")
    status_clause = "t.status != 'blank'" if role_allows(session, "supervisor") else "t.status = 'ready'"
    with connect(read_only=True) as connection:
        rows = connection.execute(
            f"""
            SELECT ae.status, ae.severity, count(*) AS count
            FROM alert_events ae
            JOIN transcripts t ON t.id = ae.transcript_id
            WHERE {status_clause}
            GROUP BY ae.status, ae.severity
            """
        ).fetchall()
    counts = {
        status_name: {severity_name: 0 for severity_name in ALERT_SEVERITIES}
        for status_name in ALERT_STATUSES
    }
    for row in rows:
        counts[row["status"]][row["severity"]] = row["count"]
    active_count = sum(counts[name][severity] for name in ("open", "acknowledged") for severity in ALERT_SEVERITIES)
    return {"counts": counts, "active_count": active_count}


@app.patch("/api/alerts/{alert_id}")
async def update_alert(
    alert_id: int,
    payload: AlertUpdatePayload,
    request: Request,
):
    session = require_role(request, "operator")
    if payload.status is not None and payload.status not in ALERT_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid alert status")
    if payload.status == "false_positive" and not role_allows(session, "supervisor"):
        raise HTTPException(
            status_code=403,
            detail="Supervisor clearance is required to classify a false positive",
        )
    if payload.assigned_to is not None:
        assigned_to = payload.assigned_to.strip()
        valid_users = {user["username"] for user in list_users() if user["active"]}
        if assigned_to and assigned_to not in valid_users:
            raise HTTPException(status_code=400, detail="Unknown alert assignee")
    else:
        assigned_to = None
    if payload.status is None and payload.assigned_to is None and payload.resolution_note is None:
        raise HTTPException(status_code=400, detail="No alert changes supplied")
    now = datetime.now().isoformat()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            f"""
            SELECT {ALERT_DETAIL_SELECT}
            FROM alert_events ae
            JOIN alert_rules ar ON ar.id = ae.rule_id
            JOIN transcripts t ON t.id = ae.transcript_id
            WHERE ae.id = ?
            """,
            (alert_id,),
        ).fetchone()
        if current is None:
            raise HTTPException(status_code=404, detail="Alert not found")
        if current["version"] != payload.version:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "This alert changed in another session",
                    "current": alert_row_to_dict(current),
                },
            )
        updates = ["updated_at = ?", "version = version + 1"]
        parameters = [now]
        if payload.status is not None:
            updates.append("status = ?")
            parameters.append(payload.status)
            if payload.status == "acknowledged":
                updates.extend(["acknowledged_by = ?", "acknowledged_at = ?"])
                parameters.extend([session["u"], now])
            elif payload.status in {"resolved", "false_positive"}:
                updates.extend(["resolved_by = ?", "resolved_at = ?"])
                parameters.extend([session["u"], now])
            elif payload.status == "open":
                updates.extend(
                    [
                        "acknowledged_by = NULL",
                        "acknowledged_at = NULL",
                        "resolved_by = NULL",
                        "resolved_at = NULL",
                    ]
                )
        if payload.assigned_to is not None:
            updates.append("assigned_to = ?")
            parameters.append(assigned_to or None)
        if payload.resolution_note is not None:
            updates.append("resolution_note = ?")
            parameters.append(payload.resolution_note.strip()[:1000])
        parameters.append(alert_id)
        connection.execute(
            f"UPDATE alert_events SET {', '.join(updates)} WHERE id = ?",
            parameters,
        )
        row = connection.execute(
            f"""
            SELECT {ALERT_DETAIL_SELECT}
            FROM alert_events ae
            JOIN alert_rules ar ON ar.id = ae.rule_id
            JOIN transcripts t ON t.id = ae.transcript_id
            WHERE ae.id = ?
            """,
            (alert_id,),
        ).fetchone()
        alert = alert_row_to_dict(row)
        action = payload.status or ("assigned" if payload.assigned_to is not None else "annotated")
        connection.execute(
            """
            INSERT INTO alert_acknowledgements(
                alert_id, action, actor_username, note, alert_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                alert_id,
                action,
                session["u"],
                payload.resolution_note.strip()[:1000]
                if payload.resolution_note
                else "",
                alert["version"],
                now,
            ),
        )
        event, _ = record_event(
            connection,
            "alert.updated",
            resource_type="alert",
            resource_id=alert_id,
            actor=session["u"],
            payload={
                "alert": alert,
                "previous_version": current["version"],
                "action": action,
            },
        )
        connection.commit()
    audit(session["u"], "update_alert", alert["transcript_id"], {"alert_id": alert_id, "action": action})
    await manager.broadcast(event)
    return alert


@app.get("/api/notification-preferences")
def get_notification_preferences(request: Request):
    session = require_role(request, "viewer")
    with connect(read_only=True) as connection:
        row = connection.execute(
            """
            SELECT configuration, updated_at
            FROM user_notification_preferences
            WHERE username = ?
            """,
            (session["u"],),
        ).fetchone()
    return {
        "configuration": json.loads(row["configuration"]) if row else {},
        "updated_at": row["updated_at"] if row else None,
    }


@app.put("/api/notification-preferences")
def save_notification_preferences(
    payload: NotificationPreferencesPayload,
    request: Request,
):
    session = require_role(request, "viewer")
    unknown = set(payload.configuration) - NOTIFICATION_PREFERENCE_ALLOWED_KEYS
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported notification settings: {', '.join(sorted(unknown))}",
        )
    encoded = json.dumps(payload.configuration, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > 10_000:
        raise HTTPException(status_code=400, detail="Notification preferences are too large")
    now = datetime.now().isoformat()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO user_notification_preferences(
                username, configuration, updated_at
            ) VALUES (?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                configuration = excluded.configuration,
                updated_at = excluded.updated_at
            """,
            (session["u"], encoded, now),
        )
        connection.commit()
    return {"configuration": payload.configuration, "updated_at": now}


def validate_incident_payload(payload):
    title = payload.title.strip()
    if not 1 <= len(title) <= 160:
        raise HTTPException(status_code=400, detail="Incident title must be 1–160 characters")
    if payload.severity not in INCIDENT_SEVERITIES:
        raise HTTPException(status_code=400, detail="Invalid incident severity")
    if payload.status not in INCIDENT_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid incident status")
    owner = (payload.owner_username or "").strip() or None
    if owner:
        valid_owners = {
            user["username"]
            for user in list_users()
            if user["active"] and user["role"] in {"operator", "supervisor", "admin"}
        }
        if owner not in valid_owners:
            raise HTTPException(status_code=400, detail="Incident owner is unavailable")
    tags = []
    for value in payload.tags:
        tag = str(value).strip()[:40]
        if tag and tag.casefold() not in {item.casefold() for item in tags}:
            tags.append(tag)
    if len(tags) > 20:
        raise HTTPException(status_code=400, detail="Incidents support at most 20 tags")
    started_at = payload.started_at or datetime.now().isoformat()
    try:
        datetime.fromisoformat(started_at)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid incident start time") from exc
    return {
        "title": title,
        "severity": payload.severity,
        "status": payload.status,
        "owner_username": owner,
        "location": payload.location.strip()[:200],
        "summary": payload.summary.strip()[:4000],
        "tags_json": json.dumps(tags, separators=(",", ":")),
        "started_at": started_at,
    }


def validate_incident_assignee(username):
    assignee = (username or "").strip() or None
    if assignee is None:
        return None
    valid_assignees = {
        user["username"]
        for user in list_users()
        if user["active"] and user["role"] in {"operator", "supervisor", "admin"}
    }
    if assignee not in valid_assignees:
        raise HTTPException(status_code=400, detail="Incident assignee is unavailable")
    return assignee


def validate_optional_datetime(value, label):
    normalized = (value or "").strip() or None
    if normalized:
        try:
            datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid {label}") from exc
    return normalized


@app.get("/api/incidents")
def get_incidents(
    request: Request,
    status: str = "open",
    owner: str = "",
    q: str = Query("", max_length=160),
):
    require_role(request, "viewer")
    clauses = []
    parameters = []
    if status == "open":
        clauses.append("i.status != 'resolved'")
    elif status != "all":
        if status not in INCIDENT_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid incident status filter")
        clauses.append("i.status = ?")
        parameters.append(status)
    if owner:
        clauses.append("i.owner_username = ?")
        parameters.append(owner)
    query = q.strip()
    if query:
        clauses.append("(i.title LIKE ? OR i.location LIKE ? OR i.summary LIKE ?)")
        pattern = f"%{query}%"
        parameters.extend((pattern, pattern, pattern))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect(read_only=True) as connection:
        rows = connection.execute(
            f"""
            {INCIDENT_SUMMARY_SELECT}
            {where}
            ORDER BY CASE i.severity
                WHEN 'critical' THEN 4 WHEN 'urgent' THEN 3
                WHEN 'caution' THEN 2 ELSE 1 END DESC,
                i.updated_at DESC, i.id DESC
            LIMIT 500
            """,
            parameters,
        ).fetchall()
    return {"items": [incident_row_to_dict(row) for row in rows], "count": len(rows)}


@app.get("/api/incidents/{incident_id}")
def get_incident_detail(incident_id: int, request: Request):
    require_role(request, "viewer")
    with connect(read_only=True) as connection:
        detail = incident_detail(connection, incident_id, transcript_row_to_dict)
    if detail is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    return detail


@app.post("/api/incidents")
async def create_incident(payload: IncidentPayload, request: Request):
    session = require_role(request, "operator")
    values = validate_incident_payload(payload)
    now = datetime.now().isoformat()
    with connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO incidents(
                title, severity, status, owner_username, location, summary,
                tags_json, started_at, resolved_at, version,
                created_by, updated_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (
                values["title"], values["severity"], values["status"],
                values["owner_username"], values["location"], values["summary"],
                values["tags_json"], values["started_at"],
                now if values["status"] == "resolved" else None,
                session["u"], session["u"], now, now,
            ),
        )
        incident = get_incident(connection, cursor.lastrowid)
        event, _ = record_event(
            connection,
            "incident.created",
            resource_type="incident",
            resource_id=incident["id"],
            actor=session["u"],
            payload={"incident": incident},
        )
        connection.commit()
    audit(session["u"], "create_incident", details={"incident_id": incident["id"]})
    await manager.broadcast(event)
    return incident


@app.patch("/api/incidents/{incident_id}")
async def update_incident(
    incident_id: int,
    payload: IncidentPayload,
    request: Request,
):
    session = require_role(request, "operator")
    if payload.version is None:
        raise HTTPException(status_code=428, detail="Incident version is required")
    values = validate_incident_payload(payload)
    now = datetime.now().isoformat()
    with connect() as connection:
        current = get_incident(connection, incident_id)
        if current is None:
            raise HTTPException(status_code=404, detail="Incident not found")
        if current["version"] != payload.version:
            return JSONResponse(
                status_code=409,
                content={"detail": "Incident changed by another operator", "current": current},
            )
        resolved_at = (
            current["resolved_at"] or now
            if values["status"] == "resolved"
            else None
        )
        connection.execute(
            """
            UPDATE incidents
            SET title = ?, severity = ?, status = ?, owner_username = ?,
                location = ?, summary = ?, tags_json = ?, started_at = ?,
                resolved_at = ?, version = version + 1,
                updated_by = ?, updated_at = ?
            WHERE id = ? AND version = ?
            """,
            (
                values["title"], values["severity"], values["status"],
                values["owner_username"], values["location"], values["summary"],
                values["tags_json"], values["started_at"], resolved_at,
                session["u"], now, incident_id, payload.version,
            ),
        )
        incident = get_incident(connection, incident_id)
        event, _ = record_event(
            connection,
            "incident.updated",
            resource_type="incident",
            resource_id=incident_id,
            actor=session["u"],
            payload={"incident": incident},
        )
        connection.commit()
    audit(session["u"], "update_incident", details={"incident_id": incident_id})
    await manager.broadcast(event)
    return incident


@app.post("/api/incidents/{incident_id}/transcripts")
async def add_incident_transcript(
    incident_id: int,
    payload: IncidentTranscriptPayload,
    request: Request,
):
    session = require_role(request, "operator")
    now = datetime.now().isoformat()
    with connect() as connection:
        if get_incident(connection, incident_id) is None:
            raise HTTPException(status_code=404, detail="Incident not found")
        transcript = connection.execute(
            "SELECT id FROM transcripts WHERE id = ?", (payload.transcript_id,)
        ).fetchone()
        if transcript is None:
            raise HTTPException(status_code=404, detail="Transmission not found")
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO incident_transcripts(
                incident_id, transcript_id, added_by, added_at
            ) VALUES (?, ?, ?, ?)
            """,
            (incident_id, payload.transcript_id, session["u"], now),
        )
        if cursor.rowcount != 1:
            raise HTTPException(status_code=409, detail="Transmission is already linked")
        incident = get_incident(connection, incident_id)
        event, _ = record_event(
            connection,
            "incident.transcript_added",
            resource_type="incident",
            resource_id=incident_id,
            actor=session["u"],
            payload={"incident": incident, "transcript_id": payload.transcript_id},
        )
        connection.commit()
    audit(session["u"], "link_incident_transcript", payload.transcript_id, {"incident_id": incident_id})
    await manager.broadcast(event)
    return incident


@app.delete("/api/incidents/{incident_id}/transcripts/{transcript_id}")
async def remove_incident_transcript(
    incident_id: int,
    transcript_id: int,
    request: Request,
):
    session = require_role(request, "operator")
    with connect() as connection:
        cursor = connection.execute(
            "DELETE FROM incident_transcripts WHERE incident_id = ? AND transcript_id = ?",
            (incident_id, transcript_id),
        )
        if cursor.rowcount != 1:
            raise HTTPException(status_code=404, detail="Incident transmission link not found")
        incident = get_incident(connection, incident_id)
        event, _ = record_event(
            connection,
            "incident.transcript_removed",
            resource_type="incident",
            resource_id=incident_id,
            actor=session["u"],
            payload={"incident": incident, "transcript_id": transcript_id},
        )
        connection.commit()
    await manager.broadcast(event)
    return incident


@app.post("/api/incidents/{incident_id}/tasks")
async def create_incident_task(
    incident_id: int,
    payload: IncidentTaskPayload,
    request: Request,
):
    session = require_role(request, "operator")
    description = payload.description.strip()
    if not 1 <= len(description) <= 500:
        raise HTTPException(status_code=400, detail="Task description must be 1–500 characters")
    assignee = validate_incident_assignee(payload.assigned_to)
    due_at = validate_optional_datetime(payload.due_at, "task due time")
    now = datetime.now().isoformat()
    with connect() as connection:
        if get_incident(connection, incident_id) is None:
            raise HTTPException(status_code=404, detail="Incident not found")
        cursor = connection.execute(
            """
            INSERT INTO incident_tasks(
                incident_id, description, status, assigned_to, due_at,
                version, created_by, updated_by, created_at, updated_at
            ) VALUES (?, ?, 'open', ?, ?, 1, ?, ?, ?, ?)
            """,
            (
                incident_id, description, assignee,
                due_at, session["u"], session["u"], now, now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM incident_tasks WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        task = task_row_to_dict(row)
        event, _ = record_event(
            connection,
            "incident.task_created",
            resource_type="incident",
            resource_id=incident_id,
            actor=session["u"],
            payload={"task": task},
        )
        connection.commit()
    await manager.broadcast(event)
    return task


@app.patch("/api/incident-tasks/{task_id}")
async def update_incident_task(
    task_id: int,
    payload: IncidentTaskUpdatePayload,
    request: Request,
):
    session = require_role(request, "operator")
    now = datetime.now().isoformat()
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM incident_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Incident task not found")
        current = task_row_to_dict(row)
        if current["version"] != payload.version:
            return JSONResponse(
                status_code=409,
                content={"detail": "Task changed by another operator", "current": current},
            )
        status = payload.status or current["status"]
        if status not in INCIDENT_TASK_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid incident task status")
        description = (
            payload.description.strip()
            if payload.description is not None
            else current["description"]
        )
        if not 1 <= len(description) <= 500:
            raise HTTPException(status_code=400, detail="Task description must be 1–500 characters")
        fields_set = getattr(payload, "model_fields_set", None)
        if fields_set is None:
            fields_set = getattr(payload, "__fields_set__", set())
        assigned_to = (
            validate_incident_assignee(payload.assigned_to)
            if "assigned_to" in fields_set
            else current["assigned_to"]
        )
        due_at = (
            validate_optional_datetime(payload.due_at, "task due time")
            if "due_at" in fields_set
            else current["due_at"]
        )
        connection.execute(
            """
            UPDATE incident_tasks
            SET description = ?, status = ?, assigned_to = ?, due_at = ?,
                version = version + 1, updated_by = ?, updated_at = ?
            WHERE id = ? AND version = ?
            """,
            (
                description, status, assigned_to or None, due_at or None,
                session["u"], now, task_id, payload.version,
            ),
        )
        updated_row = connection.execute(
            "SELECT * FROM incident_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        task = task_row_to_dict(updated_row)
        event, _ = record_event(
            connection,
            "incident.task_updated",
            resource_type="incident",
            resource_id=task["incident_id"],
            actor=session["u"],
            payload={"task": task},
        )
        connection.commit()
    await manager.broadcast(event)
    return task


@app.post("/api/incidents/{incident_id}/notes")
async def add_incident_note(
    incident_id: int,
    payload: IncidentNotePayload,
    request: Request,
):
    session = require_role(request, "operator")
    note = payload.note.strip()
    if not 1 <= len(note) <= 4000:
        raise HTTPException(status_code=400, detail="Incident note must be 1–4000 characters")
    now = datetime.now().isoformat()
    with connect() as connection:
        if get_incident(connection, incident_id) is None:
            raise HTTPException(status_code=404, detail="Incident not found")
        cursor = connection.execute(
            """
            INSERT INTO incident_notes(incident_id, note, created_by, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (incident_id, note, session["u"], now),
        )
        note_payload = {
            "id": cursor.lastrowid,
            "incident_id": incident_id,
            "note": note,
            "created_by": session["u"],
            "created_at": now,
        }
        event, _ = record_event(
            connection,
            "incident.note_added",
            resource_type="incident",
            resource_id=incident_id,
            actor=session["u"],
            payload={"note": note_payload},
        )
        connection.commit()
    await manager.broadcast(event)
    return note_payload


@app.get("/api/users")
def get_users(request: Request):
    require_role(request, "admin")
    return list_users()


@app.post("/api/users")
async def save_user(payload: UserPayload, request: Request):
    session = require_role(request, "admin")
    try:
        user = upsert_user(
            payload.username,
            payload.display_name,
            payload.role,
            payload.password,
            payload.active,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with connect() as connection:
        event, _ = record_event(
            connection,
            "profile.updated",
            resource_type="profile",
            actor=session["u"],
            payload={"username": user["username"]},
        )
        connection.commit()
    audit(session["u"], "save_profile", details={"username": payload.username, "role": payload.role})
    await manager.broadcast(event)
    return user


@app.post("/api/new_transcript")
async def new_transcript(payload: TranscriptPayload):
    if not has_meaningful_transcript(payload.transcript_text) or payload.status != "ready":
        return {"status": "ignored", "reason": payload.status}
    with connect() as connection:
        if payload.id is not None:
            row = connection.execute(
                f"SELECT {TRANSCRIPT_DETAIL_COLUMNS} FROM transcripts WHERE id = ?",
                (payload.id,),
            ).fetchone()
        else:
            row = connection.execute(
                f"SELECT {TRANSCRIPT_DETAIL_COLUMNS} FROM transcripts WHERE filename = ?",
                (payload.filename,),
            ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=409,
                detail="Transcript delivery arrived before its database record",
            )
        transcript = transcript_row_to_dict(row)
        event, created = record_event(
            connection,
            "transcript.created",
            resource_type="transcript",
            resource_id=transcript["id"],
            actor="worker",
            payload={"transcript": transcript},
            dedupe_key=f"transcript.created:{transcript['id']}",
        )
        alerts, alert_events = evaluate_transcript_alerts(
            connection,
            transcript,
            actor="worker",
        )
        connection.commit()
    if created:
        await manager.broadcast(event)
    for alert_event in alert_events:
        await manager.broadcast(alert_event)
    return {
        "status": "success",
        "event_id": event["event_id"],
        "alerts_created": len(alerts),
        "duplicate": not created,
    }


@app.post("/api/internal/heartbeat")
async def internal_heartbeat():
    return {"status": "success"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    origin = websocket.headers.get("origin")
    if origin:
        parsed_origin = urlsplit(origin)
        request_host = websocket.headers.get("host", "")
        if parsed_origin.scheme != "https" or parsed_origin.netloc != request_host:
            await websocket.close(code=4403)
            return
    session = validate_session_token(websocket.cookies.get(SESSION_COOKIE))
    if session is None:
        session = app_session(
            presented_app_key(websocket.headers, websocket.cookies)
        )
    if session is None:
        await websocket.close(code=4401)
        return
    try:
        after_event_id = int(websocket.query_params.get("after_event_id", "0"))
        if after_event_id < 0:
            raise ValueError
    except (TypeError, ValueError):
        await websocket.close(code=4400)
        return
    await manager.connect(websocket, session)
    try:
        with connect(read_only=True) as connection:
            replay_high_watermark = connection.execute(
                "SELECT coalesce(max(id), 0) FROM events"
            ).fetchone()[0]
            replay_cursor = after_event_id
            while replay_cursor < replay_high_watermark:
                missed_events = replay_events(
                    connection,
                    after_id=replay_cursor,
                    limit=500,
                )
                if not missed_events:
                    break
                for event in missed_events:
                    await websocket.send_text(
                        json.dumps({**event, "replayed": True})
                    )
                replay_cursor = missed_events[-1]["event_id"]
        await manager.broadcast_presence()
        while True:
            message = await websocket.receive_text()
            manager.touch(websocket)
            if message == "ping":
                await websocket.send_text(
                    json.dumps(
                        {
                            "event_id": None,
                            "type": "connection.pong",
                            "resource_type": "connection",
                            "resource_id": None,
                            "actor": None,
                            "payload": {},
                            "created_at": datetime.now().isoformat(),
                        }
                    )
                )
    except WebSocketDisconnect:
        manager.disconnect(websocket)
        await manager.broadcast_presence()
    except Exception as exc:
        logger.warning("WebSocket exception: %s", exc)
        manager.disconnect(websocket)
        await manager.broadcast_presence()
