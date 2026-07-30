# Radio Command Center

A private, local radio-recording transcription system for Apple Silicon Macs.
It synchronizes recordings from a local or mounted network folder, transcribes
them with Whisper, and presents a secure live operations dashboard.

## Current capabilities

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
- The dashboard uses a collapsible top command bar so feed columns use the full window.
- The newest 100 transmissions load first; scrolling upward fetches older pages.
- Operators can inspect browser, server, transcription, and sync events in the dashboard console.
- A native Mac launcher embeds the secure dashboard alongside service and folder controls.
- Administrators can pause or resume transcription without stopping the dashboard or sync service.
- Administrators can check GitHub Releases and install verified app updates in place.
- A process supervisor supplies backoff, clean shutdown, and rotating service logs.

## Install on a Mac

The accelerated path requires Apple Silicon and Python 3.12. The installer
detects Homebrew's linked and versioned Python locations and installs
`python@3.12` with Homebrew when it is missing. Transcription falls back to
`faster-whisper` on the CPU if MLX cannot load.

1. Double-click `scripts/install.command`.
2. Open `dist/Radio Command Center.app`.
3. Choose the recording folder. Mounted SMB shares under `/Volumes` are supported.
4. Open **Radio Command Center**. Services start automatically and the secure
   dashboard loads directly inside the app.
5. On first launch, enter your name and choose an administrator username and
   password. The app signs you in when setup is complete.
6. Add dashboard-only profiles from **Access Profiles** as needed.

The original `Start Radio Command Center.command` remains available as a
terminal-based launcher.

Administrators can expand **App Controls** and choose **Stop Transcription** to
leave the dashboard and recording sync available without running the
transcription engine. The same control changes to **Start Transcription**, and
the selected state persists across app restarts and updates.

## Clearance profiles

- **Viewer:** transcripts, archive search, and bookmarks view; no source audio.
- **Operator:** viewer access plus audio, review state, bookmarks, and notes.
- **Supervisor:** operator access plus suspect transcripts, corrections, and exports.
- **Administrator:** supervisor access plus profile management, program console,
  and native transcription-service controls.

The first-launch setup creates the administrator profile. It is available only
until that first profile has been saved, so setup cannot be repeated to replace
an existing administrator. Administrators can add or update profiles from the
dashboard control bar. At least one active administrator is always required.

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

On macOS, the launcher saves configuration and runtime data under
`~/Library/Application Support/Radio Command Center`. Environment variables
take precedence:

- `RADIO_SOURCE_DIR`
- `RADIO_AUDIO_DIR`
- `RADIO_DATA_DIR`
- `RADIO_SECURITY_DIR`
- `RADIO_HOST`
- `RADIO_PORT`
- `RADIO_RECORDING_YEAR`
- `RADIO_MODEL_SIZE`
- `RADIO_MODEL_DIR`
- `RADIO_RETRY_MAX_DURATION_SECONDS`
- `RADIO_RESCUE_BACKLOG_LIMIT`
- `RADIO_TRANSCRIPTION_ENGINE`
- `RADIO_SYNC_BATCH_SIZE`
- `RADIO_SYNC_VERIFY_SHA256`
- `RADIO_SYNC_MIN_FREE_BYTES`

Runtime data, recordings, databases, downloaded models, logs, settings, and credentials
remain excluded from source control.

## Transcription models

The application and DMG do not bundle Whisper model weights. The first time an
administrator chooses **Start Transcription**, the Medium MLX model downloads
once into `~/Library/Application Support/Radio Command Center/models`. If that
download fails, transcription remains off and the dashboard and sync services
continue running.

Medium handles the normal first pass. Transcripts flagged by decoder confidence,
excessive repetition, or the quality filter can receive a lower-priority second
pass with Large V3. Large V3 downloads only when the first eligible rescue is
needed. The retry text and quality measurements are retained; it replaces the
Medium text only when the retry is usable and clearly better. Blank audio and
recordings longer than three minutes skip the expensive retry by default.

## App updates

Administrators can expand **App Controls** in the Mac app and choose
**Updates**. The app also performs one quiet update check after an
administrator signs in. Update checks use the repository's latest stable
GitHub Release; draft and prerelease builds are never installed.

Before installation, the updater:

- requires a newer semantic version and the expected Apple-silicon DMG name;
- verifies the GitHub asset SHA-256 digest or its companion `.sha256` file;
- asks macOS to verify the DMG and the app's complete code signature;
- confirms the app bundle identifier and release version;
- stops the services cleanly and stages the replacement outside the app bundle;
- snapshots the database, security profiles, and settings for recovery;
- keeps recordings and all other Application Support data in place; and
- retains the immediately previous app so a failed launch can roll back.

Updates replace only `Radio Command Center.app`. The updater refuses to run if
the app and its data directory overlap. If the app is opened directly from a
DMG, drag it to **Applications** before updating.

## Deployment

Radio Command Center uses one administrator Mac as the transcription host. The
packaged DMG provides a drag-and-drop application containing its Python runtime
and transcription dependencies. Model weights download on demand after
installation rather than inflating every DMG. After the one-time administrator
setup, opening the app is a one-click daily launch.

Viewer, operator, and supervisor profiles do not need the transcription
software. They connect to the administrator host's secure dashboard in a web
browser and see no native start, stop, restart, folder, or console controls.
Network deployment requires configuring the administrator host's stable LAN or
VPN address and trusting the generated dashboard CA certificate on each client.

For distribution outside a managed local network, package the administrator
host as a signed and notarized macOS installer and distribute a separate
lightweight dashboard client or browser link to everyone else.

### Publishing an update

1. Set `CFBundleShortVersionString` in `macos/Info.plist` to the release version.
2. Run `scripts/build_dmg.command`.
3. Create a stable GitHub Release tagged `v<version>`.
4. Attach both the generated
   `Radio-Command-Center-<version>-arm64.dmg` and
   `Radio-Command-Center-<version>-arm64.dmg.sha256` files.

GitHub's asset digest is accepted when present, but publishing the checksum
file keeps verification compatible with GitHub responses that omit it. The
DMG filename, bundle version, and release tag must match exactly.

## Operations and recovery

Service logs can be viewed from **Console** by the administrator. The native
app can restart all services or control the transcription worker independently.
Stopping transcription leaves the dashboard and recording sync online.
Transcripts are committed before dashboard delivery, so a web-server outage
does not lose results. The dashboard catches up from its last seen database ID
after reconnecting.

Each feed initially loads the newest archive page. Scrolling one feed upward
loads the next older page while preserving that feed's position; other columns
that are still following live traffic remain pinned to their newest message.

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
