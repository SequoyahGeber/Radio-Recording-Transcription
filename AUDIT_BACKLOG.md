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

### Follow-up: refreshing audio alone leaves its transcript and job stale

Verified separately from sync using an updated local generated WAV: `backend/worker.py`'s `unprocessed_paths`
filters solely by transcript filename, and `backend/jobs.py`'s `enqueue_job` deduplicates on `(job_type, relative_path)`
while retaining a completed job's status. Neither predicate compares recording bytes or a recording version.
The watcher accepts created/moved events; an ordinary modified event has no handler. Even delivering another
created/moved event for the changed path cannot requeue its completed primary job.

A disposable harness used the real database initializer and durable job functions. It created a 3,244-byte WAV,
claimed/completed its primary job with a synthetic version-one transcript, then rewrote the local WAV as 6,444 bytes
without calling sync. It executed the exact AST-extracted `unprocessed_paths` function against that database, avoiding
the worker's inference imports, and called the real `enqueue_job` again:

```json
{"dueJobCount":0,"jobStatusAfterChangedAudioEvent":"completed","oldAudioBytes":3244,"reconciliationCandidates":[],"sameJobId":true,"transcriptStillVersionOne":true,"updatedAudioBytes":6444}
```

This confirms a downstream identity gap even after audio refresh succeeds; it is consolidated here because it belongs
to the same changed-recording lifecycle, rather than counted as another source-sync failure. Existing completed-event
deduplication is intentional and covered by a passing test. Preserve that behavior for unchanged bytes while defining
a new recording version for changed bytes. Preserve existing transcript/review history, visibly mark stale results,
and enqueue work for the new version. The primary persistence path also uses `INSERT OR IGNORE` on unique filename,
so merely resetting a job is insufficient to define correct replacement behavior; that forced-processing path was
reviewed in source but was not executed. Add end-to-end version-change coverage before implementing a fix.

Evidence: rolling-audit workspace `audit/radio-recording-identity.{py,log}`. No inference, file watcher, service or
production archive ran. The transcript contents were deliberately synthetic; this check verifies scheduling and
identity handling, not recognition quality or concurrent in-flight replacement.

### Follow-up: same-size replacement and legacy destination adoption

Two additional generated-WAV cases called the real `sync_once`, with SHA verification explicitly enabled:

- **Previously synchronized path:** after an initial verified copy of a 3,244-byte WAV, rewrite the source with
  different samples, the same byte length and a different settled modification time. Sync skips it, leaves the old
  local audio intact and retains the old recorded mtime. This is another consequence of the same fast-path defect,
  rather than an independent finding.
- **Legacy path without a sync row:** create valid source/local WAVs of the same 3,244-byte length but different
  sample contents. The real adoption path in `destination_is_current` checks length only, appends a sync record with
  the current source size/mtime and a null checksum, and accepts the different local bytes. A subsequent sync also
  leaves those bytes unchanged. This does not require a previously synchronized source to change: the adoption
  predicate itself equates same length with current content.

```json
{"known":{"sameSizeReplacementSkipped":true,"size":3244,"storedMtimeMatchesNewSource":false},"legacy":{"adoptedChecksumIsNull":true,"adoptedSourceIdentity":true,"destinationStillDifferent":true,"size":3244},"shaVerificationEnabled":true}
```

The adoption pass reported `copied=0, already_local=2, unsettled=0, failed=0` including the known stale fixture.
SHA verification currently applies to the copy path, so enabling it does not establish equality for adopted files.
Both paths are consolidated into AUD-001's archive/content identity lifecycle; no additional severity count is added.

**Fix scope clarification:** checking current source metadata in the known-record fast path alone cannot make legacy
adoption trustworthy. Compare content before recording an untracked destination as current, or explicitly preserve
an unverified state with a visible reconciliation policy. Retain existing recordings and reviewed transcripts until
replacement/review handling is defined. Cover equal-length/different-content files as well as growth, and preserve
the prior local copy on verification or refresh failure.

Evidence: rolling-audit workspace `audit/radio-same-size-adoption.{py,log}`. Only owned temporary source/audio/database
files were used and removed afterward. No production recording, source mount, service or model ran. These cases check
steady settled files; in-flight mutation and identical-metadata replacement are addressed separately below.

### Follow-up: in-flight changes and identical-metadata replacement

Three further generated-audio cases complete this bounded identity pass:

1. **Ordinary in-place rewrite with changed mtime:** call the real `copy_verified` on a 1,600,044-byte WAV. Immediately
   after its first 1 MiB read, rewrite the source with different generated samples and a changed mtime. The real
   post-copy identity check raises `source changed while it was being copied`, deletes `.part`, and preserves the
   prior local WAV. This protection worked in the tested interleaving.
2. **In-place rewrite with size and mtime preserved:** repeat that interleaving, restoring the source's original
   nanosecond mtime after the rewrite. With SHA verification enabled, the helper publishes the original first MiB
   followed by the new source's remaining bytes. The resulting WAV is readable but equals neither complete version.
   The transferred stream and staged destination hash match, while the source stat predicate also passes. This
   establishes a conditional source-snapshot limitation, not a claim that normal mtime-changing writes evade detection.
3. **Settled replacement with identical metadata:** after a normal verified sync, rewrite a small source with equal
   length/different generated samples and restore its exact mtime. A second real sync skips it and retains the old local
   bytes. This remains the same AUD-001 recording-identity issue; size/mtime comparison alone would not detect it.

The only copy-path injection was a reader wrapper triggering the real source-file rewrite after a deterministic read.
Read bytes, stat results, hash functions, validation decisions, publication and sync records were not mocked. The helper
was invoked directly for the first two cases because the already-logged sync fast path otherwise skips an existing
destination before refresh reaches it. The third case used `sync_once` normally.

```json
[{"case":"mtime-changed","copyRejected":true,"priorLocalPreserved":true,"publishedMixedVersions":false},{"case":"metadata-preserved","copyRejected":false,"priorLocalPreserved":false,"publishedMixedVersions":true},{"case":"settled-identical-metadata-replacement","sameSize":true,"sameMtimeNs":true,"staleLocalPreserved":true}]
```

**Fix scope clarification:** retain the working changed-mtime rejection and atomic staging behavior. Specify whether
the source contract requires immutable finalized recordings. If concurrent rewrites or metadata-preserving replacement
are supported, establish stronger snapshot/content identity and a retry/reconciliation policy; a hash calculated only
over the copied stream proves transfer consistency, not that those bytes represent one complete source version.
Available file identity/change metadata or a source recheck may help, but their guarantees must be tested on the
actual mount. No universal snapshot guarantee or physical network-filesystem behavior was proved in this audit.

Evidence: rolling-audit workspace `audit/radio-inflight-identity.{py,log}`. All source/destination/database files were
temporary and generated; no service, model, production mount or personal recording was accessed. These are controlled
interleavings rather than stress testing. They add evidence to AUD-001, not duplicate findings or fixes.

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
- Recording-version identity, same-size/legacy adoption and controlled in-flight/identical-metadata mutation were
  checked in the follow-ups above. Real mount behavior, other interleavings and model/API integration remain untested.

### Suggested next repository scope

The next highest-value unaudited active source project is `SequoyahGeber/MIRE-Unreal` (last non-audit source activity
October 6, 2026). Bound the first pass to `MireSaveRepository` / `MireSaveGameSubsystem`: interrupted save publication,
primary/backup recovery ordering, corrupt save rejection and repeated-load inventory conservation. Existing save
recovery/repository/round-trip tests provide starting points. Inspect its project instructions and isolate any fixtures
before execution; do not launch packaged games, touch personal saves, change the active checkout or invoke release CI.

This documentation commit changes only `AUDIT_BACKLOG.md`; no implementation, test, settings, workflow or deployment
change is included.
