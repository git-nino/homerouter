# Phase 3.1 — HAR connector hardening report

Baseline: commit `1dc5bc6` (`feat: add HAR draft capture, validation, and explicit import workflow`).
This phase keeps the single `config/connector.schema.json` contract and does not add a second connector schema.

## Changed files

- `tools/windows/Capture-ONT.ps1` — connector-id policy, conservative extraction contract,
  aligned endpoint/metric IDs, private-IP and generic-secret redaction, fail-closed output
  checks, HTML/XML evidence notes, Native .NET XML field-name reporting.
- `tools/windows/README.md` — documents ID policy, `-FirmwareFamily`, conservative
  extraction, HTML/XML limits, and redaction/fail-closed behavior.
- `tools/har_analyzer.py` — Python reference implementation aligned to the same ID policy,
  conservative extraction rules, redaction, fail-closed writes, and HTML/XML evidence notes.
- `tests/test_har_capture.py` — stable-ID, firmware opt-in, redaction hardening,
  fail-closed, and unsupported-content tests.
- `tests/test_windows_capture.py` — contract assertions and the recorded Windows
  13-point checklist evidence.
- `tests/fixtures/har/windows_draft_shape.json` — regenerated cross-implementation draft shape.
- `tests/fixtures/har/windows_checklist_13.json` — recorded results of the mandatory
  real Windows execution.
- `docs/PHASE3_1_HARDENING_REPORT.md` — this report.

## Connector ID policy

Permanent IDs are derived only from observed device identity:

- `manufacturer.model`, e.g. `fiberhome.hg6145f1`
- `manufacturer.model.fw-<family>` only when a reviewer explicitly supplies a firmware
  family because variants need separate profiles
- `observed.<model>-<digest>` when identity is unknown; explicitly temporary

LAN IP, Windows IP, serial number, MAC address, hostname and personal identifiers are
never used in the ID. Tests prove that the same synthetic model captured from
`192.168.1.1` and `192.168.2.1` produces the same connector ID. The Windows run repeated
this check with two LAN hosts and recorded it in the 13-point checklist.

## PowerShell / Python contract comparison

The following are now equivalent in both generators:

- connector ID policy and opt-in firmware family suffix
- endpoint IDs (`<method>_<path>` with `_02`, `_03`, ... for repeated shapes)
- scheme, body format, timeout, auth flag, query/header/body structure
- auth observations (`none`, `http_basic`, `http_digest`, `token`, `form`, `session`,
  `fiberhome_aes`, proprietary/manual-handler classification)
- dependencies, identification candidates, extraction candidates, metric mapping candidates
- evidence, `status: draft`, `enabled: false`, `evidence.verified: false`, and
  `requires_manual_auth_handler`
- conservative extraction rules: observed JSON paths only, no `transforms`, no
  `value_type`, no `validation` rules inferred from names
- redaction placeholders and fail-closed output verification

For the synthetic fixture, PowerShell and Python now produce the same 11 extraction rule
IDs and the same endpoint IDs. `tools/har_analyzer.py` remains the Python reference/test
implementation; Windows capture/generation remains PowerShell-native.

## Redaction results

Both tools redact passwords/usernames, PPPoE credentials, cookies and session IDs, CSRF
tokens and nonces, phone numbers, GPON SN/LoID, serials, MACs, public IPs, personal device
names/hostnames, private LAN IPs, and generic secret-looking values under unspecific
field names. Outputs are serialized only after a fail-closed scan for registered sensitive
literals and MAC/IPv4/token patterns; the synthetic leak test verifies that no file is
written when a secret remains.

The real Windows run found no synthetic secrets in any generated draft or evidence file.

## HTML / XML status

JSON is the only content format used for extraction. HTML and XML are classified by MIME
type but are not mapped into extraction rules:

- XML: built-in .NET/PowerShell `XmlReader` and Python `xml.etree.ElementTree` are used
  only to report observed element names as `fields_not_analyzed`.
- HTML: no safe built-in extraction method is assumed; the body is reported as unanalyzed.

Both emit explicit evidence that the endpoint needs manual extraction work. No fake metric
mapping is invented.

## Mandatory Windows execution

Executed on real Windows with the bundled Windows PowerShell 5.1:

- Windows: `Microsoft Windows NT 10.0.19045.0` (Windows 10 build 19045)
- Windows PowerShell: `5.1.19041.6456`, Desktop edition
- `Capture-ONT.bat` and `Capture-ONT.ps1` both ran with exit code 0.
- HAR 1.1, HAR 1.2, and a second-LAN-host HAR all generated valid disabled drafts.
- The same model captured from two LAN IPs produced the same ID.
- XML and HTML fixtures produced explicit `unsupported_content` evidence.
- No synthetic secrets remained in generated outputs.

All 13 checklist items passed; see `tests/fixtures/har/windows_checklist_13.json`.

## Verification

- Full Python suite: `123 tests OK`.
- Catalog validation: `Structural validity: PASS`, 1 connector, 0 errors, 0 warnings,
  `Device compatibility: NOT_EVALUATED`.
- Fixture draft validation: `PASS`, 0 errors, 2 expected draft/disabled warnings.
- Windows-generated draft validation: `PASS`, 0 errors, 2 expected draft/disabled warnings.

## FiberHome regression

The existing catalog profile `fiberhome.hg6145f1` remains unchanged. Its status remains
`observed`, it stays enabled, and no compatibility claim was added. Existing universal
metric IDs, dashboard payload behavior, connector selection logic and auth handlers were
not modified. No manufacturer-specific code was added to `app.py` or `static/app.js`.

## Remaining manual work / limits

Hardware compatibility remains `NOT_EVALUATED`. A physical ONT is still required to:

1. confirm the observed endpoints and identification values on the real device;
2. compare normalized output with the legacy path before claiming compatibility;
3. replace placeholder success/session fields and confirm units or transforms;
4. manually approve and validate any draft before enabling it.

Drafts are never enabled or imported automatically. `--approve` remains an explicit
review action, and the imported profile remains `status: draft`, `enabled: false`.
