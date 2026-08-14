import requests
import time
import os
import json
import platform
import av
import numpy as np
from threading import Thread, Lock
from datetime import datetime
from itertools import count
from queue import PriorityQueue
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from faster_whisper import WhisperModel
from faster_whisper.vad import VadOptions, get_speech_timestamps

from backend.config import (
    AUDIO_DIR,
    DB_NAME,
    MODEL_DIR,
    TLS_CA_CERT_PATH,
    bool_setting,
    number_setting,
    text_setting,
)
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
from backend.glossaries import prompt_for_channel
from backend.analytics import profile_for_channel
from backend.jobs import (
    claim_job,
    complete_job,
    defer_job,
    due_jobs,
    enqueue_job,
    fail_or_reschedule_job,
    has_live_primary_work,
    queue_summary,
)
from backend.security import load_security_config
from backend.transcript_quality import (
    assess_transcript,
    choose_retry_result,
    explain_retry_selection,
    explain_retry_trigger,
    should_retry_with_larger_model,
)

# --- CONFIGURATION ---
FOLDER_TO_WATCH = AUDIO_DIR
MODEL_SIZE = text_setting("model_size", "RADIO_MODEL_SIZE", "medium")
REQUESTED_ENGINE = text_setting(
    "transcription_engine",
    "RADIO_TRANSCRIPTION_ENGINE",
    "mlx" if platform.system() == "Darwin" and platform.machine() == "arm64" else "faster-whisper",
).lower()
MLX_MODEL = PRIMARY_MLX_MODEL
RETRY_MLX_MODEL = LARGE_V3_MLX_MODEL
SERVER_URL = os.environ.get(
    "RADIO_SERVER_URL", "https://127.0.0.1:8000/api/new_transcript"
)
MAX_WORKERS = max(1, int(os.environ.get("RADIO_MAX_WORKERS", "1")))
LIVE_WINDOW_SECONDS = 60 * number_setting(
    "live_window_minutes", "RADIO_LIVE_WINDOW_MINUTES", 30, minimum=1, maximum=1440
)

MAX_RETRIES = 20
RETRY_DELAY = 10.0
RECONCILE_SECONDS = max(60, int(os.environ.get("RADIO_RECONCILE_SECONDS", "300")))


# These are read on every use so an administrator can retune the rescue pass
# from the settings screen without restarting the transcription worker.
def rescue_is_enabled():
    return bool_setting("rescue_enabled", default=True)


def max_rescue_retries():
    return number_setting(
        "max_rescue_retries", "RADIO_MAX_RESCUE_RETRIES", 3, minimum=0, maximum=10
    )


def rescue_backlog_limit():
    return number_setting(
        "rescue_backlog_limit",
        "RADIO_RESCUE_BACKLOG_LIMIT",
        20,
        minimum=1,
        maximum=500,
    )


def rescue_max_duration():
    return number_setting(
        "rescue_max_duration_seconds",
        "RADIO_RETRY_MAX_DURATION_SECONDS",
        180,
        minimum=15,
        maximum=3600,
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
    # Each chunk carries where it began in the original recording. Whisper times
    # its segments from the start of whatever audio it is handed, so without the
    # offset every chunk after the first would report timestamps near zero.
    return [
        (audio[region["start"] : region["end"]], region["start"] / 16000)
        for region in grouped_regions
        if region["end"] - region["start"] >= int(0.2 * 16000)
    ]


def prepare_audio(audio, normalize=False, denoise=False):
    prepared = audio.copy()
    if denoise and prepared.size:
        floor = float(np.percentile(np.abs(prepared), 20))
        prepared[np.abs(prepared) < floor * 1.5] = 0
    if normalize and prepared.size:
        peak = float(np.max(np.abs(prepared)))
        if peak > 0.01:
            prepared = prepared * min(4.0, 0.95 / peak)
    return prepared


def transcribe_audio(
    filepath,
    model_repo=None,
    initial_prompt=None,
    language="en",
    normalize=False,
    denoise=False,
):
    requested_model = model_repo or MLX_MODEL
    if active_engine == "mlx":
        audio = prepare_audio(decode_audio(filepath), normalize, denoise)
        duration = len(audio) / 16000
        speech_segments = voice_activity_segments(audio)
        if not speech_segments:
            return "", duration, {"no_speech_prob": 1.0}
        results = []
        decoded_segments = []
        for speech_audio, offset_seconds in speech_segments:
            result = mlx_whisper.transcribe(
                speech_audio,
                path_or_hf_repo=requested_model,
                language=language,
                initial_prompt=initial_prompt,
                condition_on_previous_text=False,
                temperature=(0.0, 0.2, 0.4),
                compression_ratio_threshold=2.2,
                logprob_threshold=-0.9,
                no_speech_threshold=0.7,
                word_timestamps=True,
                hallucination_silence_threshold=1.5,
                verbose=False,
            )
            results.append(result)
            for segment in result.get("segments") or []:
                # Shift back onto the recording's own timeline so segment
                # seeking lands on the audio the operator is reading.
                located = dict(segment)
                located["start"] = float(segment.get("start") or 0) + offset_seconds
                located["end"] = float(segment.get("end") or 0) + offset_seconds
                decoded_segments.append(located)
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
        metrics["_segments"] = [
            {
                "start": float(segment.get("start") or 0),
                "end": float(segment.get("end") or 0),
                "text": (segment.get("text") or "").strip(),
                "confidence": (
                    round(float(np.exp(segment["avg_logprob"])), 4)
                    if segment.get("avg_logprob") is not None
                    else None
                ),
            }
            for segment in decoded_segments
            if (segment.get("text") or "").strip()
        ]
        transcript = " ".join(
            (result.get("text") or "").strip()
            for result in results
            if (result.get("text") or "").strip()
        )
        return transcript, duration, metrics

    audio_input = (
        prepare_audio(decode_audio(filepath), normalize, denoise)
        if normalize or denoise
        else filepath
    )
    segments, info = model.transcribe(
        audio_input,
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 250},
        language=language,
        initial_prompt=initial_prompt,
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
    metrics["_segments"] = [
        {
            "start": float(segment.start),
            "end": float(segment.end),
            "text": segment.text.strip(),
            "confidence": (
                round(float(np.exp(segment.avg_logprob)), 4)
                if getattr(segment, "avg_logprob", None) is not None
                else None
            ),
        }
        for segment in segments
        if segment.text.strip()
    ]
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
    if task_type == "quality_retry":
        tier, recorded_timestamp = (2, recorded_at_for_file(rel_path).timestamp())
        retry_limit = max_rescue_retries() + 1
        requested_model = RETRY_MLX_MODEL
    else:
        tier, recorded_timestamp = priority_key or priority_for_file(rel_path)
        retry_limit = MAX_RETRIES + 1
        requested_model = MLX_MODEL
    job = enqueue_job(
        task_type,
        rel_path,
        priority=tier,
        max_attempts=retry_limit,
        model=requested_model,
    )
    if job["status"] not in {"queued", "retry_wait"}:
        return False
    with queue_lock:
        if queue_key in queued_files:
            return False
        queued_files.add(queue_key)
    transcription_queue.put(
        (
            tier,
            recorded_timestamp,
            next(queue_sequence),
            task_type,
            filepath,
            rel_path,
            job["attempt_count"],
            job["id"],
        )
    )
    return True


def dispatch_due_jobs():
    for job in due_jobs():
        queue_key = (job["job_type"], job["relative_path"])
        with queue_lock:
            if queue_key in queued_files:
                continue
            queued_files.add(queue_key)
        transcription_queue.put(
            (
                job["priority"],
                recorded_at_for_file(job["relative_path"]).timestamp(),
                next(queue_sequence),
                job["job_type"],
                os.path.join(FOLDER_TO_WATCH, job["relative_path"]),
                job["relative_path"],
                job["attempt_count"],
                job["id"],
            )
        )

def unprocessed_paths(connection, relative_paths):
    """Return the recordings in this batch that have no transcript yet."""
    if not relative_paths:
        return []
    placeholders = ",".join("?" * len(relative_paths))
    known = {
        row["filename"]
        for row in connection.execute(
            f"SELECT filename FROM transcripts WHERE filename IN ({placeholders})",
            relative_paths,
        )
    }
    return [path for path in relative_paths if path not in known]


def queue_existing_unprocessed_files(batch_size=500):
    """Queue any recording on disk that has never produced a transcript.

    Recordings are checked in batches rather than by loading every processed
    filename into memory, so an archive with hundreds of thousands of files
    costs the same as a small one on each reconciliation pass.
    """
    queued_count = 0
    batch = []
    with connect(read_only=True) as connection:
        def flush(paths):
            queued = 0
            for rel_path in unprocessed_paths(connection, paths):
                if enqueue_file(os.path.join(FOLDER_TO_WATCH, rel_path), rel_path):
                    queued += 1
            return queued

        for root, _, files in os.walk(FOLDER_TO_WATCH):
            for filename in sorted(files):
                if not filename.lower().endswith((".mp3", ".wav", ".m4a")):
                    continue
                filepath = os.path.join(root, filename)
                batch.append(
                    os.path.relpath(filepath, FOLDER_TO_WATCH).replace("\\", "/")
                )
                if len(batch) >= batch_size:
                    queued_count += flush(batch)
                    batch = []
        queued_count += flush(batch)

    if queued_count > 0:
        print(f"[RECONCILE] Queued {queued_count} recording(s) that had no transcript.")
    return queued_count


def queue_existing_quality_retries():
    if active_engine != "mlx" or not rescue_is_enabled():
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
            (rescue_backlog_limit(),),
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
            max_duration=rescue_max_duration(),
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


def save_transcript_segments(transcript_id, result_kind, segments):
    with connect() as connection:
        connection.execute(
            "DELETE FROM transcript_segments WHERE transcript_id = ? AND result_kind = ?",
            (int(transcript_id), result_kind),
        )
        connection.executemany(
            """
            INSERT INTO transcript_segments(
                transcript_id, result_kind, segment_index,
                start_seconds, end_seconds, text, confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    int(transcript_id),
                    result_kind,
                    index,
                    max(0, float(segment.get("start") or 0)),
                    max(0, float(segment.get("end") or 0)),
                    str(segment.get("text") or "").strip(),
                    segment.get("confidence"),
                )
                for index, segment in enumerate(segments)
                if str(segment.get("text") or "").strip()
            ],
        )
        connection.commit()


def process_task(item):
    global consecutive_errors
    tier, recorded_timestamp, _, task_type, filepath, rel_path, retry_count, job_id = item
    queue_key = (task_type, rel_path)
    claimed = claim_job(job_id)
    if claimed is None:
        with queue_lock:
            queued_files.discard(queue_key)
        transcription_queue.task_done()
        return

    try:
        if not os.path.exists(filepath):
            fail_or_reschedule_job(job_id, "Audio file is not available", RETRY_DELAY)
            return
            
        try:
            file_size = os.path.getsize(filepath)
        except FileNotFoundError:
            fail_or_reschedule_job(job_id, "Audio file disappeared while queued", RETRY_DELAY)
            return 
        
        if file_size == 0:
            fail_or_reschedule_job(job_id, "Audio file is still empty", RETRY_DELAY)
            return

        if task_type == "quality_retry":
            if has_live_primary_work(excluding_job_id=job_id):
                defer_job(job_id, 5)
                return
            with connect(read_only=True) as connection:
                existing = connection.execute(
                    """
                    SELECT id, raw_transcript_text, transcript_text,
                           primary_quality_score, primary_quality_reason,
                           primary_quality_metrics, primary_status,
                           quality_score, quality_reason,
                           quality_metrics, status, retry_model
                    FROM transcripts
                    WHERE filename = ?
                    """,
                    (rel_path,),
                ).fetchone()
            if existing is None or existing["retry_model"]:
                complete_job(job_id, existing["id"] if existing else None)
                return
            queue_name = "LARGE-V3-RESCUE"
            requested_model = RETRY_MLX_MODEL
        else:
            existing = None
            queue_name = "LIVE" if tier == 0 else "BACKLOG"
            requested_model = MLX_MODEL
        channel = infer_channel(rel_path)
        glossary_prompt, glossary_language = prompt_for_channel(channel)
        channel_profile = profile_for_channel(channel)
        detected_language = (
            None
            if channel_profile.get("experimental_enabled")
            and channel_profile.get("language_mode") == "detect"
            else glossary_language
        )
        print(f"[QUEUE:{queue_name}] Starting transcription for: {rel_path}")
        start_time = time.time()
        
        full_transcript, audio_duration, engine_metrics = transcribe_audio(
            filepath,
            requested_model,
            glossary_prompt,
            detected_language,
            bool(channel_profile.get("normalize_audio")),
            bool(
                channel_profile.get("experimental_enabled")
                and channel_profile.get("denoise_audio")
            ),
        )
        decoded_segments = engine_metrics.pop("_segments", [])
        quality = assess_transcript(full_transcript, audio_duration, engine_metrics)
        
        end_time = time.time()
        current_time = datetime.now().isoformat()

        if task_type == "quality_retry":
            try:
                primary_metrics = json.loads(
                    existing["primary_quality_metrics"]
                    or existing["quality_metrics"]
                    or "{}"
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                primary_metrics = {}
            primary_quality = {
                "score": existing["primary_quality_score"]
                if existing["primary_quality_score"] is not None
                else existing["quality_score"],
                "reason": existing["primary_quality_reason"]
                or existing["quality_reason"]
                or "",
                "metrics": primary_metrics,
                "status": existing["primary_status"] or "ready",
            }
            selected_text, selected_quality, used_retry = choose_retry_result(
                existing["raw_transcript_text"] or existing["transcript_text"] or "",
                primary_quality,
                full_transcript,
                quality,
            )
            selection_reason = explain_retry_selection(
                primary_quality, quality, used_retry
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
                        selection_reason = ?,
                        selected_transcript = ?,
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
                        selection_reason,
                        "retry" if used_retry else "primary",
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
            complete_job(job_id, existing["id"])
            save_transcript_segments(existing["id"], "retry", decoded_segments)
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
                        transcription_model, primary_quality_score,
                        primary_quality_reason, primary_quality_metrics,
                        primary_status, primary_model, selected_transcript,
                        selection_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        quality["score"],
                        quality["reason"],
                        json.dumps(
                            quality["metrics"],
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        quality["status"],
                        MLX_MODEL if active_engine == "mlx" else MODEL_SIZE,
                        "primary",
                        "Medium result selected by default",
                    ),
                )
                inserted = cursor.rowcount == 1
                transcript_row = connection.execute(
                    "SELECT id FROM transcripts WHERE filename = ?", (rel_path,)
                ).fetchone()
                connection.commit()
            complete_job(job_id, transcript_row["id"] if transcript_row else None)
            if transcript_row:
                save_transcript_segments(transcript_row["id"], "primary", decoded_segments)

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
                and rescue_is_enabled()
                and should_retry_with_larger_model(
                    quality,
                    audio_duration,
                    rescue_max_duration(),
                )
            ):
                retry_trigger = explain_retry_trigger(quality)
                with connect() as connection:
                    connection.execute(
                        "UPDATE transcripts SET retry_trigger_reason = ? WHERE filename = ?",
                        (retry_trigger, rel_path),
                    )
                    connection.commit()
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
                "durable_queue": queue_summary(),
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
            max_rescue_retries()
            if task_type == "quality_retry"
            else MAX_RETRIES
        )
        job = fail_or_reschedule_job(job_id, e, RETRY_DELAY)
        if task_type == "quality_retry" and job and job["status"] == "failed":
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
        with queue_lock:
            queued_files.discard(queue_key)
        transcription_queue.task_done()

def transcription_worker():
    while True:
        item = transcription_queue.get()
        try:
            process_task(item)
        except Exception as exc:
            # process_task handles its own failures; anything escaping it would
            # otherwise end this thread and stop transcription silently until
            # somebody noticed and restarted the service.
            print(f"[QUEUE] Worker recovered from an unexpected failure: {exc}")
            try:
                with queue_lock:
                    queued_files.discard((item[3], item[5]))
            except (IndexError, TypeError):
                pass
            try:
                transcription_queue.task_done()
            except ValueError:
                pass


def deliver_pending_transcripts():
    failures = 0
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
                    failures += 1
                    if failures in (1, 5) or failures % 30 == 0:
                        # A dashboard that is down for hours must not fill the
                        # log with one line every couple of seconds.
                        print(
                            f"[DELIVERY] Dashboard unavailable; will keep retrying "
                            f"(attempt {failures}): {exc}"
                        )
                    break
            else:
                failures = 0
        except Exception as exc:
            failures += 1
            print(f"[DELIVERY] Retry loop error: {exc}")
        if failures:
            # Back off to at most a minute between attempts while the dashboard
            # is unreachable, then resume immediately once it answers.
            time.sleep(min(60, 2 ** min(failures, 6)))
        else:
            time.sleep(2 if rows else 5)


def heartbeat_worker():
    while True:
        try:
            durable = queue_summary()
            primary_depth = sum(
                durable.get("primary", {}).get(state, 0)
                for state in ("queued", "running", "retry_wait")
            )
            rescue_depth = sum(
                durable.get("quality_retry", {}).get(state, 0)
                for state in ("queued", "running", "retry_wait")
            )
            update_heartbeat(
                "worker",
                "online",
                {
                    "engine": active_engine,
                    "model": MLX_MODEL if active_engine == "mlx" else MODEL_SIZE,
                    "retry_model": RETRY_MLX_MODEL if active_engine == "mlx" else None,
                    "queue_depth": primary_depth,
                    "rescue_queue_depth": rescue_depth,
                    "durable_queue": durable,
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
            try:
                enqueue_file(src_path, rel_path)
            except Exception as exc:
                # A failure here runs on the watchdog's own thread. Letting it
                # escape stops file watching entirely; the periodic
                # reconciliation pass picks the recording up instead.
                print(f"[WATCHDOG] Could not queue {rel_path}: {exc}")

    def on_created(self, event):
        if not event.is_directory:
            self.queue_event_path(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self.queue_event_path(event.dest_path)

def periodic_reconciliation():
    while True:
        time.sleep(RECONCILE_SECONDS)
        try:
            queue_existing_unprocessed_files()
            queue_existing_quality_retries()
        except Exception as exc:
            # Reconciliation is the safety net that catches recordings the file
            # watcher missed, so it has to survive a transient database or disk
            # error rather than ending its thread.
            print(f"[RECONCILE] Pass failed, will try again: {exc}")


def durable_job_dispatcher():
    while True:
        try:
            dispatch_due_jobs()
        except Exception as exc:
            print(f"[QUEUE] Durable job dispatcher failed: {exc}")
        time.sleep(1)

def run_service():
    """Start the queue workers, the folder watcher, and block until interrupted."""
    queue_existing_unprocessed_files()
    queue_existing_quality_retries()

    for _ in range(MAX_WORKERS):
        Thread(target=transcription_worker, daemon=True).start()

    for background in (
        periodic_reconciliation,
        durable_job_dispatcher,
        deliver_pending_transcripts,
        heartbeat_worker,
    ):
        Thread(target=background, daemon=True).start()

    event_handler = AudioFileHandler()

    def start_observer():
        watcher = Observer()
        watcher.schedule(event_handler, FOLDER_TO_WATCH, recursive=True)
        watcher.start()
        return watcher

    observer = start_observer()

    print(f"WATCHDOG ONLINE: Monitoring '{FOLDER_TO_WATCH}' and all subfolders.")
    print("Press Ctrl+C to stop the system.\n")

    try:
        while True:
            time.sleep(1)
            if not observer.is_alive():
                # The macOS file-system watcher can die on its own - a volume
                # disappearing is enough. Rebuild it instead of running blind
                # until somebody restarts the service.
                print("[WATCHDOG] File watcher stopped; restarting it.")
                try:
                    observer.stop()
                    observer.join(timeout=5)
                except Exception:
                    pass
                try:
                    observer = start_observer()
                except Exception as exc:
                    print(f"[WATCHDOG] Could not restart the file watcher: {exc}")
                    time.sleep(10)
    except KeyboardInterrupt:
        print("\nShutting down Watchdog...")
        observer.stop()
    observer.join()


# Importing this module must not start the service; the supervisor runs it with
# `python -m backend.worker`. Without this guard the module blocks forever on
# import, which is why none of its transcription logic could be tested.
if __name__ == "__main__":
    run_service()
