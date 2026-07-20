import requests
import time
import os
import sqlite3
from threading import Thread, Timer, Lock
from datetime import datetime
from queue import Queue
from concurrent.futures import ThreadPoolExecutor
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from faster_whisper import WhisperModel

# --- CONFIGURATION ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
FOLDER_TO_WATCH = os.path.join(BASE_DIR, "live_audio")
DB_NAME = os.path.join(BASE_DIR, "festival_radio.db")
MODEL_SIZE = "small"
SERVER_URL = "http://127.0.0.1:8000/api/new_transcript"
MAX_WORKERS = 3       

MAX_RETRIES = 20      
RETRY_DELAY = 10.0    

# --- SELF-HEALING VARIABLES ---
error_lock = Lock()
consecutive_errors = 0
MAX_CONSECUTIVE_ERRORS = 10 

if not os.path.exists(FOLDER_TO_WATCH):
    os.makedirs(FOLDER_TO_WATCH)

def setup_database():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Enables write-ahead logging to prevent database locks on multiple threads
    cursor.execute("PRAGMA journal_mode=WAL;") 
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transcripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            filename TEXT,
            transcript_text TEXT
        )
    ''')
    conn.commit()
    conn.close()
    print(f"[DATABASE] Connected to {DB_NAME} successfully.")

setup_database()
transcription_queue = Queue()

print(f"Loading {MODEL_SIZE} model into memory...")
model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
print("Model ready!\n")

def queue_existing_unprocessed_files():
    print("[STARTUP] Checking for existing unprocessed audio files in all subfolders...")
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT filename FROM transcripts")
    processed_files = {row[0] for row in cursor.fetchall()}
    conn.close()

    queued_count = 0
    for root, dirs, files in os.walk(FOLDER_TO_WATCH):
        for filename in sorted(files):
            if filename.lower().endswith((".mp3", ".wav", ".m4a")):
                filepath = os.path.join(root, filename)
                rel_path = os.path.relpath(filepath, FOLDER_TO_WATCH).replace("\\", "/")
                
                if rel_path not in processed_files:
                    transcription_queue.put((filepath, rel_path, 0))
                    queued_count += 1
                
    if queued_count > 0:
        print(f"[STARTUP] Successfully queued {queued_count} old/missed files.\n")
    else:
        print("[STARTUP] No backlog found. We are fully caught up.\n")

def process_task(item):
    global consecutive_errors
    filepath, rel_path, retry_count = item
    
    try:
        if not os.path.exists(filepath):
            return
            
        try:
            size1 = os.path.getsize(filepath)
            time.sleep(1) 
            size2 = os.path.getsize(filepath)
        except FileNotFoundError:
            return 
        
        if size1 == 0 or size1 != size2:
            if retry_count < MAX_RETRIES:
                Timer(RETRY_DELAY, transcription_queue.put, args=(( (filepath, rel_path, retry_count + 1), ))).start()
            return

        print(f"[QUEUE] Starting transcription for: {rel_path}")
        start_time = time.time()
        
        segments, _ = model.transcribe(filepath, beam_size=5, vad_filter=False, language="en", condition_on_previous_text=False)
        
        # [MODIFIED] Strips internal timestamps and just outputs pure readable text
        full_transcript = " ".join([s.text.strip() for s in segments])
        
        end_time = time.time()
        current_time = datetime.now().isoformat()
        
        with sqlite3.connect(DB_NAME, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO transcripts (timestamp, filename, transcript_text) VALUES (?, ?, ?)",
                           (current_time, rel_path, full_transcript))
            conn.commit()
        
        payload = {"filename": rel_path, "transcript_text": full_transcript, "timestamp": current_time}
        requests.post(SERVER_URL, json=payload, timeout=2)
        
        print(f"[SUCCESS] Saved & Broadcasted: {rel_path} ({end_time - start_time:.2f}s)")
        
        with error_lock:
            consecutive_errors = 0
            
    except Exception as e:
        with error_lock:
            consecutive_errors += 1
            current_errors = consecutive_errors
            
        print(f"[ERROR] Transcription failed for {rel_path} ({e}).")
        
        if current_errors >= MAX_CONSECUTIVE_ERRORS:
            print(f"\n[CRITICAL] {MAX_CONSECUTIVE_ERRORS} consecutive errors reached! Model may be corrupted.")
            print("[CRITICAL] Triggering self-destruct to allow start.sh to restart the service...\n")
            os._exit(1) 
            
        if retry_count < MAX_RETRIES:
            Timer(RETRY_DELAY, transcription_queue.put, args=(( (filepath, rel_path, retry_count + 1), ))).start()
    finally:
        transcription_queue.task_done()

def transcription_worker():
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        while True:
            item = transcription_queue.get()
            executor.submit(process_task, item)

class AudioFileHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
            
        src_path = os.fsdecode(event.src_path)
        
        if src_path.lower().endswith((".mp3", ".wav", ".m4a")):
            rel_path = os.path.relpath(src_path, FOLDER_TO_WATCH).replace("\\", "/")
            transcription_queue.put((src_path, rel_path, 0))
            
queue_existing_unprocessed_files()

worker_thread = Thread(target=transcription_worker, daemon=True)
worker_thread.start()

event_handler = AudioFileHandler()
observer = Observer()
observer.schedule(event_handler, FOLDER_TO_WATCH, recursive=True)
observer.start()

print(f"WATCHDOG ONLINE: Monitoring '{FOLDER_TO_WATCH}' and all subfolders.")
print("Press Ctrl+C to stop the system.\n")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\nShutting down Watchdog...")
    observer.stop()
observer.join()