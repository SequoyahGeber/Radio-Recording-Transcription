import os
import sqlite3
import json
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List

# --- SETUP LOGGING ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="LiveOps Command Server")

# --- CORS MIDDLEWARE ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- CONFIGURATION ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
AUDIO_DIR = os.path.join(BASE_DIR, "live_audio")
DB_NAME = os.path.join(BASE_DIR, "festival_radio.db")

os.makedirs(AUDIO_DIR, exist_ok=True)
app.mount("/audio", StaticFiles(directory=AUDIO_DIR), name="audio")

# --- WEBSOCKET MANAGER ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"Client connected. Active Operators: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            logger.info(f"Client disconnected. Active Operators: {len(self.active_connections)}")

    async def broadcast(self, message: str):
        dead_connections = []
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except RuntimeError:
                dead_connections.append(connection)
        
        for dead in dead_connections:
            self.disconnect(dead)

manager = ConnectionManager()

# --- DATA MODELS ---
class TranscriptPayload(BaseModel):
    filename: str
    transcript_text: str
    timestamp: str

# --- ROUTES ---
@app.get("/")
async def get():
    html_path = os.path.join(BASE_DIR, "index.html")
    if not os.path.exists(html_path):
        return HTMLResponse("<h1>Error: index.html not found!</h1>", status_code=404)
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.get("/api/health")
def health_check():
    return {
        "status": "online", 
        "active_clients": len(manager.active_connections),
        "database_exists": os.path.exists(DB_NAME)
    }

@app.get("/api/history")
def get_history():
    if not os.path.exists(DB_NAME):
        return []
        
    try:
        db_uri = f"file:{DB_NAME}?mode=ro"
        with sqlite3.connect(db_uri, uri=True, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT timestamp, filename, transcript_text FROM transcripts ORDER BY id DESC LIMIT 200")
            rows = cursor.fetchall()
            
        return [{"timestamp": r[0], "filename": r[1], "transcript_text": r[2]} for r in reversed(rows)]
    except sqlite3.OperationalError as e:
        logger.error(f"Database read error: {e}")
        return []
    except Exception as e:
        logger.error(f"Unexpected error fetching history: {e}")
        return []

@app.post("/api/new_transcript")
async def new_transcript(payload: TranscriptPayload):
    data = json.dumps({
        "filename": payload.filename,
        "transcript_text": payload.transcript_text,
        "timestamp": payload.timestamp
    })
    await manager.broadcast(data)
    logger.info(f"Broadcasted new transmission: {payload.filename}")
    return {"status": "success"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket exception: {e}")
        manager.disconnect(websocket)