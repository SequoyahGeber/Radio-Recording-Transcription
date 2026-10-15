# Audit backlog

Rolling audits verify and log findings here; **do not implement fixes during audit work**. Keep findings open until
resumed project work implements and verifies them. Never reproduce these cases with a personal or live radio archive.

## AUD-001 — A previously synchronized recording is never refreshed after its source grows

- **Status:** OPEN — verified with generated WAV files and an isolated database; no fix applied.
- **Severity:** High / P1 (new source audio can be silently absent from the local archive and transcription input).
- **Affected revision:** `a397bcb95993887d472f8e4a726e6f4e64dfeed6` (Radio Command Center 1.4.4).
- **Paths:** `sync.py:355–378` (`sync_once` fast path); related source-identity checks are in
  `destination_is_current` and `copy_verified` in the same file.
- **Condition:** a source file grows or is replaced under the same relative filename after its first successful,
  settled copy. The source file itself is preserved; this finding concerns the stale local recording.

### Cause and trigger

If a relative path has a `synced_files` record, `sync_once` compares the local destination size with the size stored
by the previous copy. If those still match, it increments `already_local` and continues immediately. It does not
first compare the current source size or modification time, even if the candidate already supplied that source stat.
The later source-identity/stability checks are unreachable for this ordinary case. Consequently, later sync passes
keep accepting the old local bytes even when the source has acquired additional audio.

### Disposable reproduction and evidence

A separate harness set `RADIO_DATA_DIR`, `RADIO_DB_PATH`, `RADIO_SOURCE_DIR` and `RADIO_AUDIO_DIR` to a new temporary
root before importing any backend code. It called the real database initializer and `sync_once`, without patching
the sync logic, running a service or invoking transcription:

1. Write a valid mono 16-bit 16 kHz WAV containing 1,600 generated samples. Give it an old enough modification time
   to satisfy the normal settle window, then run sync. Confirm destination bytes equal source bytes.
2. Rewrite that same source path as a valid 3,200-sample WAV, again settled, then run sync a second time.
3. Read the actual destination bytes and `synced_files` row.

Observed result:

```json
{"destinationAfterSecondSyncBytes":3244,"destinationStillOld":true,"firstCopyBytes":3244,"sourceAfterGrowthBytes":6444,"sourceMatchesDestination":false,"storedIdentityBytes":3244}
```

The second real sync reported `copied=0, already_local=1, unsettled=0, failed=0`. Thus this is a silent skip, not a
reported copy error or unsettled-file delay. All source/destination/database files were generated and removed inside
the owned temporary root. Local harness and receipt: rolling-audit workspace `audit/radio-source-growth.py` and
`audit/radio-source-growth.log`.

### Suggested next-session fix

Check the current source identity before accepting the fast path, then retain the stable-source checks, verified
staging and atomic publication when the source changed. Review whether same-size replacements and legacy adoption
also require verification. Define what happens to an existing transcript/review when its recording changes: do not
silently discard reviewed work or assume that replacing the audio alone updates its transcript. Add a two-pass
regression covering both growth and replacement, plus failed refresh preserving the prior readable local copy.

### Executed coverage and verification limits

- **12 existing tests passed** across `DurableJobTests`, `SyncPriorityTests` and `MultiYearArchiveMigrationTests`.
  They cover interrupted-worker requeue, leaving this worker's active job alone, durable retry/completion, completed
  event deduplication, sync ordering, unwritable destination/low-disk behavior, verified initial copy, and idempotent
  multi-year database import with recoverable source archives. They do not cover the two-pass growth case above.
- A separate actual backup/restore harness passed four checks: an injected interruption while writing the staged
  restore preserved the current database; the failed request was cleared; a subsequent successful restore recovered
  the backup contents; the prior database was retained with its post-backup contents. Only the staged copy operation
  was fault-injected; restore/verification/recovery logic was unchanged.
- Python `3.14.7` was used for these stdlib-only checks; the README's accelerated deployment targets Python 3.12.
  No dependencies or transcription models were installed or downloaded. No MLX/Whisper inference, source mount,
  launcher, installed app, production recording or live service was accessed.
- Evidence: `audit/radio-core-tests.log` and `audit/radio-backup-recovery.{py,log}` in the rolling-audit workspace.
  This is targeted verification, not a full API/browser/model suite or a real power-cut/device acceptance test.
- Next useful scope: recording-version changes and their transcript/job identity handling, then same-size/legacy
  destination adoption. Those are review targets, not additional confirmed findings in this entry.

This documentation commit changes only `AUDIT_BACKLOG.md`; no implementation, test, settings, workflow or deployment
change is included.
