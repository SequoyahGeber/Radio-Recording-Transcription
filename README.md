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
- Indexed FTS5 archive search spans every imported year with exact counts,
  safe phrase/prefix syntax, highlighted context, facets, and relevance sorting.
- Search preferences and named saved searches follow each signed-in operator
  because they are retained server-side rather than in one browser.
- Repetitive or low-confidence transcripts are quarantined for supervisor review.
- Operators use a five-state review workflow, bookmarks, notes, immutable
  change history, and an eight-second undo window.
- Supervisors can correct transcripts and export server-generated CSV files.
- Viewer, operator, supervisor, and administrator clearance profiles are supported.
- The dashboard uses a collapsible top command bar so feed columns use the full window.
- The newest 100 transmissions load first; scrolling upward fetches older pages.
- Selecting a transmission opens a detail drawer with model comparison,
  quality/source metadata, review history, and proper note/correction editors.
- One global audio player supplies seek, skip, speed, volume, mute, and
  previous/next controls without creating an audio element for every card.
- Per-user and supervisor-shared workspaces retain feed visibility/order,
  focus mode, filters, density, and alert visibility in SQLite.
- The `⌘K` command palette and keyboard shortcuts cover routine review,
  bookmark, note, correction, feed, search, and audio actions.
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

Fresh installations use one unified `festival_radio.db` archive and recordings
directory for every year. Existing installations continue to use their current
paths. On first launch after this upgrade, annual `festival_radio_YYYY.db`
archives beside the active database are imported without modifying or deleting
the source files. The importer creates a `pre-multiyear.bak` recovery snapshot
before the first merge and records each source size, modification time, source
count, and imported count so repeated startups are idempotent and auditable.

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

Install the developer-only browser dependency once:

```sh
npm install
```

Then run the complete Python, API, and browser baseline:

```sh
scripts/run_tests.command
```

The Python suite covers multi-year schema/import migration, live-first ordering,
verified copies, garbage-transcript classification, access control, archive
pagination, uncapped streaming export, versioned review history, workspace
visibility, model management, and updater safety. The API suite verifies FTS5
phrases, prefixes, snippets, facets, exact year counts, cursors, reindexing,
preferences, and saved-search authorization. The Playwright suite starts an
isolated HTTPS server, creates 176 disposable transmissions across eight
realistic feeds and three years, and checks authentication, indexed archive
search, filtering, pagination, saved views, overlay controls, the console,
detail editing and undo, the global player, shortcuts, workspaces, full export,
mobile feed navigation, and feed geometry from 320 through 1,920 pixels in the
installed stable Google Chrome.

The same command runs a deterministic 100,000- and 500,000-row FTS5 benchmark.
Its exact-count search must remain below a 200 ms p95 gate at both sizes.
Override the ceiling only for diagnostic comparison with
`RADIO_SEARCH_P95_LIMIT_MS`; do not raise it in routine validation.

Run only the browser regression tests with:

```sh
scripts/run_browser_tests.command
```

Use `PLAYWRIGHT_HEADED=1 scripts/run_browser_tests.command` to watch the test,
or set `PLAYWRIGHT_BROWSER_CHANNEL=chromium` after running
`npx playwright install chromium` on a machine without Google Chrome. The
fixture is generated under the system temporary directory and removed when the
test server exits; it never reads or changes production recordings, accounts,
settings, or databases.

The responsive geometry matrix verifies 320, 390, 640, 700, 820, 1,040, 1,280,
1,440, and 1,920 pixel viewports; 640 CSS pixels also exercises the effective
layout of a 1,280-pixel window at 200% zoom. It asserts that feed rectangles
never overlap, the page itself never scrolls horizontally, and mobile layouts
expose exactly one feed through the feed navigator.

Set `RADIO_SKIP_BROWSER_TESTS=1` only when running the Python/API subset in an
environment that cannot launch a local HTTPS server.
