# Phase Six handoff

Phase Six is complete as of 2026-08-01.

## Delivered

- SQLite-backed primary and Large V3 rescue jobs with durable attempt counts,
  scheduled retries, errors, restart recovery, and live-primary priority.
- Background Medium and Large V3 model readiness with disk checks, progress,
  ETA, pause, cancel, resume, retry, and cache-integrity validation.
- Separate immutable Medium and Large V3 results, automatic selection reasons,
  supervisor override, version history, and conflict protection.
- Per-channel spelling, callsign, venue, and radio-term glossaries passed to
  both MLX and faster-whisper as an initial prompt.
- Dashboard model readiness, primary/rescue queue counts, glossary editor,
  comparison explanations, and manual selection controls.

## Gate evidence

- 26 unit tests passed.
- 18 API integration tests passed.
- 13 Playwright browser scenarios passed.
- Real Apple MLX/Metal validation passed on an existing 6.14-second recording.
  The Medium model returned `Max, you on radio?`, matching the reviewed archive
  reference.
- The production Large V3 model remains optional until a rescue is needed; its
  background manager and recovery controls are tested without forcing an
  unrequested multi-gigabyte download.

## Operational notes

- Live primary jobs always outrank historical primary and rescue jobs.
- A rescue job defers itself if any live primary work is queued or running.
- Jobs that were running during a crash return to the queued state at database
  initialization.
- Partial model downloads remain cached so Retry or Resume can continue them.
