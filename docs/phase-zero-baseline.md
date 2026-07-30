# Phase Zero baseline

Date: 2026-07-30

## Code checkpoint

- Baseline commit: `b744639` (`v1.2.3`, Fix reconnect archive high-water mark)
- Branch at audit: `main`
- Remote state at audit: `main` matched `origin/main`
- Working tree at audit: clean
- The v1.2 Medium-first and selective Large V3 rescue work described as
  uncommitted in the original upgrade plan had already been released in
  `v1.2.0`; follow-up updater and reconnect fixes were released through
  `v1.2.3`.

This commit is the Phase Zero product checkpoint. The browser-harness changes
that follow are test infrastructure for the next implementation phases.

## Reproducible test baseline

Run:

```sh
npm install
scripts/run_tests.command
```

The current baseline contains:

- 35 Python unit tests.
- 9 FastAPI integration tests.
- 3 Playwright browser scenarios.

The browser harness uses an isolated temporary application root, a generated
test administrator, locally generated TLS material, 176 silent test recordings,
and 176 realistic transcripts distributed across eight feeds. The fixture also
includes suspect transcripts, reviews, bookmarks, notes, corrections, Large V3
retry metadata, service heartbeats, and console entries.

The test server is HTTPS-only and uses the same FastAPI application, static
assets, authentication, SQLite schema, and WebSocket path as the product. By
default Playwright drives the installed stable Google Chrome. Production data
and the live service are not used.

## Phase One defect captured

The narrow-layout feed-overlap assertion is intentionally annotated as an
expected failure. It verifies the known conflict between the dashboard grid
track width and the narrow-breakpoint `.channel-column` minimum width. The
annotation makes the complete Phase Zero suite green while ensuring that a
future responsive fix produces an unexpected pass until the annotation is
removed.

Resolved on 2026-07-30: Phase One replaced the conflicting width rules and
converted this assertion into a passing eight-breakpoint geometry matrix.

## Phase Zero completion gate

- The repository baseline is identified and recoverable by tag and commit.
- The Python/API baseline passes.
- The realistic fixture can be recreated without production data.
- The browser harness passes against the real application shell.
- Browser failures retain a Playwright trace and screenshot.
- The known responsive defect has an executable regression assertion ready for
  Phase One.
