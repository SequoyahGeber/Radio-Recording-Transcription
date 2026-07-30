# Phase Two handoff

Date: 2026-07-30

## Delivered

### Transmission detail workflow

- Clicking a card or choosing **Open details** opens a responsive,
  non-destructive detail drawer.
- The drawer presents the complete transcript, recording/source metadata,
  quality explanation, Medium original, Large V3 retry, notes, correction
  editor, bookmark control, and recent mutation history.
- Browser prompts have been removed from note and correction workflows.
- Operator and supervisor controls remain clearance-aware.

### Review states, history, and undo

- Transmissions now support `unreviewed`, `in_review`, `confirmed`,
  `corrected`, and `dismissed` states.
- Review actor, timestamp, resolution, and record version are retained.
- Existing boolean `reviewed` callers remain compatible and map to the new
  workflow.
- Every dashboard review, bookmark, note, and correction mutation offers an
  eight-second undo action.
- Mutations record immutable before/after snapshots in
  `transcript_versions`.
- Version-aware updates return HTTP 409 instead of silently overwriting a
  newer operator change.

### Single global audio player

- Cards have one lightweight **Play recording** entry point and no embedded
  `<audio>` elements.
- The dashboard creates exactly one audio element for the selected recording.
- The player provides play/pause, ±5-second skip, previous/next transmission,
  seekable waveform-style progress, duration, 0.75×–2× speed, volume, mute,
  keyboard control, and a compact mobile layout.
- Playback stops and releases the source when the player closes.

### Keyboard workflow and command palette

- `⌘K` opens a searchable command palette.
- `/` focuses archive search.
- `J`/`K` selects the next/previous visible transmission.
- `Enter`, `Space`, `R`, `B`, `N`, `E`, and `F` operate the selected
  transmission.
- `Shift` + left/right arrow skips audio by five seconds.
- `1`–`9` focuses a configured feed.
- `?` displays the shortcut reference and `Esc` closes the active surface.
- Shortcuts are disabled while typing in form controls or editors.

### Saved workspaces and feed focus

- Operators can save named workspaces in SQLite instead of relying on browser
  storage.
- Workspaces retain visible feeds, feed order, focused feed, board mode,
  search/date/suspect/bookmark filters, density, and alert visibility.
- Supervisors and administrators can publish shared workspaces.
- Operators see their personal workspaces plus shared workspaces.
- Only a workspace owner or administrator can delete it.
- Every feed has a one-click focus control; the same operation is accessible
  through the command palette and numeric shortcuts.

## API and schema

- `GET /api/transcripts/{id}` returns detail metadata and recent change
  history.
- `PATCH /api/transcripts/{id}` accepts review workflow fields and an optional
  expected `version`.
- `GET /api/workspaces` returns personal and shared workspaces visible to the
  authenticated profile.
- `POST /api/workspaces` creates or updates a named workspace.
- `DELETE /api/workspaces/{id}` enforces owner/administrator deletion.
- Database initialization migrates existing reviewed records to `confirmed`
  without replacing the original transcript or audio.

## Validation

Run:

```sh
scripts/run_tests.command
```

Phase Two adds two API integration scenarios and three browser scenarios. The
expanded suites verify:

- Review transitions, history snapshots, correction state, and stale-version
  conflict handling.
- Personal/shared workspace visibility and authorization.
- Drawer editing and undo.
- Exactly one global audio element and no card audio elements.
- Player transport, seek, speed, and volume controls.
- Keyboard selection, command palette, feed focus, and workspace saving.
- The complete Phase One responsive breakpoint and export matrix.

The browser-control visual pass also verified the desktop and mobile drawer,
the desktop and mobile player, full-width toolbar containment, zero document
horizontal overflow, and a single visible feed at the mobile breakpoint.
All visual validation used disposable fixture data; the installed services,
live database, recordings, accounts, and settings were not touched.
