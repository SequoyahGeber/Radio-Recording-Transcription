# Radio Command Center

A private, local radio-recording transcription system for Apple Silicon Macs.
It synchronizes recordings from a local or mounted network folder, transcribes
them with Whisper, and presents a secure live operations dashboard.

## What changed in version 2

- Live recordings are synchronized before historical backlog batches.
- Source files must be stable across scans and copied files can be SHA-256 verified.
- Dashboard notifications use a durable SQLite outbox and retry after outages.
- Browsers recover missed transmissions after reconnecting.
- Worker, sync, mount, queue, delivery, and disk status appear in system health.
- Search and date filters query the complete database, not just loaded cards.
- Repetitive or low-confidence transcripts are quarantined for supervisor review.
- Operators can review, bookmark, and annotate transmissions.
- Supervisors can correct transcripts and export server-generated CSV files.
- Viewer, operator, supervisor, and administrator clearance profiles are supported.
- A native Mac launcher provides launch, restart, stop, dashboard, and folder controls.
- A process supervisor supplies backoff, clean shutdown, and rotating service logs.

## Install on a Mac

The accelerated path requires Apple Silicon and Python 3.12. The installer
detects Homebrew's linked and versioned Python locations and installs
`python@3.12` with Homebrew when it is missing. Transcription falls back to
`faster-whisper` on the CPU if MLX cannot load.

1. Double-click `scripts/install.command`.
2. Open `dist/Radio Command Center.app`.
3. Choose the recording folder. Mounted SMB shares under `/Volumes` are supported.
4. Select **Launch**, then **Open Dashboard**.
5. Use the initial administrator login saved to
   `Desktop/Radio Dashboard Login.txt`.
6. Trust `Desktop/Radio Dashboard CA Certificate.crt` on authorized devices.

The original `Start Radio Command Center.command` remains available as a
terminal-based launcher.

## Clearance profiles

- **Viewer:** transcripts, archive search, and bookmarks view; no source audio.
- **Operator:** viewer access plus audio, review state, bookmarks, and notes.
- **Supervisor:** operator access plus suspect transcripts, corrections, and exports.
- **Administrator:** supervisor access plus profile management.

The first generated `operator` profile is an administrator. Administrators can
add or update profiles from the dashboard sidebar. At least one active
administrator is always required.

## Transcript quality

MLX decoding disables previous-text conditioning and applies repetition,
log-probability, no-speech, and hallucination-silence thresholds. A second
quality gate checks dominant words, repeating phrases, vocabulary diversity,
and decoder metrics.

Suspicious transcripts are retained with their original text and explanation,
but hidden from routine operations. Supervisors can enable **Show Suspect
Transcripts**, listen to the source audio, and correct valid messages. Short
urgent repetitions such as “go go go” are intentionally not auto-hidden.

Tune the gate only after reviewing real station audio. It is designed to prefer
false-positive review over permanent data loss.

## Configuration

The launcher saves configuration in `data/settings.json`. Environment variables
take precedence:

- `RADIO_SOURCE_DIR`
- `RADIO_AUDIO_DIR`
- `RADIO_DATA_DIR`
- `RADIO_SECURITY_DIR`
- `RADIO_HOST`
- `RADIO_PORT`
- `RADIO_RECORDING_YEAR`
- `RADIO_MODEL_SIZE`
- `RADIO_TRANSCRIPTION_ENGINE`
- `RADIO_SYNC_BATCH_SIZE`
- `RADIO_SYNC_VERIFY_SHA256`
- `RADIO_SYNC_MIN_FREE_BYTES`

Runtime data, recordings, databases, models, logs, settings, and credentials
remain excluded from source control.

## Operations and recovery

Service logs are stored in `data/logs`. The native app can restart all services.
Transcripts are committed before dashboard delivery, so a web-server outage
does not lose results. The dashboard catches up from its last seen database ID
after reconnecting.

The sync worker stops copying before the configured free-space reserve is
crossed and reports the condition as degraded. Recordings are never deleted
automatically; define an explicit archival policy for the deployment.

## Tests

Run:

```sh
scripts/run_tests.command
```

The suite covers schema migration, live-first ordering, verified copies, and
garbage-transcript classification. Python, shell, JavaScript, and Swift syntax
are also checked during release validation.
