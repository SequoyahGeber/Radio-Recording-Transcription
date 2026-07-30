# Phase Three handoff

Date: 2026-07-30

## Delivered

### Indexed multi-year search

- Archive queries use an external-content SQLite FTS5 index instead of wildcard
  `LIKE` scans.
- Operators can use individual terms, exact quoted phrases, `*` suffix
  prefixes, and `AND`, `OR`, or `NOT`; input is sanitized before it reaches
  SQLite's query parser.
- Results include an exact count, safely highlighted transcript context,
  relevance or recency ordering, elapsed search time, and an opaque
  query-bound pagination cursor.
- Search covers transcript text, notes, and filenames. Transcript and note
  edits remain synchronized through database triggers.

### Archive filters and saved views

- The dedicated search surface filters by channel, year, date, time, review
  state, traffic status, reviewer, bookmark state, transcription model, and
  page size.
- Available channel, year, model, and reviewer facets come from the complete
  archive with exact counts.
- Per-user search preferences are stored in SQLite and restored on sign-in.
- Named saved searches retain the query, filters, and sort order server-side;
  only their owner can list or delete them.
- Existing viewer restrictions remain in force: suspect traffic is not exposed
  by a crafted filter request.

### Safe multi-year migration

- Fresh installations use one unified recordings directory and
  `festival_radio.db` archive rather than a year-specific database.
- Existing installations continue from their current annual database path, so
  configured audio locations do not break during the upgrade.
- Other `festival_radio_YYYY.db` files in the database directory are attached
  read-only and imported into the active archive.
- Before the first historical merge, SQLite creates a
  `pre-multiyear.bak` recovery snapshot.
- Every source database is tracked by path, size, modification time, source
  count, imported count, and completion time. Unchanged sources are not
  imported twice and source files are never renamed or deleted.
- Recording year and channel metadata are backfilled from existing timestamps
  and filenames. The sync scanner now accepts supported recordings from any
  year as well as generic filenames with a filesystem-time fallback.

## API and schema

- `GET /api/search` returns indexed results, snippets, exact counts, and the
  next opaque cursor.
- `GET /api/archive/facets` returns complete archive filter values and counts.
- `GET /api/archive/years` reports exact per-year totals and annual-import
  verification data.
- `GET` and `PUT /api/preferences` retain allowlisted per-user search settings.
- `GET`, `POST`, and `DELETE /api/saved-searches` manage owner-scoped saved
  searches.
- `transcripts.recording_year` and `transcripts.channel` are indexed metadata.
- `transcripts_fts`, its synchronization triggers, `user_preferences`,
  `saved_searches`, `archive_imports`, and `schema_metadata` are created
  idempotently.

## Validation

Run:

```sh
scripts/run_tests.command
```

Phase Three adds migration, sync, API, browser, and performance coverage for:

- Read-only annual database import, recovery backup creation, year/channel
  backfill, FTS rebuild, and repeat-startup idempotency.
- Older, current, and generic recording filename discovery.
- Phrase and prefix search, safe snippets, relevance/recency sorting, facets,
  exact counts, invalid cursors, and edit-triggered reindexing.
- Per-user preferences and saved-search creation, listing, application, and
  deletion.
- A three-year, 176-transmission browser fixture with dedicated search results,
  filters, pagination, highlighting, and saved views.
- Exact-count FTS5 searches at 100,000 and 500,000 rows. The required p95 is
  below 200 ms at both sizes.

The browser-control visual pass verified desktop search containment,
highlight readability, exact-count hierarchy, saved-search dialog layout, and
zero document horizontal overflow. The automated geometry matrix retains mobile
and zoom coverage from 320 through 1,920 CSS pixels. All fixture databases,
credentials, recordings, and benchmark archives are disposable; production
data and installed services are not touched.
