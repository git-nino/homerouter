# Windows HAR capture analysis (`Capture-ONT`)

Offline, Windows-native tooling that turns a HAR export from the router's web UI into a
**sanitized, disabled Homerrouter connector draft** plus supporting evidence.

It uses only built-in Windows PowerShell cmdlets and .NET Framework classes available in
Windows PowerShell 5.1+. It does **not** require administrator rights, WSL, Docker,
Python, Node, or any external package for capture/analysis/generation.

> The HAR import step comes first. This tool does not browse the router, probe the
> network, scan a subnet, scan ports, guess credentials, or assume that the default
> gateway is the ONT UI host.

## 1. Workflow

1. On the Windows machine, open **Microsoft Edge** or **Google Chrome**.
2. Browse to the ONT/router UI address (for example `http://192.168.1.1/`). **Sign in
   manually** in the browser. The script never handles your password.
3. Open Developer Tools with <kbd>F12</kbd>, select the **Network** tab, and enable
   **Preserve log**.
4. Reload the UI if needed, then click through the pages you want captured:
   status/dashboard, WAN/Internet, optical/PON/GPON, system/version/firmware,
   VoIP/telephony, and connected devices/LAN clients.
5. In the Network tab, choose **Export HAR (with sensitive data / response content)**.
   Chrome/Edge label this differently by version; pick the option that includes
   response bodies. Save it locally, for example as `capture.har`.
6. Keep that raw HAR file private. It can contain passwords, cookies, tokens, PPPoE
   credentials, phone numbers, public IP addresses, serial numbers, and personal device
   names. **The script never copies the raw HAR into its outputs.**
7. Run the script from this directory. From PowerShell:

   ```powershell
   .\Capture-ONT.bat -HarPath .\capture.har -Gateway 192.168.1.1 -OutputDirectory .\output
   ```

   You can also double-click `Capture-ONT.bat`, but it is a console tool and normally
   should be run from `cmd.exe` or PowerShell so you can see warnings and results.

## 2. Arguments

| Argument | Required | Description |
| --- | --- | --- |
| `-HarPath` | yes | Path to the HAR JSON file exported by Edge/Chrome. |
| `-Gateway` | no | Manual target host override. Accepts an IP, DNS name, `host:port`, or URL. Used **only** as a strict URL filter. |
| `-Hostname` | no | Separate UI host override (for example `router.local` or ` ont.local `). Overrides the host portion of `-Gateway` for filtering; the port from `-Gateway` is still honored. |
| `-FirmwareFamily` | no | Optional reviewer-supplied firmware family used **only** in the connector id when firmware variants require separate profiles. |
| `-OutputDirectory` | no | Output root. Defaults to `.\output` in the current working directory. |

The script auto-detects the active IPv4 default route with `Get-NetRoute` and the
interface/IP/prefix with `Get-NetIPConfiguration` (fallback: `Get-NetIPAddress`) and
prints:

- interface name
- Windows IPv4 address
- subnet prefix length
- default gateway

If detection fails, supply `-Gateway` or `-Hostname` explicitly.

### Gateway override caution

The IPv4 default gateway is **only a network hint**. It is not proof that the gateway is
the ONT management UI, and the UI may be on a different IP, hostname, or port. Verify the
address in the browser bar. If it differs, rerun with `-Gateway` and/or `-Hostname`.

Only HAR requests whose absolute URL host (and, when supplied, port) **exactly equals**
the selected target are analyzed. Every other HAR entry is ignored. There is no subnet
scan, port scan, brute force, or credential guessing.

## 3. What is produced

For a deterministic connector id such as `fiberhome.hg6145f1` (or
`observed.capture-<digest>` when identity is unknown), exactly these outputs are written
under `-OutputDirectory`:

```text
output/fiberhome.hg6145f1.draft.json
output/fiberhome.hg6145f1.evidence/
    capture-summary.json
    redaction-report.json
    endpoints/
        get_....json
        post_....json
        ...
```

- **`<connector-id>.draft.json`** — Homerrouter draft connector:
  `status: "draft"`, `enabled: false`, `evidence.verified: false`,
  `identification.confidence: "unknown"` unless a non-sensitive observed identifier was
  used, and `capabilities` derived only from observed fields (`unclassified` if none).
  It contains only observed endpoints and field names; unresolved items are explicit
  `<unobserved-...>` placeholders for manual review.
- **`capture-summary.json`** — selected-host totals, network detection (redacted),
  chronological call sequence, auth observations, capabilities, limitations.
- **`redaction-report.json`** — redaction counts by placeholder type, sensitive field
  names observed (names only), ignored/malformed entry counts, output list.
- **`endpoints/*.json`** — one sanitized representative response structure per derived
  endpoint, with sanitized request query/header/body structure and observed field paths.

Existing output files with the same deterministic names are overwritten. The raw HAR is
never placed in the evidence directory and is never uploaded or transmitted by this
tool.

## 4. Supported input and classification

- **HAR versions:** 1.1 and 1.2 JSON (`log.entries[]`). Unknown versions produce a
  warning but are still processed if the entry shape is valid. Legacy HAR files with
  `log.pages` only and no `entries` are rejected with a clear error.
- **Response content:** JSON response bodies are parsed for structure and field paths.
  `encoding: "base64"` response text is decoded in memory. Entries with no response
  content are reported and noted for manual recapture; their endpoints are still listed.
  HTML/XML/text bodies are classified by MIME type but not parsed into JSON paths.
- **Auth classification from observed traffic:**
  - no auth headers/cookies → `none`
  - `Authorization: Basic ...` → `http_basic`
  - `Authorization: Digest ...` → `http_digest`
  - `Authorization: Bearer ...` or token-like headers/fields → `token`
  - HTML form login body to a login-like path → `form`
  - `Cookie` / `Set-Cookie` / session-like fields → `session`
  - nonce/challenge/encrypted login fields → `requires_manual_auth_handler: true` with
    the observed sequence documented in `auth_observations`; no algorithm or executable
    code is invented
  - the exact FiberHome `get_acs_random` / `do_login` / `loginpd` / `acsRandom` /
    `sessionid` pattern may be classified as `fiberhome_aes`
  
  Only existing supported handler types are emitted: `none`, `http_basic`,
  `http_digest`, `form`, `session`, `token`, `fiberhome_aes`.

### Connector id policy

The permanent connector id is derived only from non-sensitive device identity:

- `manufacturer.model` when both are observed (for example `fiberhome.hg6145f1`)
- `manufacturer.model.fw-<family>` only when a reviewer supplies `-FirmwareFamily`
  because firmware variants require separate profiles
- `observed.<model>-<digest>` (explicitly temporary) when identity is unknown

The selected LAN IP, Windows IP, serial number, MAC address, hostname and other personal
identifiers are **never** used in the id. The same model captured from two different LAN
IPs therefore produces the same connector id.

Endpoints are derived from **observed method + path** (GET/POST only), with a numeric
suffix for repeated shapes. The selected host is used only as a strict HAR filter.
Query parameters, body field names, and a small safe header allowlist are preserved as
structure. Dynamic values seen to come from an earlier response are expressed with
`{"source":"state","field":"<observed-field-name>"}` and recorded in `dependencies`.

Metric candidates use only existing universal metric IDs (for example
`device.manufacturer`, `device.model`, `device.firmware`, `device.hardware`,
`wan.status`, `wan.public_ip`, `optical.rx_power_dbm`, `optical.tx_power_dbm`,
`optical.temperature_c`, `voip.phone_number`, `voip.registration_state`,
`system.uptime_s`, `devices.count`). The PowerShell tool and
`tools/har_analyzer.py` use the same conservative mapping table. Extraction rules carry
only observed JSON paths (`method: "json_path"`) with no inferred units, `transforms`,
`value_type` or invented `validation` rules. A field name alone is never treated as proof
of a unit, encoding or transform; those belong in `manual_review`. No `xpath` or `css`
rules are emitted.

HTML and XML response bodies are **not mapped into extraction rules**. The runtime has no
safe HTML extraction method and no XML extraction method, so the evidence records name
the endpoint and any observed XML element names as `fields_not_analyzed`, with a note that
manual extraction work is required. No fake mapping is created.

## 5. Mandatory sanitization

The tool is deliberately aggressive. It never writes the following into a draft or
evidence file:

- passwords, login secrets, PPPoE credentials, usernames/account names
- `Authorization`, `Cookie`, `Set-Cookie`, CSRF/XSRF, nonce, API-key, token and session
  headers/values
- session IDs and challenge/seed values
- phone numbers, public **and private/LAN** IPv4 addresses, serial numbers, GPON
  SN/LoID values
- MAC addresses, personal hostnames/device names and generic secret-looking tokens
  hidden under unspecific field names

Field names and nesting are preserved; values become placeholders such as
`<redacted-password>`, `<redacted-session>`, `<redacted-token>`, `<redacted-nonce>`,
`<redacted-phone>`, `<redacted-ip>`, `<redacted-public-ip>`, `<redacted-serial>`,
`<redacted-mac>`, `<redacted-hostname>`, and `<redacted-credential>`. Non-sensitive
observed identifiers (model/manufacturer/firmware/hardware) may remain as values when
they pass the safety checks. Before any file is written, the serialized JSON is
re-checked against every registered sensitive literal plus MAC/IPv4/long-token patterns;
if any remain, that file is refused (fail closed).

> Raw HARs may contain credentials and live session secrets. Store them outside shared
> folders, do not attach them to tickets, and delete them when the draft has been
> reviewed. The generated draft is disabled and must be manually reviewed before it is
> trusted.

## 6. After generation (optional, uses the repo's Python tooling)

Capture and generation do **not** need Python. The repository's Python builder is only
for later validation and import into the catalog:

```bash
python -m tools.connector_builder validate-draft output/fiberhome.hg6145f1.draft.json
python -m tools.connector_builder import-draft output/fiberhome.hg6145f1.draft.json --approve
```

Run these from the repository root after reviewing the draft, `manual_review`, and all
evidence files. `--approve` is the explicit act that allows the draft to be appended to
`config/connectors.json`; the imported connector is still written as `status: "draft"`
and `enabled: false`. Enabling it is a separate manual decision after device
verification. The Windows tool never enables or approves anything.

## 7. Security notes

- Runs as a normal user; no `Run as administrator` is required or used.
- No network requests are made by the tool. All analysis is offline.
- No external packages are downloaded or invoked for capture/analysis/generation.
- Error messages are written generically and never include HAR content, so a malformed
  or secret-bearing response body cannot leak into the console log.
- The generated connector is `enabled: false` and `evidence.verified: false` until a
  human verifies it against the real device and completes the repo's approval flow.
