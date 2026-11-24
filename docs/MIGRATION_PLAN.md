# Migration and milestone plan

**Status:** historical migration is **implemented** for the live runtime; future
milestones are **planned**. Physical FiberHome equivalence is **not evaluated**.

This document supersedes stale test counts and proposals in
`docs/AUDIT_PHASE1.md`, `docs/AUDIT_PHASE2.md`, and
`docs/PHASE3_1_HARDENING_REPORT.md`. Those files remain historical records.

## What already changed

The original application used vendor-specific logic in `gateway.RouterClient`
and a direct legacy view. The current live path is:

```text
Flask routes → discovery → ConnectorManager → declarative connector profile
→ ConnectorExecutor → trusted auth handler + HTTP requests
→ extraction/transforms/validation → UniversalModel → dashboard
```

`legacy_view.py` is retained only for comparison. `app.py` does not import or
call `RouterClient` or `build_view()`.

Phase 1 restored the previous dashboard presentation and kept the simple login
experience. The Speedtest reset correction later made fresh page display
page-scoped without changing backend state, the worker, or the API.

## Current state by area

| Area | Status |
|---|---|
| Declarative catalog, schema, loader, manager/executor integration | **Implemented** |
| Trusted auth handler registry | **Implemented** |
| FiberHome dynamic AES handler and explicit logout | **Implemented**, physical equivalence **not evaluated** |
| `UniversalModel`, seven domains, 61 metric IDs, availability states | **Implemented** |
| Discovery APIs, login defaults, unsupported message | **Implemented** |
| Manufacturer selector validation/state | **Implemented**; discovery constrained by equipment catalog |
| Profile Builder create/validate/inspect/test/map/register | **Implemented**; fixture-only dry run, no live mode |
| Huawei/ZTE profiles | **Missing** |
| Full JSON Schema Draft 2020-12 validator | **Missing**; in-house subset implemented |
| Physical old-vs-new normalized output comparison | **Not evaluated** |

## Compatibility invariants

These must remain true through every later milestone:

- No dashboard redesign. Keep restored layout, icons, PWA assets, and the
  page-scoped Speedtest reset.
- No connector removal. Keep `fiberhome.hg6145f1` enabled and its AES flow
  working.
- No changes to `/api/data` public shape, metric IDs, availability vocabulary,
  or frontend bindings.
- No change to Speedtest worker/API semantics or VoIP `tel:` behavior.
- No credentials, secrets, profile IDs, compatibility details, or evidence in
  the public dashboard payload.
- No executable code in profiles and no new runtime dependency.
- No database, no duplicate functionality, and no split of modules merely to
  match an older document.
- Structural validation remains separate from physical compatibility.
- Legacy clients that omit `manufacturer_id` keep working.

## M1 — Contract and documentation freeze

**Status: this milestone.** No runtime behavior changes.

Created from current source and tests:

- `docs/ARCHITECTURE.md`
- `docs/DATA_MODEL.md`
- `docs/CONNECTOR_PROFILE_SPEC.md`
- `docs/AUTHENTICATION_SPEC.md`
- `docs/DISCOVERY_SPEC.md`
- `docs/PROFILE_BUILDER_SPEC.md`
- `docs/MIGRATION_PLAN.md`
- `tests/test_phase2_contracts.py`
- focused `README.md` updates

M1 freezes API shapes, metric/domain/availability contracts, auth identifiers,
the FiberHome AES vector, discovery/login-defaults responses, VoIP and
Speedtest behavior, and manufacturer compatibility.

## M2 — Profile foundation gap closure

**Status: implemented, independently reviewed, and deployed.**

The audit's original "profile foundation" milestone is already largely
implemented. Rebuilding the schema/loader would duplicate live functionality.
The actual gap is metadata and constrained selection:

- add optional explicit `manufacturer_id`, `model`, and `firmware` metadata to
  connector profiles;
- link `config/equipment.json` manufacturers to connector IDs;
- extend manager validation without breaking existing catalogs;
- pass the selector into discovery so only compatible manufacturer profiles are
  candidates;
- preserve all legacy requests and the public API shapes.

Expected files include `config/connector.schema.json`,
`config/connectors.json`, `config/equipment.json`, `connector_manager.py`,
`app.py`, and focused tests/docs. No dashboard rewrite.

## M3 — Profile Builder and manual model registration

**Status: implemented; see `docs/M3_IMPLEMENTATION_REPORT.md`.**

M3 extends the existing CLI rather than replacing it. It adds declarative
endpoint configuration, field-path inspection, canonical metric mapping,
fixture-only `test`, trusted-handler discovery, manufacturer/model listings, and
explicit `register --approve` for the connector plus its equipment association.
There is no live/network mode, no real credential use, and no new executable
profile field.

`profile_safety.py` is a vendor-neutral runtime hardening boundary shared by the
builder, manager, executor, and API gateway validation. It pins requests to a
bare configured gateway host, validates methods/schemes/paths, disables redirect
following, rejects 3xx responses, and bounds response size. This does not make
model registration require Python changes; only a documented trusted auth
handler or parser would do that.

Manual fallback remains manufacturer-constrained: registration must name an
existing manufacturer or supply `--manufacturer-name`, must agree with optional
profile metadata, rejects duplicate model/manufacturer pairs, rejects dangling
references, and never falls back to all manufacturers. Omitted selectors in
legacy discovery requests remain unchanged. Mocked tests never claim hardware
compatibility.

## M4 — Physical-equivalence gate

**Status: not evaluated; required before claiming compatibility.**

Compare legacy and executor normalized output on the physical HG6145F1 for at
least identity/firmware/system/optical/WAN fields, connected devices and count,
VoIP number/registration, and logout followed by fresh login. Redact phone
numbers, public IPs, serials, and credentials in any report.

Unit tests with mocked HTTP are not sufficient evidence. Until the physical
comparison passes, the profile status remains `observed` and compatibility
remains `NOT_EVALUATED`.

## Verification gate for each milestone

Before any milestone is considered complete:

1. Run the complete unittest suite with the project virtualenv.
2. Run `python -m tools.connector_builder validate`.
3. Run available documentation/link consistency checks.
4. Confirm protected runtime files are unchanged when the milestone is
   documentation-only.
5. Do not deploy, restart `gwmon`, commit, or push until explicitly approved.
