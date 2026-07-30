import csv
import io
import json
import logging
import os
import re
import sqlite3
import threading
import time
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
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.config import (
    AUDIO_DIR,
    DB_NAME,
    LOG_DIR,
    PROJECT_ROOT,
    RADIO_HOST,
    RECORDING_SOURCE_DIR,
    load_settings,
)
from backend.database import audit, connect, initialize_database
from backend.security import (
    SESSION_COOKIE,
    SESSION_SECONDS,
    authenticate_user,
    create_initial_admin,
    create_session_token,
    list_users,
    role_allows,
    setup_required,
    upsert_user,
    validate_session_token,
    verify_internal_token,
)
from backend.search import search_transcripts
from backend.transcript_quality import has_meaningful_transcript


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

RADIO_MODEL_SIZE = os.environ.get("RADIO_MODEL_SIZE", "medium")
RADIO_ENGINE = os.environ.get("RADIO_TRANSCRIPTION_ENGINE", "mlx")
ALLOWED_HOSTS = list(
    dict.fromkeys([RADIO_HOST, "127.0.0.1", "localhost", os.environ.get("RADIO_HOSTNAME", "")])
)
ALLOWED_HOSTS = [host for host in ALLOWED_HOSTS if host]
LOGIN_WINDOW_SECONDS = 15 * 60
MAX_LOGIN_FAILURES = 5
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


class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, payload):
        message = json.dumps(payload)
        dead_connections = []
        for connection in list(self.active_connections):
            try:
                await connection.send_text(message)
            except Exception as exc:
                logger.warning("Removing stale dashboard connection: %s", exc)
                dead_connections.append(connection)
        for connection in dead_connections:
            self.disconnect(connection)


manager = ConnectionManager()
login_failures = defaultdict(deque)
login_lock = threading.Lock()
stats_lock = threading.Lock()
stats_cache = {"at": 0.0, "payload": None}
audio_count_lock = threading.Lock()
audio_count_cache = {"at": 0.0, "value": 0}
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


class TranscriptUpdatePayload(BaseModel):
    reviewed: Optional[bool] = None
    review_state: Optional[str] = None
    review_resolution: Optional[str] = None
    version: Optional[int] = None
    bookmarked: Optional[bool] = None
    notes: Optional[str] = None
    transcript_text: Optional[str] = None


class WorkspacePayload(BaseModel):
    name: str
    configuration: dict
    is_shared: bool = False


class PreferencesPayload(BaseModel):
    configuration: dict


class SavedSearchPayload(BaseModel):
    name: str
    configuration: dict


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
    retry_status, retry_attempted_at
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


def get_session(request):
    return validate_session_token(request.cookies.get(SESSION_COOKIE))


def require_role(request, minimum_role):
    session = get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not role_allows(session, minimum_role):
        raise HTTPException(status_code=403, detail="Your profile does not have clearance")
    return session


def wants_html(request):
    return "text/html" in request.headers.get("accept", "")


@app.middleware("http")
async def authentication_and_headers(request: Request, call_next):
    path = request.url.path
    is_public = path == "/login" or path == "/api/login" or path.startswith("/static/")
    is_setup_public = path == "/setup" or path == "/api/setup"
    is_internal = path in {"/api/new_transcript", "/api/internal/heartbeat"}

    if is_internal:
        if not verify_internal_token(request.headers.get("X-Radio-Internal-Token")):
            return secure_headers(JSONResponse({"detail": "Forbidden"}, status_code=403))
    elif setup_required():
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
    return secure_headers(response)


def client_key(request):
    return request.client.host if request.client else "unknown"


def login_is_limited(key):
    cutoff = time.time() - LOGIN_WINDOW_SECONDS
    with login_lock:
        attempts = login_failures[key]
        while attempts and attempts[0] < cutoff:
            attempts.popleft()
        return len(attempts) >= MAX_LOGIN_FAILURES


def record_login_failure(key):
    with login_lock:
        login_failures[key].append(time.time())


def clear_login_failures(key):
    with login_lock:
        login_failures.pop(key, None)


def read_html(filename):
    path = os.path.join(PROJECT_ROOT, "frontend", filename)
    if not os.path.exists(path):
        return HTMLResponse("<h1>Interface file not found.</h1>", status_code=404)
    with open(path, "r", encoding="utf-8") as handle:
        return HTMLResponse(handle.read())


def count_audio_files():
    with audio_count_lock:
        if time.time() - audio_count_cache["at"] < 30:
            return audio_count_cache["value"]

        total = 0
        pending_directories = [AUDIO_DIR]
        while pending_directories:
            directory = pending_directories.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                pending_directories.append(entry.path)
                            elif entry.is_file(follow_symlinks=False) and entry.name.lower().endswith(
                                (".mp3", ".wav", ".m4a")
                            ):
                                total += 1
                        except OSError:
                            continue
            except OSError:
                continue

        audio_count_cache.update({"at": time.time(), "value": total})
        return total


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

    total_audio = count_audio_files()
    processed = row["processed"] or 0
    backlog = max(0, total_audio - processed)
    rate_per_minute = recent_count / 15
    eta_minutes = backlog / rate_per_minute if rate_per_minute > 0 else None
    heartbeats = read_heartbeats()
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
        "active_clients": len(manager.active_connections),
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


app.mount("/static", StaticFiles(directory=os.path.join(PROJECT_ROOT, "frontend")), name="static")
app.mount("/audio", StaticFiles(directory=AUDIO_DIR), name="audio")


@app.get("/login")
async def login_page(request: Request):
    if setup_required():
        return RedirectResponse("/setup", status_code=303)
    if get_session(request):
        return RedirectResponse("/", status_code=303)
    return read_html("login.html")


@app.get("/setup")
async def setup_page():
    if not setup_required():
        return RedirectResponse("/login", status_code=303)
    return read_html("setup.html")


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
    response = JSONResponse({"status": "success"})
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(user),
        max_age=SESSION_SECONDS,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return response


@app.post("/api/login")
async def login(payload: LoginPayload, request: Request):
    key = client_key(request)
    if login_is_limited(key):
        return JSONResponse(
            {"detail": "Too many failed attempts. Try again in 15 minutes."},
            status_code=429,
        )
    user = authenticate_user(payload.username, payload.password)
    if not user:
        record_login_failure(key)
        return JSONResponse({"detail": "Invalid username or password."}, status_code=401)
    clear_login_failures(key)
    response = JSONResponse({"status": "success"})
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(user),
        max_age=SESSION_SECONDS,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return response


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
        "permissions": {
            "audio": role_allows(session, "operator"),
            "review": role_allows(session, "operator"),
            "correct": role_allows(session, "supervisor"),
            "export": role_allows(session, "supervisor"),
            "profiles": role_allows(session, "admin"),
            "suspect": role_allows(session, "supervisor"),
            "console": role_allows(session, "admin"),
        },
    }


@app.get("/api/health")
def health_check():
    return {
        "status": build_stats()["status"],
        "database_exists": os.path.exists(DB_NAME),
        "source_mounted": os.path.isdir(RECORDING_SOURCE_DIR),
    }


@app.get("/api/stats")
def get_stats():
    with stats_lock:
        if stats_cache["payload"] is not None and time.time() - stats_cache["at"] < 10:
            return stats_cache["payload"]
        payload = build_stats()
        stats_cache.update({"at": time.time(), "payload": payload})
        return payload


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
    rows = query_transcripts(
        query=q.strip(),
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
        headers={"X-Radio-High-Watermark": str(high_watermark)},
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
    return result


@app.patch("/api/transcripts/{transcript_id}")
def update_transcript(
    transcript_id: int,
    payload: TranscriptUpdatePayload,
    request: Request,
):
    session = require_role(request, "operator")
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
        connection.commit()
    audit(session["u"], "update_transcript", transcript_id, action_details)
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


@app.get("/api/users")
def get_users(request: Request):
    require_role(request, "admin")
    return list_users()


@app.post("/api/users")
def save_user(payload: UserPayload, request: Request):
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
    audit(session["u"], "save_profile", details={"username": payload.username, "role": payload.role})
    return user


@app.post("/api/new_transcript")
async def new_transcript(payload: TranscriptPayload):
    if not has_meaningful_transcript(payload.transcript_text) or payload.status != "ready":
        return {"status": "ignored", "reason": payload.status}
    data = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    await manager.broadcast(data)
    return {"status": "success"}


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
    if validate_session_token(websocket.cookies.get(SESSION_COOKIE)) is None:
        await websocket.close(code=4401)
        return
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as exc:
        logger.warning("WebSocket exception: %s", exc)
        manager.disconnect(websocket)
