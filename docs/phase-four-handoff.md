# Phase Four handoff

Date: 2026-07-30

## Delivered

### Server-owned alert workflow

- Browser-local emergency keywords have been replaced by rules stored in
  SQLite and shared by every operator.
- Rules support informational, caution, urgent, and critical severity; whole
  word/phrase or prefix matching; exclusions; channel and time-of-day scope;
  minimum transcript quality; cooldown; sound name; acknowledgement
  requirement; escalation interval; active/paused state; and record version.
- Five conservative default rules separate critical medical/security language,
  missing-person reports, routine medical response, and broad safety
  assistance. A word such as “help” is caution-level rather than automatically
  critical.
- Supervisors can create, edit, pause, and test a rule against sample text
  without creating a real alert.
- Each rule creates no more than one event for a transmission. Configurable
  cooldown also suppresses repeated rule/term events across closely spaced
  transmissions.

### Alert inbox and response history

- A permanent top-bar alert button shows the number of events requiring
  attention.
- The dedicated inbox filters by state, severity, and assignee.
- Alert cards explain exactly which rule and text matched and retain the
  source channel, recording time, transcript, severity, status, assignment,
  acknowledgement actor/time, resolution actor/time, note, and version.
- Operators can acknowledge, assign, resolve, or reopen an alert.
- Supervisors can classify a false positive for later rule tuning.
- Every response action is appended to `alert_acknowledgements`; current state
  remains in `alert_events`.
- Notification permission remains controlled by the browser, while enabled
  state, minimum severity, sound preference, and quiet-hours fields are stored
  per user on the server.

### Durable real-time collaboration

- `events` is the single durable stream for transcript creation/update, alert
  creation/update, alert-rule changes, and access-profile changes.
- Each event has an increasing ID, type, resource identity, actor, payload,
  creation time, and optional deduplication key.
- WebSocket clients connect with `after_event_id`. The server replays every
  missed event in bounded batches up to a captured high-water mark before
  switching to live delivery.
- Initial archive responses provide an event high-water mark so a fresh
  dashboard does not replay historical mutations unnecessarily.
- Replayed alerts are marked as historical and never produce a duplicate
  desktop notification.
- Transcript edits now require the client’s known version. Missing versions
  return HTTP 428; stale versions return HTTP 409 with the current record.
- Alert and rule edits use the same optimistic-concurrency contract.
- Connected-user status counts unique authenticated usernames rather than
  tabs. Supervisors can inspect the signed-in user list; routine operators see
  only aggregate presence.
- Profile changes are broadcast immediately. A user whose own clearance
  changes refreshes securely to apply the new permissions.

## API and schema

- `GET /api/events` returns durable events after an event cursor.
- `GET /api/presence` reports unique-user and connection counts; supervisors
  also receive active user details.
- `GET /api/alert-assignees` returns active operator-capable profiles.
- `GET`, `POST`, and `PUT /api/alert-rules` list and manage versioned rules.
- `POST /api/alert-rules/{id}/test` previews matching without mutation.
- `GET /api/alerts` returns the filtered inbox, exact count, attention count,
  and an older-page cursor.
- `GET /api/alerts/summary` supplies badge counts by status and severity.
- `PATCH /api/alerts/{id}` performs versioned response and assignment actions.
- `GET` and `PUT /api/notification-preferences` retain allowlisted per-user
  alert settings.
- New tables: `events`, `alert_rules`, `alert_events`,
  `alert_acknowledgements`, `alert_deliveries`, and
  `user_notification_preferences`.

## Validation

Run:

```sh
scripts/run_tests.command
```

Phase Four coverage includes:

- Rule boundary, phrase, prefix, exclusion, channel, quality, and overnight
  time-window matching.
- Default-rule migration and idempotent schema initialization.
- Internal-token protection, transcript-delivery deduplication, alert creation,
  cooldown/uniqueness, exact inbox state, preferences, and event persistence.
- Missing and stale transcript versions, stale alert versions, and the returned
  current record.
- Two independently authenticated WebSocket clients receiving the same
  transcript mutation event.
- Unique presence for two users, supervisor presence detail, disconnect
  cleanup, and REST replay from the prior event high-water mark.
- Browser acknowledgement, rule preview, alert badge/filters, desktop layout,
  mobile containment, and the complete Phase One–Three regression matrix.

The browser-control visual pass verified the 1,280-pixel alert inbox, two-pane
rule editor, 390-pixel response workflow, severity/status hierarchy, response
actions, and zero document horizontal overflow. All alerts, users, events,
recordings, and preferences used by validation are disposable fixture data;
production services and the installed database are not modified.
