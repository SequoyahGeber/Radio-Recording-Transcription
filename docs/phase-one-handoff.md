# Phase One handoff

Date: 2026-07-30

## Delivered

### Responsive board

- Five feeds fit above 1,800 px.
- Four feeds fit from 1,500 through 1,799 px.
- Three feeds fit from 1,050 through 1,499 px.
- Two feeds fit from 700 through 1,049 px.
- Below 700 px, one feed is shown at a time through an accessible previous,
  next, and select-based feed navigator.
- Additional desktop and tablet feeds remain available through deliberate
  horizontal board scrolling.
- Feed tracks and child widths now share one responsive source of truth, so
  cards cannot overflow into adjacent tracks.

### Compact command bar

- The closed desktop command bar is no more than 96 px high; visual validation
  measured 79 px at 1,440 × 900.
- The settings deck is an overlay and no longer reduces the dashboard height.
- Tablet and mobile layouts progressively collapse low-priority labels and
  metrics while preserving accessible names.
- Mobile Console, Export, and Sign out icon buttons have explicit accessible
  names.

### Mobile feed navigation

- The selected feed persists locally.
- Switching feeds clears that feed's unread count.
- Hidden and search-filtered feeds are excluded from the active navigator.
- Incoming messages on off-screen feeds increment their unread badge.

### Simplified cards

- Each card has one audio entry point.
- Review, bookmark, note, and correction commands live in a More menu.
- Reviewed, bookmarked, suspect, corrected, and Large V3 states remain visible
  as compact badges.
- The Follow control now reports `Following live` or `Paused · N new`.

### Complete export

- `/api/export.csv` streams matching rows in 500-row batches.
- The previous 2,000-row limit is removed.
- `/api/export/count` supplies the exact matching-row count before download.
- Confirmed exports are pinned to that count response's database high-water
  mark, so newly arriving radio traffic cannot alter the download.
- The CSV response includes `X-Radio-Export-Count`.
- The dashboard confirms the exact count with the operator before downloading.
- Integration coverage exports and verifies 2,105 matching records.

## Validation

Run:

```sh
scripts/run_tests.command
```

The Phase One browser suite contains six scenarios covering:

- Authenticated realistic-board rendering.
- Archive search and overlay controls.
- Collision-free geometry at 320, 390, 640, 700, 820, 1,040, 1,280, 1,440,
  and 1,920 pixels; the 640-pixel case covers a 1,280-pixel window at 200%
  effective zoom.
- Single-feed mobile navigation and accessible icon actions.
- One audio control and the secondary-action menu.
- Exact full-stream export counts.

The disposable fixture and HTTPS test service do not use the live database,
recording source, accounts, settings, or running services.
