import hashlib
import os
import shutil
import time
from datetime import datetime

from backend.config import (
    AUDIO_DIR,
    RECORDING_SOURCE_DIR,
    bool_setting,
    number_setting,
    text_setting,
)
from backend.database import connect, initialize_database, update_heartbeat


AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a")
_announced = {}


def announce(message, key, repeat_seconds=900):
    """Print a status line, but not the same one over and over.

    Sync runs every few seconds forever. Repeating an unchanged status on each
    pass produced megabytes of identical log lines a day.
    """
    now = time.monotonic()
    previous_message, previous_time = _announced.get(key, (None, 0.0))
    if previous_message == message and now - previous_time < repeat_seconds:
        return
    _announced[key] = (message, now)
    print(message)


# Every tunable is read at the start of each pass, so changing the recording
# folder or a timing value in the settings screen takes effect within seconds
# and never needs a service restart.
def source_directory():
    return os.path.abspath(
        os.path.expanduser(
            text_setting("source_dir", "RADIO_SOURCE_DIR", RECORDING_SOURCE_DIR)
        )
    )


def poll_seconds():
    return number_setting(
        "sync_poll_seconds", "RADIO_SYNC_POLL_SECONDS", 15, minimum=5, maximum=600
    )


def settle_seconds():
    return number_setting(
        "sync_settle_seconds", "RADIO_SYNC_SETTLE_SECONDS", 10, minimum=2, maximum=300
    )


def live_window_seconds():
    return 60 * number_setting(
        "live_window_minutes",
        "RADIO_LIVE_WINDOW_MINUTES",
        30,
        minimum=1,
        maximum=1440,
    )


def backlog_batch_size():
    return number_setting(
        "sync_batch_size", "RADIO_SYNC_BATCH_SIZE", 250, minimum=1, maximum=5000
    )


def verify_sha256():
    return bool_setting("sync_verify_sha256", "RADIO_SYNC_VERIFY_SHA256", True)


def minimum_free_bytes():
    return number_setting(
        "minimum_free_bytes",
        "RADIO_SYNC_MIN_FREE_BYTES",
        10 * 1024**3,
        minimum=1024**3,
        maximum=4000 * 1024**3,
    )


def recorded_at(filename, source_stat=None):
    try:
        return datetime.strptime(filename[:19], "%Y-%m-%d-%H-%M-%S")
    except (TypeError, ValueError):
        if source_stat is None:
            return None
        return datetime.fromtimestamp(source_stat.st_mtime)


def candidate_files(source_dir=None, live_window=None):
    source_dir = source_dir or source_directory()
    live_window = live_window or live_window_seconds()
    now = datetime.now()
    candidates = []
    for root, _, files in os.walk(source_dir):
        for filename in files:
            if not filename.lower().endswith(AUDIO_EXTENSIONS):
                continue
            source_path = os.path.join(root, filename)
            relative_path = os.path.relpath(source_path, source_dir)
            source_stat = None
            recording_time = recorded_at(filename)
            if recording_time is None:
                try:
                    source_stat = os.stat(source_path)
                except OSError:
                    continue
                recording_time = recorded_at(filename, source_stat)
            is_live = (now - recording_time).total_seconds() <= live_window
            candidates.append(
                (
                    0 if is_live else 1,
                    recording_time.timestamp(),
                    relative_path,
                    source_path,
                    source_stat,
                )
            )
    # Within each priority tier, newest recordings are copied first so a large
    # historical archive cannot keep current-day radio traffic waiting.
    candidates.sort(key=lambda item: (item[0], -item[1], item[2]))
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


def source_is_stable(source_path, source_stat, settle=None):
    del source_path
    settle = settle_seconds() if settle is None else settle
    if source_stat.st_size == 0:
        return False
    # Once a source has not changed for the settle window it can be copied
    # immediately. A second full archive scan is unnecessary because
    # copy_verified checks the source identity again after reading it.
    return source_stat.st_mtime <= time.time() - settle


def load_sync_records():
    with connect(read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT relative_path, source_size, source_mtime_ns
            FROM synced_files
            """
        ).fetchall()
    return {
        row["relative_path"]: (row["source_size"], row["source_mtime_ns"])
        for row in rows
    }


def destination_is_current(
    relative_path,
    source_stat,
    destination_path,
    sync_records=None,
    adopted_records=None,
):
    try:
        if os.path.getsize(destination_path) != source_stat.st_size:
            return False
    except OSError:
        return False

    if sync_records is None:
        with connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT source_size, source_mtime_ns
                FROM synced_files
                WHERE relative_path = ?
                """,
                (relative_path,),
            ).fetchone()
        identity = (
            (row["source_size"], row["source_mtime_ns"])
            if row is not None
            else None
        )
    else:
        identity = sync_records.get(relative_path)

    if identity is None:
        # Adopt files from older installations without recopying the entire archive.
        if adopted_records is None:
            record_sync(relative_path, source_stat, None)
        else:
            adopted_records.append(
                (
                    relative_path,
                    source_stat.st_size,
                    source_stat.st_mtime_ns,
                    None,
                    datetime.now().isoformat(),
                )
            )
        return True
    return (
        identity[0] == source_stat.st_size
        and identity[1] == source_stat.st_mtime_ns
    )


def record_sync_rows(rows):
    if not rows:
        return
    with connect() as connection:
        connection.executemany(
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
            rows,
        )
        connection.commit()


def record_sync(relative_path, source_stat, checksum):
    record_sync_rows(
        [
            (
                relative_path,
                source_stat.st_size,
                source_stat.st_mtime_ns,
                checksum,
                datetime.now().isoformat(),
            )
        ]
    )


def copy_verified(
    relative_path, source_path, source_stat, destination_path, verify=None
):
    verify = verify_sha256() if verify is None else verify
    os.makedirs(os.path.dirname(destination_path), exist_ok=True)
    temporary_path = f"{destination_path}.part"
    try:
        # copy2() also tries to reproduce permissions, flags, and extended
        # attributes from the network share. Sandboxed macOS app children may
        # read the audio but are not permitted to apply that metadata locally.
        # Only the recording bytes are required; integrity is verified below.
        # Hash during the copy so the network file is read only once.
        source_digest = hashlib.sha256() if verify else None
        with open(source_path, "rb") as source_handle, open(
            temporary_path, "wb"
        ) as destination_handle:
            while True:
                chunk = source_handle.read(1024 * 1024)
                if not chunk:
                    break
                destination_handle.write(chunk)
                if source_digest is not None:
                    source_digest.update(chunk)
        after_copy_stat = os.stat(source_path)
        if (
            after_copy_stat.st_size != source_stat.st_size
            or after_copy_stat.st_mtime_ns != source_stat.st_mtime_ns
        ):
            raise OSError("source changed while it was being copied")
        if os.path.getsize(temporary_path) != source_stat.st_size:
            raise OSError("copied file size does not match source")
        checksum = None
        if verify:
            source_checksum = source_digest.hexdigest()
            destination_checksum = file_sha256(temporary_path)
            if source_checksum != destination_checksum:
                raise OSError("copied file checksum does not match source")
            checksum = source_checksum
        os.replace(temporary_path, destination_path)
    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise
    record_sync(relative_path, source_stat, checksum)


def destination_write_error():
    probe_path = os.path.join(AUDIO_DIR, f".sync-write-test-{os.getpid()}")
    try:
        os.makedirs(AUDIO_DIR, exist_ok=True)
        with open(probe_path, "wb") as handle:
            handle.write(b"ok")
        os.unlink(probe_path)
        return None
    except OSError as exc:
        try:
            os.unlink(probe_path)
        except OSError:
            pass
        return str(exc)


def sync_once():
    source_dir = source_directory()
    if not os.path.isdir(source_dir):
        update_heartbeat(
            "sync",
            "waiting",
            {"source_mounted": False, "source_directory": source_dir},
        )
        announce(f"[SYNC] Waiting for network mount: {source_dir}", key="waiting")
        return

    write_error = destination_write_error()
    if write_error:
        details = {
            "reason": "destination_not_writable",
            "source_mounted": True,
            "source_directory": source_dir,
            "destination_directory": AUDIO_DIR,
            "error": write_error[:500],
        }
        update_heartbeat("sync", "error", details)
        announce(
            f"[SYNC] Destination is not writable: {AUDIO_DIR}: {write_error}",
            key="destination",
        )
        return

    copied = 0
    skipped = 0
    unsettled = 0
    failed = 0
    settle = settle_seconds()
    verify = verify_sha256()
    reserve = minimum_free_bytes()
    batch_size = backlog_batch_size()
    candidates = candidate_files(source_dir)
    sync_records = load_sync_records()
    adopted_records = []
    free_bytes = shutil.disk_usage(AUDIO_DIR).free
    paused_reason = None
    last_error = None

    for tier, _, relative_path, source_path, source_stat in candidates:
        destination_path = os.path.join(AUDIO_DIR, relative_path)
        recorded_identity = sync_records.get(relative_path)
        if recorded_identity is not None:
            try:
                if os.path.getsize(destination_path) == recorded_identity[0]:
                    skipped += 1
                    continue
            except OSError:
                pass
        if source_stat is None:
            try:
                source_stat = os.stat(source_path)
            except OSError:
                unsettled += 1
                continue
        if destination_is_current(
            relative_path,
            source_stat,
            destination_path,
            sync_records,
            adopted_records,
        ):
            skipped += 1
            continue
        if not source_is_stable(source_path, source_stat, settle):
            unsettled += 1
            continue
        if free_bytes < reserve:
            paused_reason = "low_disk_space"
            announce(
                "[SYNC] Paused: local free space is below the configured safety reserve.",
                key="low_disk",
            )
            break
        try:
            copy_verified(
                relative_path, source_path, source_stat, destination_path, verify
            )
            copied += 1
            free_bytes = max(0, free_bytes - source_stat.st_size)
            queue_name = "LIVE" if tier == 0 else "BACKLOG"
            print(f"[SYNC:{queue_name}] Published verified recording: {relative_path}")
        except (OSError, shutil.Error) as exc:
            failed += 1
            last_error = str(exc)[:500]
            print(f"[SYNC] Copy failed for {relative_path}: {exc}")
            if failed >= 10:
                break
        if tier == 1 and copied >= batch_size:
            # Rescan now so a newly arrived live recording never waits for the whole archive.
            break

    record_sync_rows(adopted_records)
    free_bytes = shutil.disk_usage(AUDIO_DIR).free
    heartbeat_status = "paused" if paused_reason else ("error" if failed else "online")
    heartbeat_reason = paused_reason or ("copy_failed" if failed else None)
    update_heartbeat(
        "sync",
        heartbeat_status,
        {
            "reason": heartbeat_reason,
            "source_mounted": True,
            "source_directory": source_dir,
            "destination_directory": AUDIO_DIR,
            "source_files": len(candidates),
            "copied_this_pass": copied,
            "already_local": skipped,
            "unsettled": unsettled,
            "failed": failed,
            "error": last_error,
            "free_bytes": free_bytes,
        },
    )
    summary = (
        f"[SYNC] Pass complete: source={len(candidates)}, copied={copied}, "
        f"already_local={skipped}, unsettled={unsettled}, failed={failed}"
    )
    if copied or failed or paused_reason:
        print(summary)
    else:
        # A quiet pass every few seconds must not fill the log for months on end.
        announce(summary, key="idle", repeat_seconds=3600)


if __name__ == "__main__":
    os.makedirs(AUDIO_DIR, exist_ok=True)
    initialize_database()
    print(
        f"[SYNC] Live-first reconciliation from '{source_directory()}' "
        f"to '{AUDIO_DIR}'."
    )
    while True:
        try:
            sync_once()
        except Exception as exc:
            update_heartbeat("sync", "error", {"error": str(exc)[:500]})
            print(f"[SYNC] Pass failed: {exc}")
        time.sleep(poll_seconds())
