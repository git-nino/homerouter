# Phase 1 integration report

Date: 2026-11-24
Repository: `/root/projects/homerouter` (`main`, HEAD `9567d24d85d4c0cc7c73143957c70d5ca103c577`)
Service: `gwmon.service` → `/root/projects/homerouter/app.py` on port 7000

## References used

- `/home/aaaa/projects/homerouter` — original Phase 1 work (dashboard/icon/catalog changes and phase-1 tests), built on the older RouterClient architecture.
- `/home/aaaa/projects/homerouter-integration` — partial Phase 1 integration on top of the newer ConnectorManager/ConnectorExecutor architecture; used for the additive app/template/JS/CSS changes.
- `/home/aaaa/audit_homerouter` — audit snapshot; confirmed to match the canonical pre-change application.

No whole application files were copied from the older checkout. All changes were adapted to the current universal architecture.

## Backup

`/root/backups/homerouter/homerouter-pre-phase1-20261124-061009.tar.gz`
SHA-256: `9f1a0b11ecd77618da1f272856c22742ea1c8c273194873010fbdc2e35c7078c`

Verified with `sha256sum -c`, extracted, and key files compared byte-for-byte before changes. Checksum re-verified immediately before the service restart.

## Changes integrated

- Dashboard: Speedtest button moved from the header into the Speedtest card, monochrome Lucide SVG icons, dark theme and layout unchanged.
- Manufacturer selector: dynamic `<select>` before Gateway, `config/equipment.json` catalog, `GET /api/manufacturers`, optional `manufacturer_id`/`manufacturer` in `/api/connect`, exposed in `/api/session`, cleared on disconnect/session expiry, unknown IDs rejected, missing/empty catalog degrades gracefully. No automatic model discovery.
- PWA: HomeRouter SVG master plus 192/512/maskable PNGs, manifest updated with all three icons and purposes, service worker cache bumped `gwmon-v7` → `gwmon-v8` with the new icon added to the precache. Favicon, dashboard-icons attribution, router SVG and activation/fetch behavior preserved.

## Tests

- Baseline before changes: `python -m unittest discover -s tests` → **123 passed**.
- `python -m tools.connector_builder validate` → structural validity PASS, 0 errors/warnings.
- After changes: **143 passed** (123 original + 20 new `tests/test_phase1_integration.py` checks).
- The only existing test edit updated the expected service-worker cache version from `gwmon-v7` to `gwmon-v8` and added a maskable-icon assertion. No test was deleted or weakened.
- New coverage includes dashboard loading, single in-card Speedtest button, selector ordering, catalog loading/endpoint/degradation, manufacturer ID propagation, legacy connect, disconnect cleanup, discovery/auth handlers, UniversalModel `/api/data`, VoIP `tel:` behavior, speedtest routes/polling, PWA assets, `gwmon-v8`, and Python/JavaScript syntax (`node --check`).

## Deployment and live verification

- `systemctl restart gwmon`; `systemctl is-active gwmon` → `active`; service PID confirmed running the canonical `app.py`.
- `journalctl -u gwmon -n 80 --no-pager` → clean startup on port 7000, no tracebacks.
- Live HTTP checks against `http://localhost:7000`: **35/35 passed**, covering updated HTML/JS/CSS, manufacturer catalog, single in-card Speedtest button, selector position, all PWA icons and manifest entries (HTTP 200), `gwmon-v8` service worker, existing discovery/login-defaults/session/gateway/speedtest-status/connect/disconnect routes, `/api/data` auth guard, and preserved VoIP markup/JS.
