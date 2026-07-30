import requests
import time
import os
import json
import platform
import av
import numpy as np
from threading import Thread, Timer, Lock
from datetime import datetime
from itertools import count
from queue import PriorityQueue
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from faster_whisper import WhisperModel
from faster_whisper.vad import VadOptions, get_speech_timestamps

from backend.config import AUDIO_DIR, DB_NAME, MODEL_DIR, TLS_CA_CERT_PATH
from backend.database import (
    connect,
    infer_channel,
    initialize_database,
    update_heartbeat,
)
from backend.model_manager import (
    PRIMARY_MLX_MODEL,
    RETRY_MLX_MODEL as LARGE_V3_MLX_MODEL,
)
from backend.security import load_security_config
from backend.transcript_quality import (
    assess_transcript,
    choose_retry_result,
    should_retry_with_larger_model,
)

# --- CONFIGURATION ---
FOLDER_TO_WATCH = AUDIO_DIR
MODEL_SIZE = os.environ.get("RADIO_MODEL_SIZE", "medium")
REQUESTED_ENGINE = os.environ.get(
    "RADIO_TRANSCRIPTION_ENGINE",
    "mlx" if platform.system() == "Darwin" and platform.machine() == "arm64" else "faster-whisper",
).lower()
MLX_MODEL = PRIMARY_MLX_MODEL
RETRY_MLX_MODEL = LARGE_V3_MLX_MODEL
SERVER_URL = os.environ.get(
    "RADIO_SERVER_URL", "https://127.0.0.1:8000/api/new_transcript"
)
MAX_WORKERS = max(1, int(os.environ.get("RADIO_MAX_WORKERS", "1")))
LIVE_WINDOW_SECONDS = max(
    60, int(os.environ.get("RADIO_LIVE_WINDOW_MINUTES", "30")) * 60
)

MAX_RETRIES = 20
MAX_RESCUE_RETRIES = max(0, int(os.environ.get("RADIO_MAX_RESCUE_RETRIES", "3")))
MAX_RESCUE_BACKLOG = max(
    1,
    int(os.environ.get("RADIO_RESCUE_BACKLOG_LIMIT", "20")),
)
RETRY_DELAY = 10.0
RECONCILE_SECONDS = max(60, int(os.environ.get("RADIO_RECONCILE_SECONDS", "300")))
RETRY_MAX_DURATION = max(
    15,
    int(os.environ.get("RADIO_RETRY_MAX_DURATION_SECONDS", "180")),
)
VAD_OPTIONS = VadOptions(min_silence_duration_ms=500, speech_pad_ms=250)
INTERNAL_TOKEN = load_security_config()["internal_token"]

# --- SELF-HEALING VARIABLES ---
error_lock = Lock()
consecutive_errors = 0
MAX_CONSECUTIVE_ERRORS = 10


initialize_database()
print(f"[DATABASE] Connected to {DB_NAME} successfully.")
transcription_queue = PriorityQueue()
queue_sequence = count()
queued_files = set()
queue_lock = Lock()

os.makedirs(MODEL_DIR, exist_ok=True)
mlx_whisper = None
model = None
active_engine = REQUESTED_ENGINE

if REQUESTED_ENGINE == "mlx":
    try:
        import mlx_whisper as _mlx_whisper

        mlx_whisper = _mlx_whisper
        print(f"[ENGINE] Apple MLX/Metal ready with {MLX_MODEL}.")
    except Exception as exc:
        print(f"[ENGINE] MLX unavailable ({exc}); falling back to faster-whisper CPU.")
        active_engine = "faster-whisper"

if active_engine != "mlx":
    print(f"Loading {MODEL_SIZE} model into CPU memory...")
    model = WhisperModel(
        MODEL_SIZE,
        device="cpu",
        compute_type="int8",
        download_root=MODEL_DIR,
        cpu_threads=max(4, (os.cpu_count() or 8) - 2),
        num_workers=1,
    )
    print("CPU model ready!\n")

if active_engine == "mlx" and MAX_WORKERS > 1:
    print("[ENGINE] MLX uses one queue worker to avoid GPU memory contention.")
    MAX_WORKERS = 1


def decode_audio(filepath):
    chunks = []
    with av.open(filepath) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in container.decode(stream):
            for converted in resampler.resample(frame):
                chunks.append(converted.to_ndarray().reshape(-1))
        for converted in resampler.resample(None):
            chunks.append(converted.to_ndarray().reshape(-1))
    if not chunks:
        return np.empty(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def voice_activity_segments(audio):
    if audio.size == 0:
        return []
    speech_regions = get_speech_timestamps(audio, VAD_OPTIONS, sampling_rate=16000)
    if not speech_regions:
        return []

    # Keep nearby speech in its natural timeline. The previous implementation
    # concatenated every VAD chunk, creating abrupt word-to-word joins that can
    # encourage Whisper repetition loops.
    grouped_regions = []
    for region in speech_regions:
        if (
            grouped_regions
            and region["start"] - grouped_regions[-1]["end"] <= 2 * 16000
        ):
            grouped_regions[-1]["end"] = region["end"]
        else:
            grouped_regions.append(dict(region))
    return [
        audio[region["start"] : region["end"]]
        for region in grouped_regions
        if region["end"] - region["start"] >= int(0.2 * 16000)
    ]


def transcribe_audio(filepath, model_repo=None):
    requested_model = model_repo or MLX_MODEL
    if active_engine == "mlx":
        audio = decode_audio(filepath)
        duration = len(audio) / 16000
        speech_segments = voice_activity_segments(audio)
        if not speech_segments:
            return "", duration, {"no_speech_prob": 1.0}
        results = [
            mlx_whisper.transcribe(
                speech_audio,
                path_or_hf_repo=requested_model,
                language="en",
                condition_on_previous_text=False,
                temperature=(0.0, 0.2, 0.4),
                compression_ratio_threshold=2.2,
                logprob_threshold=-0.9,
                no_speech_threshold=0.7,
                word_timestamps=True,
                hallucination_silence_threshold=1.5,
                verbose=False,
            )
            for speech_audio in speech_segments
        ]
        decoded_segments = [
            segment
            for result in results
            for segment in (result.get("segments") or [])
        ]
        metrics = {}
        compression_values = [
            segment.get("compression_ratio")
            for segment in decoded_segments
            if segment.get("compression_ratio") is not None
        ]
        logprob_values = [
            segment.get("avg_logprob")
            for segment in decoded_segments
            if segment.get("avg_logprob") is not None
        ]
        no_speech_values = [
            segment.get("no_speech_prob")
            for segment in decoded_segments
            if segment.get("no_speech_prob") is not None
        ]
        if compression_values:
            metrics["compression_ratio"] = max(compression_values)
        if logprob_values:
            metrics["avg_logprob"] = min(logprob_values)
        if no_speech_values:
            metrics["no_speech_prob"] = max(no_speech_values)
        metrics["model"] = requested_model
        transcript = " ".join(
            (result.get("text") or "").strip()
            for result in results
            if (result.get("text") or "").strip()
        )
        return transcript, duration, metrics

    segments, info = model.transcribe(
        filepath,
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 250},
        language="en",
        condition_on_previous_text=False,
    )
    segments = list(segments)
    metrics = {}
    compression_values = [
        segment.compression_ratio
        for segment in segments
        if getattr(segment, "compression_ratio", None) is not None
    ]
    logprob_values = [
        segment.avg_logprob
        for segment in segments
        if getattr(segment, "avg_logprob", None) is not None
    ]
    no_speech_values = [
        segment.no_speech_prob
        for segment in segments
        if getattr(segment, "no_speech_prob", None) is not None
    ]
    if compression_values:
        metrics["compression_ratio"] = max(compression_values)
    if logprob_values:
        metrics["avg_logprob"] = min(logprob_values)
    if no_speech_values:
        metrics["no_speech_prob"] = max(no_speech_values)
    return (
        " ".join(segment.text.strip() for segment in segments).strip(),
        info.duration,
        metrics,
    )

def recorded_at_for_file(rel_path):
    filename = os.path.basename(rel_path)
    try:
        return datetime.strptime(filename[:19], "%Y-%m-%d-%H-%M-%S")
    except (TypeError, ValueError):
        try:
            return datetime.fromtimestamp(os.path.getmtime(os.path.join(FOLDER_TO_WATCH, rel_path)))
        except OSError:
            return datetime.now()


def priority_for_file(rel_path):
    try:
        recorded_at = recorded_at_for_file(rel_path)
        age_seconds = (datetime.now() - recorded_at).total_seconds()
        tier = 0 if age_seconds <= LIVE_WINDOW_SECONDS else 1
        return tier, recorded_at.timestamp()
    except (TypeError, ValueError, OSError):
        return 1, time.time()

def enqueue_file(
    filepath,
    rel_path,
    retry_count=0,
    priority_key=None,
    task_type="primary",
):
    queue_key = (task_type, rel_path)
    if retry_count == 0:
        with queue_lock:
            if queue_key in queued_files:
                return False
            queued_files.add(queue_key)
    if task_type == "quality_retry":
        tier, recorded_timestamp = (2, recorded_at_for_file(rel_path).timestamp())
    else:
        tier, recorded_timestamp = priority_key or priority_for_file(rel_path)
    transcription_queue.put(
        (
            tier,
            recorded_timestamp,
            next(queue_sequence),
            task_type,
            filepath,
            rel_path,
            retry_count,
        )
    )
    return True

def queue_existing_unprocessed_files():
    print("[STARTUP] Checking for existing unprocessed audio files in all subfolders...")
    with connect(read_only=True) as connection:
        processed_files = {
            row["filename"]
            for row in connection.execute("SELECT filename FROM transcripts").fetchall()
        }

    queued_count = 0
    for root, dirs, files in os.walk(FOLDER_TO_WATCH):
        for filename in sorted(files):
            if filename.lower().endswith((".mp3", ".wav", ".m4a")):
                filepath = os.path.join(root, filename)
                rel_path = os.path.relpath(filepath, FOLDER_TO_WATCH).replace("\\", "/")
                
                if rel_path not in processed_files:
                    if enqueue_file(filepath, rel_path):
                        queued_count += 1
                
    if queued_count > 0:
        print(f"[STARTUP] Successfully queued {queued_count} old/missed files.\n")
    else:
        print("[STARTUP] No backlog found. We are fully caught up.\n")


def queue_existing_quality_retries():
    if active_engine != "mlx":
        return
    with connect(read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT filename, transcript_text, quality_score, quality_reason,
                   quality_metrics, status
            FROM transcripts
            WHERE retry_model IS NULL
              AND corrected_at IS NULL
              AND status != 'blank'
            ORDER BY coalesce(recorded_at, timestamp) DESC, id DESC
            LIMIT ?
            """,
            (MAX_RESCUE_BACKLOG,),
        ).fetchall()

    queued_count = 0
    for row in rows:
        try:
            metrics = json.loads(row["quality_metrics"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            metrics = {}
        quality = {
            "score": row["quality_score"],
            "reason": row["quality_reason"] or "",
            "metrics": metrics,
            "status": row["status"],
        }
        if not should_retry_with_larger_model(
            quality,
            max_duration=RETRY_MAX_DURATION,
        ):
            continue
        rel_path = row["filename"]
        filepath = os.path.join(FOLDER_TO_WATCH, rel_path)
        if os.path.isfile(filepath) and enqueue_file(
            filepath,
            rel_path,
            task_type="quality_retry",
        ):
            queued_count += 1
    if queued_count:
        print(
            f"[STARTUP] Queued {queued_count} low-quality transcript(s) "
            "for selective Large V3 rescue."
        )


def process_task(item):
    global consecutive_errors
    tier, recorded_timestamp, _, task_type, filepath, rel_path, retry_count = item
    queue_key = (task_type, rel_path)
    priority_key = (tier, recorded_timestamp)
    retry_scheduled = False
    
    try:
        if not os.path.exists(filepath):
            return
            
        try:
            file_size = os.path.getsize(filepath)
        except FileNotFoundError:
            return 
        
        if file_size == 0:
            retry_limit = (
                MAX_RESCUE_RETRIES
                if task_type == "quality_retry"
                else MAX_RETRIES
            )
            if retry_count < retry_limit:
                retry_scheduled = True
                retry_timer = Timer(
                    RETRY_DELAY,
                    enqueue_file,
                    args=(filepath, rel_path),
                    kwargs={
                        "retry_count": retry_count + 1,
                        "priority_key": priority_key,
                        "task_type": task_type,
                    },
                )
                retry_timer.daemon = True
                retry_timer.start()
            return

        if task_type == "quality_retry":
            with connect(read_only=True) as connection:
                existing = connection.execute(
                    """
                    SELECT transcript_text, quality_score, quality_reason,
                           quality_metrics, status, retry_model
                    FROM transcripts
                    WHERE filename = ?
                    """,
                    (rel_path,),
                ).fetchone()
            if existing is None or existing["retry_model"]:
                return
            queue_name = "LARGE-V3-RESCUE"
            requested_model = RETRY_MLX_MODEL
        else:
            existing = None
            queue_name = "LIVE" if tier == 0 else "BACKLOG"
            requested_model = MLX_MODEL
        print(f"[QUEUE:{queue_name}] Starting transcription for: {rel_path}")
        start_time = time.time()
        
        full_transcript, audio_duration, engine_metrics = transcribe_audio(
            filepath,
            requested_model,
        )
        quality = assess_transcript(full_transcript, audio_duration, engine_metrics)
        
        end_time = time.time()
        current_time = datetime.now().isoformat()

        if task_type == "quality_retry":
            try:
                primary_metrics = json.loads(existing["quality_metrics"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                primary_metrics = {}
            primary_quality = {
                "score": existing["quality_score"],
                "reason": existing["quality_reason"] or "",
                "metrics": primary_metrics,
                "status": existing["status"],
            }
            selected_text, selected_quality, used_retry = choose_retry_result(
                existing["transcript_text"] or "",
                primary_quality,
                full_transcript,
                quality,
            )
            with connect() as connection:
                connection.execute(
                    """
                    UPDATE transcripts
                    SET retry_transcript_text = ?,
                        retry_model = ?,
                        retry_quality_score = ?,
                        retry_quality_reason = ?,
                        retry_quality_metrics = ?,
                        retry_status = ?,
                        retry_attempted_at = ?,
                        transcript_text = ?,
                        quality_score = ?,
                        quality_reason = ?,
                        quality_metrics = ?,
                        status = ?,
                        transcription_model = CASE
                            WHEN ? THEN ? ELSE transcription_model
                        END,
                        broadcast_pending = CASE
                            WHEN ? = 'ready' THEN 1 ELSE broadcast_pending
                        END
                    WHERE filename = ?
                    """,
                    (
                        full_transcript,
                        RETRY_MLX_MODEL,
                        quality["score"],
                        quality["reason"],
                        json.dumps(
                            quality["metrics"],
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        "selected" if used_retry else "kept_primary",
                        current_time,
                        selected_text,
                        selected_quality["score"],
                        selected_quality["reason"],
                        json.dumps(
                            selected_quality["metrics"],
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        selected_quality["status"],
                        int(used_retry),
                        RETRY_MLX_MODEL,
                        selected_quality["status"],
                        rel_path,
                    ),
                )
                connection.commit()
            inserted = False
            result = (
                "Large V3 rescue selected"
                if used_retry
                else "Large V3 rescue retained for review; Medium result kept"
            )
        else:
            recording_time = recorded_at_for_file(rel_path)
            with connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO transcripts (
                        timestamp, recorded_at, recording_year, channel,
                        filename, transcript_text,
                        raw_transcript_text, quality_score, quality_reason,
                        quality_metrics, status, broadcast_pending,
                        transcription_model
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        current_time,
                        recording_time.isoformat(),
                        recording_time.year,
                        infer_channel(rel_path),
                        rel_path,
                        full_transcript,
                        full_transcript,
                        quality["score"],
                        quality["reason"],
                        json.dumps(
                            quality["metrics"],
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        quality["status"],
                        int(quality["status"] == "ready"),
                        MLX_MODEL if active_engine == "mlx" else MODEL_SIZE,
                    ),
                )
                inserted = cursor.rowcount == 1
                connection.commit()

            if inserted and quality["status"] == "blank":
                result = "Saved as processed; blank audio hidden from dashboard"
            elif inserted and quality["status"] == "suspect":
                result = f"Quarantined suspect transcript ({quality['reason']})"
            elif inserted:
                result = "Saved; queued for durable dashboard delivery"
            else:
                result = "Already saved"
            if (
                inserted
                and active_engine == "mlx"
                and should_retry_with_larger_model(
                    quality,
                    audio_duration,
                    RETRY_MAX_DURATION,
                )
            ):
                enqueue_file(
                    filepath,
                    rel_path,
                    task_type="quality_retry",
                )
        print(
            f"[SUCCESS] {result}: {rel_path} "
            f"(audio={audio_duration:.1f}s, processing={end_time - start_time:.2f}s, "
            f"engine={active_engine})"
        )
        
        if task_type == "primary":
            with error_lock:
                consecutive_errors = 0
        update_heartbeat(
            "worker",
            "online",
            {
                "engine": active_engine,
                "model": requested_model,
                "task_type": task_type,
                "queue_depth": transcription_queue.qsize(),
                "last_file": rel_path,
                "last_quality_status": quality["status"],
            },
        )
            
    except Exception as e:
        if task_type == "primary":
            with error_lock:
                consecutive_errors += 1
                current_errors = consecutive_errors
        else:
            current_errors = 0
            
        print(f"[ERROR] Transcription failed for {rel_path} ({e}).")
        
        if task_type == "primary" and current_errors >= MAX_CONSECUTIVE_ERRORS:
            print(f"\n[CRITICAL] {MAX_CONSECUTIVE_ERRORS} consecutive errors reached! Model may be corrupted.")
            print("[CRITICAL] Triggering self-destruct to allow start.sh to restart the service...\n")
            os._exit(1) 
            
        retry_limit = (
            MAX_RESCUE_RETRIES
            if task_type == "quality_retry"
            else MAX_RETRIES
        )
        if retry_count < retry_limit:
            retry_scheduled = True
            retry_timer = Timer(
                RETRY_DELAY,
                enqueue_file,
                args=(filepath, rel_path),
                kwargs={
                    "retry_count": retry_count + 1,
                    "priority_key": priority_key,
                    "task_type": task_type,
                },
            )
            retry_timer.daemon = True
            retry_timer.start()
        elif task_type == "quality_retry":
            with connect() as connection:
                connection.execute(
                    """
                    UPDATE transcripts
                    SET retry_model = ?,
                        retry_status = 'failed',
                        retry_attempted_at = ?
                    WHERE filename = ? AND retry_model IS NULL
                    """,
                    (RETRY_MLX_MODEL, datetime.now().isoformat(), rel_path),
                )
                connection.commit()
    finally:
        if not retry_scheduled:
            with queue_lock:
                queued_files.discard(queue_key)
        transcription_queue.task_done()

def transcription_worker():
    while True:
        item = transcription_queue.get()
        process_task(item)


def deliver_pending_transcripts():
    while True:
        rows = []
        try:
            with connect(read_only=True) as connection:
                rows = connection.execute(
                    """
                    SELECT id, filename, transcript_text, timestamp, recorded_at,
                           quality_score, quality_reason, status
                    FROM transcripts
                    WHERE broadcast_pending = 1 AND status = 'ready'
                    ORDER BY id ASC
                    LIMIT 50
                    """
                ).fetchall()
            for row in rows:
                payload = dict(row)
                try:
                    response = requests.post(
                        SERVER_URL,
                        json=payload,
                        headers={"X-Radio-Internal-Token": INTERNAL_TOKEN},
                        timeout=5,
                        verify=TLS_CA_CERT_PATH,
                    )
                    response.raise_for_status()
                    with connect() as connection:
                        connection.execute(
                            """
                            UPDATE transcripts
                            SET broadcast_pending = 0,
                                broadcast_attempts = broadcast_attempts + 1,
                                last_broadcast_error = NULL
                            WHERE id = ?
                            """,
                            (row["id"],),
                        )
                        connection.commit()
                except requests.RequestException as exc:
                    with connect() as connection:
                        connection.execute(
                            """
                            UPDATE transcripts
                            SET broadcast_attempts = broadcast_attempts + 1,
                                last_broadcast_error = ?
                            WHERE id = ?
                            """,
                            (str(exc)[:500], row["id"]),
                        )
                        connection.commit()
                    print(f"[DELIVERY] Dashboard unavailable; will retry: {exc}")
                    break
        except Exception as exc:
            print(f"[DELIVERY] Retry loop error: {exc}")
        time.sleep(2 if rows else 5)


def heartbeat_worker():
    while True:
        try:
            update_heartbeat(
                "worker",
                "online",
                {
                    "engine": active_engine,
                    "model": MLX_MODEL if active_engine == "mlx" else MODEL_SIZE,
                    "retry_model": RETRY_MLX_MODEL if active_engine == "mlx" else None,
                    "queue_depth": transcription_queue.qsize(),
                },
            )
        except Exception as exc:
            print(f"[HEARTBEAT] Worker heartbeat failed: {exc}")
        time.sleep(10)


class AudioFileHandler(FileSystemEventHandler):
    def queue_event_path(self, event_path):
        src_path = os.fsdecode(event_path)
        if src_path.lower().endswith((".mp3", ".wav", ".m4a")):
            rel_path = os.path.relpath(src_path, FOLDER_TO_WATCH).replace("\\", "/")
            enqueue_file(src_path, rel_path)

    def on_created(self, event):
        if not event.is_directory:
            self.queue_event_path(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self.queue_event_path(event.dest_path)

def periodic_reconciliation():
    while True:
        time.sleep(RECONCILE_SECONDS)
        queue_existing_unprocessed_files()
        queue_existing_quality_retries()

queue_existing_unprocessed_files()
queue_existing_quality_retries()

worker_threads = []
for _ in range(MAX_WORKERS):
    worker_thread = Thread(target=transcription_worker, daemon=True)
    worker_thread.start()
    worker_threads.append(worker_thread)

reconciliation_thread = Thread(target=periodic_reconciliation, daemon=True)
reconciliation_thread.start()

delivery_thread = Thread(target=deliver_pending_transcripts, daemon=True)
delivery_thread.start()

heartbeat_thread = Thread(target=heartbeat_worker, daemon=True)
heartbeat_thread.start()

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
