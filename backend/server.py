import csv
import io
import json
import logging
import os
import re
import threading
import time
import zlib
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import List, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
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
    bookmarked: Optional[bool] = None
    notes: Optional[str] = None
    transcript_text: Optional[str] = None


class UserPayload(BaseModel):
    username: str
    display_name: str = ""
    role: str = "viewer"
    password: Optional[str] = None
    active: bool = True


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
    return {
        "id": row["id"],
        "timestamp": row["timestamp"],
        "recorded_at": row["recorded_at"],
        "filename": row["filename"],
        "transcript_text": row["transcript_text"] or "",
        "quality_score": row["quality_score"],
        "quality_reason": row["quality_reason"] or "",
        "status": row["status"],
        "reviewed": bool(row["reviewed"]),
        "bookmarked": bool(row["bookmarked"]),
        "notes": row["notes"] or "",
        "corrected_by": row["corrected_by"],
        "corrected_at": row["corrected_at"],
        "transcription_model": row["transcription_model"],
        "retry_status": row["retry_status"],
    }


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

    order_by = (
        "id ASC"
        if after_id is not None
        else "coalesce(recorded_at, timestamp) DESC, id DESC"
    )
    parameters.append(max(1, min(limit, 2000)))
    sql = f"""
        SELECT id, timestamp, recorded_at, filename, transcript_text,
               quality_score, quality_reason, status, reviewed, bookmarked,
               notes, corrected_by, corrected_at, transcription_model,
               retry_status
        FROM transcripts
        WHERE {' AND '.join(clauses)}
        ORDER BY {order_by}
        LIMIT ?
    """
    with connect(read_only=True) as connection:
        rows = connection.execute(sql, parameters).fetchall()
    if after_id is None:
        rows = list(reversed(rows))
    return [transcript_row_to_dict(row) for row in rows]


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
    return query_transcripts(
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


@app.get("/api/export.csv")
def export_csv(
    request: Request,
    q: str = Query("", max_length=200),
    date: str = "",
    start: str = "",
    end: str = "",
    bookmarked: bool = False,
):
    session = require_role(request, "supervisor")
    rows = query_transcripts(
        query=q.strip(),
        limit=2000,
        date_value=date,
        start_time=start,
        end_time=end,
        include_suspect=True,
        bookmarked=bookmarked,
    )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["Recorded", "Channel/File", "Transcript", "Status", "Reviewed", "Bookmarked", "Notes"]
    )
    for row in rows:
        writer.writerow(
            [
                row["recorded_at"] or row["timestamp"],
                row["filename"],
                row["transcript_text"],
                row["status"],
                row["reviewed"],
                row["bookmarked"],
                row["notes"],
            ]
        )
    audit(session["u"], "export", details={"rows": len(rows), "query": q})
    filename = f"radio-transcripts-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
    if payload.reviewed is not None:
        updates.append("reviewed = ?")
        parameters.append(int(payload.reviewed))
        action_details["reviewed"] = payload.reviewed
    if payload.bookmarked is not None:
        updates.append("bookmarked = ?")
        parameters.append(int(payload.bookmarked))
        action_details["bookmarked"] = payload.bookmarked
    if payload.notes is not None:
        updates.append("notes = ?")
        parameters.append(payload.notes[:4000])
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
            ]
        )
        parameters.extend([corrected, session["u"], datetime.now().isoformat()])
        action_details["corrected"] = True
    if not updates:
        raise HTTPException(status_code=400, detail="No changes supplied")
    parameters.append(transcript_id)
    with connect() as connection:
        connection.execute(
            f"UPDATE transcripts SET {', '.join(updates)} WHERE id = ?",
            parameters,
        )
        row = connection.execute(
            """
            SELECT id, timestamp, recorded_at, filename, transcript_text,
                   quality_score, quality_reason, status, reviewed, bookmarked,
                   notes, corrected_by, corrected_at, transcription_model,
                   retry_status
            FROM transcripts WHERE id = ?
            """,
            (transcript_id,),
        ).fetchone()
        connection.commit()
    if row is None:
        raise HTTPException(status_code=404, detail="Transmission not found")
    audit(session["u"], "update_transcript", transcript_id, action_details)
    return transcript_row_to_dict(row)


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
