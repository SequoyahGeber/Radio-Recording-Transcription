import hashlib
import os
import shutil
import time
from datetime import datetime

from backend.config import AUDIO_DIR, RECORDING_SOURCE_DIR, RECORDING_YEAR
from backend.database import connect, initialize_database, update_heartbeat


POLL_SECONDS = max(5, int(os.environ.get("RADIO_SYNC_POLL_SECONDS", "15")))
SETTLE_SECONDS = max(2, int(os.environ.get("RADIO_SYNC_SETTLE_SECONDS", "10")))
LIVE_WINDOW_SECONDS = max(
    60, int(os.environ.get("RADIO_LIVE_WINDOW_MINUTES", "30")) * 60
)
BACKLOG_BATCH_SIZE = max(1, int(os.environ.get("RADIO_SYNC_BATCH_SIZE", "250")))
VERIFY_SHA256 = os.environ.get("RADIO_SYNC_VERIFY_SHA256", "1") != "0"
MIN_FREE_BYTES = max(
    1_000_000_000,
    int(os.environ.get("RADIO_SYNC_MIN_FREE_BYTES", str(10 * 1024**3))),
)
observations = {}


def recorded_at(filename, source_stat):
    try:
        return datetime.strptime(filename[:19], "%Y-%m-%d-%H-%M-%S")
    except (TypeError, ValueError):
        return datetime.fromtimestamp(source_stat.st_mtime)


def candidate_files():
    prefix = f"{RECORDING_YEAR}-"
    now = datetime.now()
    candidates = []
    for root, _, files in os.walk(RECORDING_SOURCE_DIR):
        for filename in files:
            if not filename.lower().endswith(".mp3") or not filename.startswith(prefix):
                continue
            source_path = os.path.join(root, filename)
            try:
                source_stat = os.stat(source_path)
            except OSError:
                continue
            relative_path = os.path.relpath(source_path, RECORDING_SOURCE_DIR)
            recording_time = recorded_at(filename, source_stat)
            is_live = (now - recording_time).total_seconds() <= LIVE_WINDOW_SECONDS
            candidates.append(
                (
                    0 if is_live else 1,
                    recording_time.timestamp(),
                    relative_path,
                    source_path,
                    source_stat,
                )
            )
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return candidates


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def source_is_stable(source_path, source_stat):
    signature = (source_stat.st_size, source_stat.st_mtime_ns)
    previous = observations.get(source_path)
    observations[source_path] = (signature, time.monotonic())
    if source_stat.st_size == 0:
        return False
    if source_stat.st_mtime > time.time() - SETTLE_SECONDS:
        return False
    if previous is None or previous[0] != signature:
        return False
    return time.monotonic() - previous[1] >= min(SETTLE_SECONDS, POLL_SECONDS)


def destination_is_current(relative_path, source_stat, destination_path):
    try:
        if os.path.getsize(destination_path) != source_stat.st_size:
            return False
    except OSError:
        return False
    with connect(read_only=True) as connection:
        row = connection.execute(
            """
            SELECT source_size, source_mtime_ns
            FROM synced_files
            WHERE relative_path = ?
            """,
            (relative_path,),
        ).fetchone()
    if row is None:
        # Adopt files from older installations without recopying the entire archive.
        record_sync(relative_path, source_stat, None)
        return True
    return (
        row["source_size"] == source_stat.st_size
        and row["source_mtime_ns"] == source_stat.st_mtime_ns
    )


def record_sync(relative_path, source_stat, checksum):
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO synced_files(
                relative_path, source_size, source_mtime_ns, sha256, synced_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(relative_path) DO UPDATE SET
                source_size = excluded.source_size,
                source_mtime_ns = excluded.source_mtime_ns,
                sha256 = excluded.sha256,
                synced_at = excluded.synced_at
            """,
            (
                relative_path,
                source_stat.st_size,
                source_stat.st_mtime_ns,
                checksum,
                datetime.now().isoformat(),
            ),
        )
        connection.commit()


def copy_verified(relative_path, source_path, source_stat, destination_path):
    os.makedirs(os.path.dirname(destination_path), exist_ok=True)
    temporary_path = f"{destination_path}.part"
    shutil.copy2(source_path, temporary_path)
    after_copy_stat = os.stat(source_path)
    if (
        after_copy_stat.st_size != source_stat.st_size
        or after_copy_stat.st_mtime_ns != source_stat.st_mtime_ns
    ):
        raise OSError("source changed while it was being copied")
    if os.path.getsize(temporary_path) != source_stat.st_size:
        raise OSError("copied file size does not match source")
    checksum = None
    if VERIFY_SHA256:
        source_checksum = file_sha256(source_path)
        destination_checksum = file_sha256(temporary_path)
        if source_checksum != destination_checksum:
            raise OSError("copied file checksum does not match source")
        checksum = source_checksum
    os.replace(temporary_path, destination_path)
    record_sync(relative_path, source_stat, checksum)


def sync_once():
    if not os.path.isdir(RECORDING_SOURCE_DIR):
        update_heartbeat(
            "sync",
            "waiting",
            {"source_mounted": False, "source_directory": RECORDING_SOURCE_DIR},
        )
        print(f"[SYNC] Waiting for network mount: {RECORDING_SOURCE_DIR}")
        return

    copied = 0
    skipped = 0
    unsettled = 0
    failed = 0
    candidates = candidate_files()

    for tier, _, relative_path, source_path, source_stat in candidates:
        destination_path = os.path.join(AUDIO_DIR, relative_path)
        if destination_is_current(relative_path, source_stat, destination_path):
            skipped += 1
            continue
        if not source_is_stable(source_path, source_stat):
            unsettled += 1
            continue
        if shutil.disk_usage(AUDIO_DIR).free < MIN_FREE_BYTES:
            update_heartbeat(
                "sync",
                "paused",
                {
                    "reason": "low_disk_space",
                    "source_mounted": True,
                    "free_bytes": shutil.disk_usage(AUDIO_DIR).free,
                },
            )
            print("[SYNC] Paused: local free space is below the configured safety reserve.")
            break
        try:
            copy_verified(relative_path, source_path, source_stat, destination_path)
            copied += 1
            queue_name = "LIVE" if tier == 0 else "BACKLOG"
            print(f"[SYNC:{queue_name}] Published verified recording: {relative_path}")
        except (OSError, shutil.Error) as exc:
            failed += 1
            print(f"[SYNC] Copy failed for {relative_path}: {exc}")
            if failed >= 10:
                break
        if tier == 1 and copied >= BACKLOG_BATCH_SIZE:
            # Rescan now so a newly arrived live recording never waits for the whole archive.
            break

    free_bytes = shutil.disk_usage(AUDIO_DIR).free
    update_heartbeat(
        "sync",
        "online",
        {
            "source_mounted": True,
            "source_directory": RECORDING_SOURCE_DIR,
            "source_files": len(candidates),
            "copied_this_pass": copied,
            "already_local": skipped,
            "unsettled": unsettled,
            "failed": failed,
            "free_bytes": free_bytes,
        },
    )
    print(
        f"[SYNC] Pass complete: source={len(candidates)}, copied={copied}, "
        f"already_local={skipped}, unsettled={unsettled}, failed={failed}"
    )


if __name__ == "__main__":
    os.makedirs(AUDIO_DIR, exist_ok=True)
    initialize_database()
    print(
        f"[SYNC] Live-first reconciliation from '{RECORDING_SOURCE_DIR}' "
        f"to '{AUDIO_DIR}'."
    )
    while True:
        try:
            sync_once()
        except Exception as exc:
            update_heartbeat("sync", "error", {"error": str(exc)[:500]})
            print(f"[SYNC] Pass failed: {exc}")
        time.sleep(POLL_SECONDS)
