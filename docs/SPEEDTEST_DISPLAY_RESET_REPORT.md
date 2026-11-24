# Speedtest display reset correction

Date: 2026-11-24
Scope: dashboard-only correction after Phase 1. No Phase 2 work performed.

## Root cause

The completed Speedtest result was retained in the backend process memory
(`app.py` global `state["speedtest"]`). It is intentionally kept there for
diagnostics and API compatibility, and is exposed by `/api/speedtest/status`
and injected into every `/api/data` response as `speed_test.*` metrics.

On a fresh page load with an existing router session, `init()` called
`enterDashboard()` → `refreshData()` → `render()` → `renderSpeed()`, which read
those metrics and replayed the last result into the visible Speedtest card.
The card itself starts in its default state in `templates/index.html`.

Investigation ruled out the other candidates: no `localStorage`,
`sessionStorage`, `IndexedDB`, `document.cookie` or Flask session usage exists;
the service worker only precaches static assets and explicitly bypasses
`/api/`. No backend operation was misbehaving — the frontend was re-rendering
stale API state.

## Fix

Page-scoped display state only:

- `static/app.js` adds `speedResultFromThisPage` (default `false`) and
  `resetSpeedDisplay()`, which clears the displayed download/upload/ping/jitter/
  server/error fields, hides the result/progress/error panels, shows the idle
  prompt, restores the `idle` pill, and clears client-only polling timers.
- `renderSpeed()` ignores `/api/data` Speedtest metrics until this page has
  actually started a test.
- The flag is set only after a successful `POST /api/speedtest`.
- `resetSpeedDisplay()` runs at the start of `init()` and `enterDashboard()`
  (fresh load, dashboard reopen) and from `leaveDashboard()`.
- No fetch, no `/api/` call, no router/auth state and no backend file is
  touched by the reset. The worker, engine, `/api/speedtest` API and diagnostic
  state are unchanged.

## Files changed

- `static/app.js` — the correction.
- `tests/speedtest_reset_browser.test.js` — new Node fake-DOM lifecycle harness.
- `tests/test_speedtest_reset.py` — new Python regression tests.

Backups: `/root/backups/homerouter/speedtest-reset-pre-20261124-062237.tar.gz`
and `/root/backups/homerouter/speedtest-reset-post-20261124-062500.tar.gz`
(both verified).

## Tests

- Full suite: **148 passed** (143 from Phase 1 + 5 new).
- New checks cover: stale backend result ignored on fresh load and on later
  refreshes, completed local test displays its values, result stays visible
  while the page remains open, fresh reload resets to defaults, dashboard
  reopen resets, no browser-storage/stale-status polling fallback, API/worker
  contract unchanged, and completed results still available through
  `/api/speedtest/status` and `/api/data`.

## Deployment / live verification

Only a static asset changed, so `gwmon` was **not restarted** (PID and
`ActiveEnterTimestamp` unchanged; service remained `active`). The served
`/static/app.js` was fetched from `localhost:7000` and the browser-lifecycle
harness was run against that served copy: **PASS**. All relevant live HTTP
routes returned 200 (dashboard, updated JS/CSS, APIs, manifest, service worker)
and the journal showed no errors.
