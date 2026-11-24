"""Connector Builder CLI.

    python -m tools.connector_builder create --id vendor.model --display-name "Vendor Model"
    python -m tools.connector_builder validate
    python -m tools.connector_builder validate-draft output/vendor.model.draft.json
    python -m tools.connector_builder import-draft output/vendor.model.draft.json --approve
    python -m tools.connector_builder list
    python -m tools.connector_builder manufacturers --json
    python -m tools.connector_builder models --json
    python -m tools.connector_builder configure output/vendor.model.draft.json \
      --endpoint-id root --path /cgi-bin/ajax --method GET --auth-handler none
    python -m tools.connector_builder map output/vendor.model.draft.json \
      --endpoint root --field '$.Model' --metric device.model
    python -m tools.connector_builder inspect output/vendor.model.draft.json \
      --fixture output/vendor.model.fixture.json --json
    python -m tools.connector_builder test output/vendor.model.draft.json \
      --fixture output/vendor.model.fixture.json --json
    python -m tools.connector_builder register output/vendor.model.draft.json \
      --manufacturer vendor --model Model --approve

``create`` prints a draft scaffold unless ``--output`` is supplied. Drafts are
explicitly marked as unobserved; no endpoint, credential, or value is treated
as evidence until it has been seen on a real device and the status is updated.

``validate-draft`` checks one generated or hand-written draft against the same
schema and cross-checks as the catalog. ``import-draft`` requires an explicit
``--approve`` flag, refuses duplicate ids, never enables the imported profile,
and never rewrites existing connector entries.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence
from urllib.parse import urlparse

from auth_handlers import HANDLERS
from connector_executor import ConnectorExecutor, EndpointError
from connector_manager import (
    DEFAULT_CATALOG,
    DEFAULT_CREDENTIALS,
    DEFAULT_EQUIPMENT,
    DEFAULT_SCHEMA,
    ConnectorManager,
    ValidationIssue,
)
from models.universal import METRICS
from profile_safety import (
    MAX_RESPONSE_BYTES,
    UnsafeProfileRequest,
    normalize_gateway,
    validate_endpoint_path,
    validate_method,
    validate_scheme,
)


CONNECTOR_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:\.[a-z0-9_-]+)+$")
FIELD_PATH_RE = re.compile(r"^\$(?:\.[A-Za-z0-9_*-]+)+$")
SECRET_KEY_RE = re.compile(
    r"password|passwd|secret|token|authorization|cookie|bearer|api[_-]?key|private[_-]?key",
    re.IGNORECASE,
)
UNRESOLVED_PLACEHOLDER_RE = re.compile(r"__OBSERVE_[A-Z]")
REGISTRATION_MANAGED_FIELDS = frozenset({
    "manufacturer_id", "model", "firmware", "enabled",
})
SENSITIVE_METRICS = frozenset({
    "device.serial",
    "device.gpon_sn",
    "device.mac_base",
    "device.mac_wan",
    "wan.pppoe_user",
    "wan.public_ip",
    "wan.gateway_ip",
    "wan.mac",
    "voip.phone_number",
    "optical.loid",
})
MAX_FIXTURE_BYTES = MAX_RESPONSE_BYTES
MAX_FIXTURE_ENDPOINTS = 100


def build_scaffold(connector_id: str, display_name: str) -> Dict[str, Any]:
    """Return an inert, secret-free draft connector."""

    return {
        "id": connector_id,
        "display_name": display_name,
        "status": "draft",
        "enabled": False,
        "generated_by": "tools.connector_builder",
        "source_har": "not_applicable",
        "requires_manual_auth_handler": False,
        "evidence": {
            "source": "Template only — no device has been observed.",
            "verified": False,
            "notes": [
                "Replace every __OBSERVE_*__ placeholder with values seen on a real device.",
                "Add extraction rules only for fields observed in real responses.",
                "Change status to 'observed' only after recording evidence."
            ]
        },
        "identification": {
            "confidence": "unknown",
            "rules": [
                {
                    "id": "observe-model",
                    "phase": "pre_auth",
                    "source": "root",
                    "field": "ModelName",
                    "operator": "equals",
                    "value": "__OBSERVE_MODEL__",
                    "confidence": "unknown",
                    "evidence": "Replace with a value observed before authentication."
                }
            ]
        },
        "auth": {
            "credential_suggestion_key": "generic_user_supplied",
            "flows": [
                {
                    "id": "observe-auth-flow",
                    "type": "none",
                    "order": 1,
                    "success": {
                        "field": "__OBSERVE_SUCCESS_FIELD__",
                        "allowed": [True]
                    }
                }
            ]
        },
        "auth_observations": {
            "classification": "none",
            "proposed_handler": "none",
            "sequence": [],
            "notes": ["No traffic was analyzed for this template."],
            "evidence": "Template only."
        },
        "identification_candidates": [],
        "extraction_candidates": [],
        "metric_mapping_candidates": [],
        "endpoints": [
            {
                "id": "root",
                "method": "GET",
                "path": "/__OBSERVE_PATH__",
                "format": "json",
                "timeout_seconds": 5,
                "auth": False,
                "description": "Template endpoint; replace with an observed request."
            }
        ],
        "extraction": [],
        "validation": [],
        "dependencies": {},
        "capabilities": ["unclassified"],
        "limitations": [
            "Draft scaffold is not device-ready and is disabled by default."
        ],
        "manual_review": [
            "Replace every placeholder with a value observed on a real device.",
            "Confirm the authentication flow against real traffic.",
            "Validate the disabled draft on a physical ONT before enabling it."
        ]
    }


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "connectors": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Cannot read catalog {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("connectors"), list):
        raise SystemExit(f"Catalog {path} must contain a connectors array")
    return data


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"Cannot write catalog {path}: {exc}") from exc


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    """Write via a temporary file in the same directory, then replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _load_equipment(path: Path) -> Dict[str, Any]:
    """Read the equipment catalog with fail-closed shape checks."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"Cannot read equipment catalog {path}: {exc}") from exc
    except ValueError as exc:
        raise SystemExit(f"Equipment catalog {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") not in (1, "1", "1.0.0"):
        raise SystemExit(f"Equipment catalog {path} has an unsupported schema version")
    if not isinstance(data.get("manufacturers"), list):
        raise SystemExit(f"Equipment catalog {path} has no manufacturers array")
    return data


def _equipment_rows(catalog: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for entry in catalog.get("manufacturers", []):
        if not isinstance(entry, dict):
            continue
        rows.append({
            "id": str(entry.get("id", "")),
            "name": str(entry.get("name", "")),
            "profiles": [str(item) for item in entry.get("profiles", []) if isinstance(item, str)],
        })
    return rows


def _atomic_write_many(writes: Sequence[tuple]) -> None:
    """Atomically write one or more JSON files after all data is validated."""

    prepared: List[tuple] = []
    try:
        for path, data in writes:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            )
            temp_path = Path(handle.name)
            try:
                with handle:
                    handle.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                temp_path.unlink(missing_ok=True)
                raise
            prepared.append((temp_path, path))
        for temp_path, path in prepared:
            os.replace(temp_path, path)
    finally:
        for temp_path, _ in prepared:
            temp_path.unlink(missing_ok=True)


def _assert_no_secrets(value: Any, label: str = "$fixture") -> None:
    """Refuse credential-like keys and oversized fixture payloads."""

    if len(json.dumps(value, ensure_ascii=False)) > MAX_FIXTURE_BYTES:
        raise SystemExit("Fixture is too large")
    if SECRET_KEY_RE.search(label):
        raise SystemExit(f"Fixture contains a credential-like key: {label}")
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_no_secrets(item, f"{label}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_secrets(item, f"{label}[{index}]")


def _load_fixture(path: Path) -> Dict[str, Any]:
    """Load a sanitized, deterministic endpoint-response fixture."""

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SystemExit(f"Cannot read fixture {path}: {exc}") from exc
    if len(raw) > MAX_FIXTURE_BYTES:
        raise SystemExit("Fixture exceeds the size limit")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SystemExit(f"Fixture {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit("Fixture must be a JSON object")
    _assert_no_secrets(data)
    if data.get("schema_version") != 1:
        raise SystemExit("Fixture schema_version must be 1")
    responses = data.get("responses")
    if not isinstance(responses, dict) or not responses:
        raise SystemExit("Fixture must contain a non-empty responses object")
    if len(responses) > MAX_FIXTURE_ENDPOINTS:
        raise SystemExit("Fixture contains too many endpoint responses")
    for name, payload in responses.items():
        if not isinstance(name, str) or not isinstance(payload, dict):
            raise SystemExit("Fixture responses must map names to JSON objects")
    for key in ("failed_endpoints", "status_overrides", "state"):
        if key in data and not isinstance(data[key], dict if key != "failed_endpoints" else list):
            raise SystemExit(f"Fixture {key} has an invalid shape")
    if data.get("gateway"):
        try:
            normalize_gateway(data["gateway"])
        except UnsafeProfileRequest as exc:
            raise SystemExit(f"Unsafe fixture gateway: {exc}") from exc
    return data


class _FixtureResponse:
    """Minimal response object for offline profile testing."""

    def __init__(self, payload: Dict[str, Any], status_code: int = 200):
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)
        self.headers = {"Content-Type": "application/json"}


class _FixtureSession:
    """Deterministic read-only session backed solely by fixture payloads."""

    def __init__(self, fixture: Dict[str, Any]):
        self.headers: Dict[str, str] = {}
        self.auth: Any = None
        self.responses = fixture["responses"]
        self.failed = set(fixture.get("failed_endpoints", []))
        self.status_overrides = fixture.get("status_overrides", {})
        self.calls: List[Dict[str, Any]] = []

    def request(self, method, url, **kwargs):
        params = dict(kwargs.get("params") or {})
        data = dict(kwargs.get("data") or {})
        name = str(
            params.get("ajaxmethod")
            or data.get("ajaxmethod")
            or urlparse(url).path
        )
        self.calls.append({"method": method, "path": urlparse(url).path, "name": name})
        if name in self.failed:
            return _FixtureResponse({}, 500)
        status = self.status_overrides.get(name, 200)
        if isinstance(status, list):
            status = status[min(len(self.calls) - 1, len(status) - 1)]
        payload = self.responses.get(name)
        if payload is None:
            return _FixtureResponse({}, 404)
        return _FixtureResponse(dict(payload), int(status))


def _flatten_json(value: Any, prefix: str = "$", limit: int = 200) -> List[Dict[str, Any]]:
    """Return field paths and types only; never return response values."""

    rows: List[Dict[str, Any]] = []

    def walk(node: Any, path: str, depth: int) -> None:
        if len(rows) >= limit or depth > 8:
            return
        if isinstance(node, dict):
            for key, item in node.items():
                child = f"{path}.{key}" if re.fullmatch(r"[A-Za-z0-9_-]+", str(key)) else f"{path}[{key}]"
                walk(item, child, depth + 1)
        elif isinstance(node, list):
            rows.append({"path": path, "type": "array", "length": len(node)})
            for index, item in enumerate(node[:5]):
                walk(item, f"{path}[{index}]", depth + 1)
        else:
            rows.append({"path": path, "type": type(node).__name__})

    walk(value, prefix, 0)
    return rows


def _safe_metric_value(metric_id: str, value: Any) -> Any:
    if metric_id in SENSITIVE_METRICS:
        return "[REDACTED]"
    return value


def _model_report(model: Any) -> Dict[str, Any]:
    """Build a redacted Profile Builder report from a normalized model."""

    domains: Dict[str, Any] = {}
    for domain in sorted(model.domains, key=lambda item: item.value):
        metrics: Dict[str, Any] = {}
        for metric_id, metric in sorted(model.domains[domain].items()):
            metrics[metric_id] = {
                "availability": metric.availability.value,
                "value": _safe_metric_value(metric_id, metric.value),
            }
        domains[domain.value] = metrics
    return {
        "gateway": model.gateway,
        "domains": domains,
        "endpoint_errors": list(model.endpoint_errors),
        "devices_loaded": model.devices_loaded,
        "device_count": len(model.devices),
        "voip_lines": len(model.voip_lines),
        "device_compatibility": "not_evaluated",
        "physical_device_check_required": True,
    }


def _profile_issues(profile: Dict[str, Any], args: argparse.Namespace) -> List[ValidationIssue]:
    if profile.get("status") == "draft":
        return validate_draft_issues(profile, args)
    return _validate_catalog_data({"schema_version": 1, "connectors": [profile]}, args)


def _registration_comparable(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Return profile content excluding fields owned by registration itself."""

    return {
        key: value for key, value in profile.items()
        if key not in REGISTRATION_MANAGED_FIELDS
    }


def _fail_on_errors(issues: Sequence[ValidationIssue], label: str) -> bool:
    errors = [issue for issue in issues if issue.severity == "error"]
    if not errors:
        return False
    print(f"{label} failed with {len(errors)} error(s); nothing was executed or written.")
    _print_issues(errors)
    return True


@contextlib.contextmanager
def _manager_for_profile(
    profile: Dict[str, Any],
    args: argparse.Namespace,
    equipment_path: Optional[Path] = None,
) -> Iterator[ConnectorManager]:
    with tempfile.TemporaryDirectory(prefix="homerouter-profile-test-") as directory:
        catalog_path = Path(directory) / "connectors.json"
        catalog_path.write_text(
            json.dumps({"schema_version": 1, "connectors": [profile]}, indent=2),
            encoding="utf-8",
        )
        yield ConnectorManager(
            catalog_path=catalog_path,
            credentials_path=Path(args.credentials),
            schema_path=Path(args.schema),
            equipment_path=equipment_path,
        )


def cmd_create(args: argparse.Namespace) -> int:
    connector_id = args.id.strip()
    if not CONNECTOR_ID_PATTERN.match(connector_id):
        raise SystemExit(
            "Invalid connector id: use lowercase vendor.device, e.g. fiberhome.hg6145f1"
        )
    scaffold = build_scaffold(connector_id, args.display_name.strip())

    if not args.output:
        print(json.dumps(scaffold, indent=2, ensure_ascii=False))
        return 0

    output = Path(args.output)
    catalog = _read_json(output)
    existing = {
        item.get("id")
        for item in catalog["connectors"]
        if isinstance(item, dict)
    }
    if connector_id in existing:
        raise SystemExit(f"Connector '{connector_id}' already exists in {output}")
    catalog.setdefault("schema_version", 1)
    catalog["connectors"].append(scaffold)
    _write_json(output, catalog)
    print(f"Added draft connector '{connector_id}' to {output}")
    print("It is disabled and unobserved until real evidence is recorded.")
    return 0


def _manager(args: argparse.Namespace) -> ConnectorManager:
    return ConnectorManager(
        catalog_path=Path(args.catalog),
        credentials_path=Path(args.credentials),
        schema_path=Path(args.schema),
    )


def _print_issues(issues: Sequence[Any]) -> None:
    for issue in issues:
        print(f"  [{issue.severity.upper()}] {issue.code} at {issue.path}")
        print(f"      {issue.message}")


def _load_draft(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"Cannot read draft {path}: {exc}") from exc
    except ValueError as exc:
        raise SystemExit(f"Draft {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"Draft {path} must be a JSON object")
    return data


def _validate_catalog_data(
    catalog: Dict[str, Any], args: argparse.Namespace
) -> List[ValidationIssue]:
    """Validate in-memory catalog data with the project's schema/checks."""

    with tempfile.TemporaryDirectory(prefix="homerouter-draft-") as directory:
        catalog_path = Path(directory) / "connectors.json"
        catalog_path.write_text(
            json.dumps(catalog, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        manager = ConnectorManager(
            catalog_path=catalog_path,
            credentials_path=Path(args.credentials),
            schema_path=Path(args.schema),
        )
        return list(manager.validate().issues)


def _draft_specific_issues(draft: Dict[str, Any]) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    base = "$.draft"

    def add_error(code: str, path: str, message: str) -> None:
        issues.append(ValidationIssue("error", code, f"{base}{path}", message))

    def add_warning(code: str, path: str, message: str) -> None:
        issues.append(ValidationIssue("warning", code, f"{base}{path}", message))

    if draft.get("status") != "draft":
        add_error("draft_status_required", ".status", "an imported draft must have status 'draft'")
    if draft.get("enabled") is not False:
        add_error("draft_must_be_disabled", ".enabled", "an imported draft must have enabled: false")
    evidence = draft.get("evidence")
    if not isinstance(evidence, dict) or evidence.get("verified") is not False:
        add_error("draft_unverified", ".evidence.verified", "draft evidence must have verified: false")
    identification = draft.get("identification")
    if isinstance(identification, dict) and identification.get("confidence") == "exact":
        add_error(
            "draft_confidence_not_exact", ".identification.confidence",
            "a draft must not claim exact identification until physically reviewed",
        )
    if not draft.get("manual_review"):
        add_error("draft_manual_review_required", ".manual_review", "draft profiles must list manual review steps")
    if not draft.get("limitations"):
        add_warning("draft_limitations_missing", ".limitations", "draft profiles should state their limitations")
    if not draft.get("generated_by"):
        add_warning("draft_generated_by_missing", ".generated_by", "record which tool or process generated the draft")
    if not draft.get("source_har"):
        add_warning("draft_source_har_missing", ".source_har", "record the sanitized HAR source name")
    return issues


def validate_draft_issues(
    draft: Dict[str, Any], args: argparse.Namespace
) -> List[ValidationIssue]:
    catalog = {"schema_version": 1, "connectors": [draft]}
    issues = _validate_catalog_data(catalog, args)
    issues.extend(_draft_specific_issues(draft))
    return issues


def cmd_validate_draft(args: argparse.Namespace) -> int:
    draft = _load_draft(Path(args.draft))
    issues = validate_draft_issues(draft, args)
    errors = [issue for issue in issues if issue.severity == "error"]
    warnings = [issue for issue in issues if issue.severity == "warning"]

    if args.json:
        print(json.dumps({
            "valid": not errors,
            "connector_id": draft.get("id"),
            "errors": [issue.to_dict() for issue in errors],
            "warnings": [issue.to_dict() for issue in warnings],
            "device_compatibility": "not_evaluated",
        }, indent=2, ensure_ascii=False))
        return 0 if not errors else 1

    result = "PASS" if not errors else "FAIL"
    print(f"Draft validation: {result}")
    print(f"  draft: {args.draft}")
    print(f"  connector: {draft.get('id')}")
    print(f"  errors: {len(errors)}")
    print(f"  warnings: {len(warnings)}")
    if issues:
        print("Issues:")
        _print_issues(issues)
    print("Device compatibility: NOT EVALUATED (requires a physical ONT comparison)")
    return 0 if not errors else 1


def cmd_import_draft(args: argparse.Namespace) -> int:
    if not args.approve:
        print(
            "Refusing to import: explicit approval is required.\n"
            "Re-run with --approve after reviewing the draft and its evidence bundle.",
            file=sys.stderr,
        )
        return 2

    draft = _load_draft(Path(args.draft))
    draft_issues = validate_draft_issues(draft, args)
    draft_errors = [issue for issue in draft_issues if issue.severity == "error"]
    if draft_errors:
        print(f"Draft validation failed with {len(draft_errors)} error(s); nothing was imported.")
        _print_issues(draft_errors)
        return 1
    if draft_issues:
        print("Draft warnings:")
        _print_issues(draft_issues)

    catalog_path = Path(args.catalog)
    catalog = _read_json(catalog_path)
    existing = [
        item for item in catalog["connectors"]
        if isinstance(item, dict)
    ]
    existing_ids = {item.get("id") for item in existing}
    connector_id = draft.get("id")
    if connector_id in existing_ids:
        print(
            f"Refusing to import '{connector_id}': that id already exists in {catalog_path}.",
            file=sys.stderr,
        )
        return 1

    original_connectors = copy.deepcopy(catalog["connectors"])
    profile = copy.deepcopy(draft)
    profile["enabled"] = False
    profile["status"] = "draft"
    catalog.setdefault("schema_version", 1)
    catalog["connectors"] = original_connectors + [profile]

    combined_issues = _validate_catalog_data(catalog, args)
    combined_errors = [issue for issue in combined_issues if issue.severity == "error"]
    if combined_errors:
        print("Combined catalog validation failed; nothing was imported.")
        _print_issues(combined_errors)
        return 1

    _atomic_write_json(catalog_path, catalog)

    written = _read_json(catalog_path)
    if written["connectors"][:-1] != original_connectors:
        print("Post-write verification failed: existing connectors changed.", file=sys.stderr)
        return 1
    imported = written["connectors"][-1]
    if imported.get("id") != connector_id or imported.get("enabled") is not False:
        print("Post-write verification failed: imported profile is not a disabled draft.", file=sys.stderr)
        return 1

    print(f"Imported disabled draft '{connector_id}' into {catalog_path}")
    print("Existing connector profiles were left unchanged.")
    print("Compatibility is NOT EVALUATED; physical ONT validation is still required.")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    report = _manager(args).validate()
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        return 0 if report.structural_valid else 1

    result = "PASS" if report.structural_valid else "FAIL"
    print(f"Structural validity: {result}")
    print(f"  catalog: {args.catalog}")
    print(f"  connectors: {report.connector_count}")
    print(f"  errors: {len(report.errors)}")
    print(f"  warnings: {len(report.warnings)}")
    if report.issues:
        print("Issues:")
        _print_issues(report.issues)

    compatibility = report.compatibility()
    print(f"Device compatibility: {compatibility['status'].upper()}")
    print(f"  {compatibility['reason']}")
    return 0 if report.structural_valid else 1


def cmd_list(args: argparse.Namespace) -> int:
    rows = _manager(args).list_connectors()
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0

    print(f"{'ID':<28} {'STATUS':<10} {'CONFIDENCE':<10} ENABLED  CAPABILITIES")
    for row in rows:
        print(
            f"{row['id']:<28} {row['status']:<10} "
            f"{row['identification_confidence']:<10} "
            f"{str(row['enabled']):<7}  {', '.join(row['capabilities'])}"
        )
    print("Device compatibility: NOT EVALUATED (requires a physical ONT comparison)")
    return 0


def cmd_auth_handlers(args: argparse.Namespace) -> int:
    handlers = sorted(HANDLERS)
    if args.json:
        print(json.dumps({"handlers": handlers}, indent=2))
        return 0
    print("Trusted authentication handlers:")
    for handler in handlers:
        print(f"  {handler}")
    print("Profiles may select only these identifiers; profile JSON never contains code.")
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    rows = [
        {"id": metric_id, "domain": spec.domain.value, "label": spec.label}
        for metric_id, spec in sorted(METRICS.items())
    ]
    if args.json:
        print(json.dumps({"metrics": rows}, indent=2))
        return 0
    print(f"{'METRIC ID':<32} {'DOMAIN':<20} LABEL")
    for row in rows:
        print(f"{row['id']:<32} {row['domain']:<20} {row['label']}")
    return 0


def cmd_manufacturers(args: argparse.Namespace) -> int:
    catalog = _load_equipment(Path(args.equipment))
    rows = _equipment_rows(catalog)
    if args.json:
        print(json.dumps({
            "ok": bool(rows),
            "manufacturers": rows,
            "message": "" if rows else "Manufacturer catalog is empty.",
        }, indent=2, ensure_ascii=False))
        return 0
    print(f"{'ID':<16} {'NAME':<24} PROFILES")
    for row in rows:
        print(f"{row['id']:<16} {row['name']:<24} {', '.join(row['profiles']) or '-'}")
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    connectors = _read_json(Path(args.catalog))
    equipment = _load_equipment(Path(args.equipment))
    linked = {
        str(profile_id)
        for row in _equipment_rows(equipment)
        for profile_id in row["profiles"]
    }
    rows: List[Dict[str, Any]] = []
    for connector in connectors.get("connectors", []):
        if not isinstance(connector, dict):
            continue
        connector_id = str(connector.get("id", ""))
        manufacturer_id = str(connector.get("manufacturer_id") or "")
        if connector_id not in linked and not manufacturer_id:
            continue
        rows.append({
            "id": connector_id,
            "manufacturer_id": manufacturer_id,
            "model": str(connector.get("model") or ""),
            "status": str(connector.get("status") or ""),
            "enabled": bool(connector.get("enabled", True)),
            "registered": connector_id in linked,
        })
    if args.json:
        print(json.dumps({"models": rows}, indent=2, ensure_ascii=False))
        return 0
    print(f"{'ID':<28} {'MANUFACTURER':<14} {'MODEL':<18} {'STATUS':<10} ENABLED REGISTERED")
    for row in rows:
        print(
            f"{row['id']:<28} {row['manufacturer_id']:<14} {row['model']:<18} "
            f"{row['status']:<10} {str(row['enabled']):<7} {row['registered']}"
        )
    print("Device compatibility: NOT EVALUATED unless physically verified against the exact firmware.")
    return 0


def cmd_configure(args: argparse.Namespace) -> int:
    path = Path(args.draft)
    profile = _load_draft(path)
    modified = copy.deepcopy(profile)

    if args.manufacturer:
        modified["manufacturer_id"] = args.manufacturer.strip()
    if args.model:
        modified["model"] = args.model.strip()
    if args.firmware:
        modified["firmware"] = args.firmware.strip()

    if args.endpoint_id:
        try:
            method = validate_method(args.method or "GET")
            scheme = validate_scheme(args.scheme or modified.get("scheme", "http"))
            endpoint_path = validate_endpoint_path(args.path)
        except UnsafeProfileRequest as exc:
            raise SystemExit(f"Unsafe endpoint configuration: {exc}") from exc
        endpoints = modified.setdefault("endpoints", [])
        endpoint = next(
            (item for item in endpoints if isinstance(item, dict) and item.get("id") == args.endpoint_id),
            None,
        )
        if endpoint is None:
            endpoint = {"id": args.endpoint_id}
            endpoints.append(endpoint)
        endpoint.update({
            "method": method,
            "path": endpoint_path,
            "format": args.format,
            "timeout_seconds": args.timeout,
            "auth": False,
            "scheme": scheme,
            "description": f"Profile Builder configured endpoint {args.endpoint_id}.",
        })

    if args.auth_handler:
        if args.auth_handler not in HANDLERS:
            raise SystemExit(f"Unknown authentication handler {args.auth_handler!r}")
        flows = ((modified.get("auth") or {}).get("flows") or [])
        if flows and isinstance(flows[0], dict):
            flows[0]["type"] = args.auth_handler
        else:
            modified.setdefault("auth", {})["flows"] = [{
                "id": "selected-auth-handler",
                "type": args.auth_handler,
                "order": 1,
                "success": {"field": "authenticated", "allowed": [True]},
            }]
        modified["requires_manual_auth_handler"] = args.auth_handler not in {"none"}

    issues = _profile_issues(modified, args)
    if _fail_on_errors(issues, "Profile configuration validation"):
        return 1
    _atomic_write_json(path, modified)
    print(f"Updated declarative profile {path}; no Python source was modified.")
    print("Authentication handlers are selected by trusted identifier only.")
    print("Device compatibility: NOT EVALUATED")
    return 0


def cmd_map(args: argparse.Namespace) -> int:
    path = Path(args.draft)
    profile = _load_draft(path)
    metric_id = args.metric.strip()
    if metric_id not in METRICS:
        raise SystemExit(f"Unknown UniversalModel metric id {metric_id!r}; run 'metrics --json'")
    if not FIELD_PATH_RE.match(args.field.strip()):
        raise SystemExit("Field path must be a JSON path such as $.Status.Model")
    endpoint_ids = {
        item.get("id")
        for item in profile.get("endpoints", [])
        if isinstance(item, dict)
    }
    if args.endpoint not in endpoint_ids:
        raise SystemExit(f"Unknown endpoint {args.endpoint!r} in profile")

    rule_id = args.id or f"map-{metric_id.replace('.', '-')}"
    modified = copy.deepcopy(profile)
    extraction = [
        item for item in modified.get("extraction", [])
        if not (
            isinstance(item, dict)
            and item.get("metric") == metric_id
            and item.get("endpoint") == args.endpoint
        )
    ]
    extraction.append({
        "id": rule_id,
        "metric": metric_id,
        "endpoint": args.endpoint,
        "method": "json_path",
        "expression": args.field.strip(),
        "availability_on_missing": args.availability_on_missing,
    })
    modified["extraction"] = extraction
    capabilities = [
        item for item in modified.get("capabilities", [])
        if isinstance(item, str) and item != "unclassified"
    ]
    domain = METRICS[metric_id].domain.value
    if domain not in capabilities:
        capabilities.append(domain)
    modified["capabilities"] = capabilities
    candidates = [
        item for item in modified.get("metric_mapping_candidates", [])
        if not (isinstance(item, dict) and item.get("metric") == metric_id)
    ]
    candidates.append({
        "endpoint": args.endpoint,
        "field": args.field.strip(),
        "metric": metric_id,
        "manual_review": True,
        "evidence": "Mapped from an inspected response field; not physical-hardware evidence.",
    })
    modified["metric_mapping_candidates"] = candidates

    issues = _profile_issues(modified, args)
    if _fail_on_errors(issues, "Metric mapping validation"):
        return 1
    _atomic_write_json(path, modified)
    print(f"Mapped {args.field} -> {metric_id} on endpoint {args.endpoint}.")
    print(f"Domain: {METRICS[metric_id].domain.value}; compatibility remains NOT EVALUATED.")
    return 0


def _inspect_names(executor: Any, requested: Optional[str]) -> List[str]:
    endpoints = executor.endpoints
    if requested:
        if requested not in endpoints:
            raise SystemExit(f"Unknown endpoint {requested!r}")
        endpoint = endpoints[requested]
        if endpoint.get("auth") is not False:
            raise SystemExit("Only unauthenticated endpoints can be inspected")
        if SECRET_KEY_RE.search(json.dumps(endpoint.get("body", {}), ensure_ascii=False)):
            raise SystemExit("Refusing to inspect an endpoint requiring credentials")
        return [requested]
    sources = {
        str(rule.get("source"))
        for rule in (executor.connector.get("identification") or {}).get("rules", [])
        if isinstance(rule, dict) and rule.get("phase") == "pre_auth"
    }
    names = [
        endpoint_id for endpoint_id in sources
        if endpoint_id in endpoints and endpoints[endpoint_id].get("auth") is False
    ]
    if names:
        return names
    return [
        endpoint_id for endpoint_id, endpoint in endpoints.items()
        if endpoint.get("auth") is False
        and not SECRET_KEY_RE.search(json.dumps(endpoint.get("body", {}), ensure_ascii=False))
    ]


def cmd_inspect(args: argparse.Namespace) -> int:
    profile = _load_draft(Path(args.profile))
    issues = _profile_issues(profile, args)
    if _fail_on_errors(issues, "Profile inspection validation"):
        return 1

    fixture = _load_fixture(Path(args.fixture))
    gateway = args.gateway or fixture.get("gateway")
    if not gateway:
        raise SystemExit("Fixture has no gateway; supply --gateway")
    session = _FixtureSession(fixture)

    runner = copy.deepcopy(profile)
    runner["enabled"] = True
    with _manager_for_profile(runner, args) as manager:
        executor = ConnectorExecutor(runner, gateway, {}, manager=manager, session=session)
        results = []
        for endpoint_id in _inspect_names(executor, args.endpoint):
            endpoint = executor.endpoints[endpoint_id]
            try:
                result = executor.execute_endpoint(endpoint_id)
                results.append({
                    "endpoint": endpoint_id,
                    "method": str(endpoint.get("method", "GET")).upper(),
                    "path": endpoint.get("path"),
                    "status_code": result.status_code,
                    "fields": _flatten_json(result.payload),
                    "error": None,
                })
            except EndpointError as exc:
                results.append({
                    "endpoint": endpoint_id,
                    "method": str(endpoint.get("method", "GET")).upper(),
                    "path": endpoint.get("path"),
                    "status_code": None,
                    "fields": [],
                    "error": str(exc),
                })

    report = {
        "gateway": executor.gateway,
        "mode": "offline_fixture",
        "endpoints": results,
        "device_compatibility": "not_evaluated",
        "physical_device_check_required": True,
    }
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    print(f"Inspection mode: {report['mode']}; gateway: {executor.gateway}")
    for row in results:
        print(f"  {row['endpoint']}: {row['method']} {row['path']} ({len(row['fields'])} fields)")
        for field in row["fields"]:
            print(f"    {field['path']} [{field['type']}]")
        if row["error"]:
            print(f"    error: {row['error']}")
    print("Response values and credentials are never printed.")
    print("Device compatibility: NOT EVALUATED")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    profile = _load_draft(Path(args.profile))
    issues = _profile_issues(profile, args)
    if _fail_on_errors(issues, "Profile test validation"):
        return 1
    fixture = _load_fixture(Path(args.fixture))
    gateway = args.gateway or fixture.get("gateway")
    if not gateway:
        raise SystemExit("Fixture has no gateway; supply --gateway")
    session = _FixtureSession(fixture)

    runner = copy.deepcopy(profile)
    runner["enabled"] = True
    with _manager_for_profile(runner, args) as manager:
        executor = ConnectorExecutor(runner, gateway, {}, manager=manager, session=session)
        executor.state.update({
            key: value for key, value in fixture.get("state", {}).items()
        })
        executor.authenticated = True  # offline fixture only; never contacts a router
        discovery = executor.discover()
        report: Dict[str, Any] = {
            "connector_id": profile.get("id"),
            "gateway": executor.gateway,
            "mode": "offline_fixture",
            "discovery_errors": discovery["errors"],
            "observations": {
                name: _flatten_json(payload)
                for name, payload in discovery["observations"].items()
            },
            "device_compatibility": "not_evaluated",
            "physical_device_check_required": True,
        }
        model = executor._collect_once()
        report.update(_model_report(model))
        report["endpoint_errors_count"] = len(model.endpoint_errors)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    print(f"Profile test mode: {report['mode']}; gateway: {report['gateway']}")
    print(f"Observations: {len(report['observations'])}; discovery errors: {len(report['discovery_errors'])}")
    if "domains" in report:
        print(f"Domains: {len(report['domains'])}; endpoint errors: {report['endpoint_errors_count']}")
    print("No credentials were used and no router settings were changed.")
    print("Device compatibility: NOT EVALUATED")
    return 0


def _validate_catalog_and_equipment(
    catalog: Dict[str, Any], equipment: Dict[str, Any], args: argparse.Namespace
) -> List[ValidationIssue]:
    with tempfile.TemporaryDirectory(prefix="homerouter-registration-") as directory:
        root = Path(directory)
        catalog_path = root / "connectors.json"
        equipment_path = root / "equipment.json"
        catalog_path.write_text(json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8")
        equipment_path.write_text(json.dumps(equipment, indent=2, ensure_ascii=False), encoding="utf-8")
        manager = ConnectorManager(
            catalog_path=catalog_path,
            credentials_path=Path(args.credentials),
            schema_path=Path(args.schema),
            equipment_path=equipment_path,
        )
        return list(manager.validate().issues)


def cmd_register(args: argparse.Namespace) -> int:
    if not args.approve:
        print(
            "Refusing to register: --approve is required after manual review.",
            file=sys.stderr,
        )
        return 2

    profile = _load_draft(Path(args.profile))
    if UNRESOLVED_PLACEHOLDER_RE.search(json.dumps(profile, ensure_ascii=False)):
        raise SystemExit(
            "Refusing to register: unresolved __OBSERVE_*__ placeholder remains; "
            "complete manual review first"
        )
    issues = _profile_issues(profile, args)
    if _fail_on_errors(issues, "Registration validation"):
        return 1

    manufacturer_id = args.manufacturer.strip()
    model = args.model.strip()
    if not manufacturer_id or not model:
        raise SystemExit("--manufacturer and --model are required")
    declared_manufacturer = str(profile.get("manufacturer_id") or "").strip()
    declared_model = str(profile.get("model") or "").strip()
    if declared_manufacturer and declared_manufacturer != manufacturer_id:
        raise SystemExit(
            f"Profile declares manufacturer '{declared_manufacturer}', not '{manufacturer_id}'"
        )
    if declared_model and declared_model.lower() != model.lower():
        raise SystemExit(f"Profile declares model '{declared_model}', not '{model}'")

    connector_id = str(profile.get("id", ""))
    if not CONNECTOR_ID_PATTERN.match(connector_id):
        raise SystemExit("Profile has an invalid connector id")

    if args.enable:
        evidence = profile.get("evidence")
        if profile.get("status") != "observed" or not isinstance(evidence, dict) or evidence.get("verified") is not True:
            raise SystemExit("Refusing to enable: only an observed profile with verified manual evidence can be enabled")

    catalog_path = Path(args.catalog)
    equipment_path = Path(args.equipment)
    catalog = _read_json(catalog_path)
    equipment = _load_equipment(equipment_path)
    entries = [entry for entry in equipment["manufacturers"] if isinstance(entry, dict)]
    manufacturer_entry = next(
        (entry for entry in entries if str(entry.get("id")) == manufacturer_id), None
    )
    if manufacturer_entry is None:
        if not (args.manufacturer_name or "").strip():
            raise SystemExit("New manufacturer requires --manufacturer-name")
        manufacturer_entry = {
            "id": manufacturer_id,
            "name": args.manufacturer_name.strip(),
            "profiles": [],
        }
        entries.append(manufacturer_entry)
    manufacturer_entry["profiles"] = [
        item for item in (manufacturer_entry.get("profiles") or []) if isinstance(item, str)
    ]

    linked_ids = {
        str(item)
        for entry in entries
        for item in (entry.get("profiles") or [])
        if isinstance(item, str)
    }
    for existing in catalog.get("connectors", []):
        if not isinstance(existing, dict):
            continue
        if str(existing.get("id")) not in linked_ids:
            continue
        if str(existing.get("model") or "").strip().lower() == model.lower():
            if str(existing.get("id")) != connector_id:
                raise SystemExit(
                    f"Model '{model}' is already registered to '{existing.get('id')}'"
                )

    existing_connectors = [
        item for item in catalog.get("connectors", []) if isinstance(item, dict)
    ]
    existing_by_id = {str(item.get("id")): item for item in existing_connectors}
    if (
        connector_id in existing_by_id
        and _registration_comparable(existing_by_id[connector_id])
        != _registration_comparable(profile)
    ):
        raise SystemExit(
            f"Connector '{connector_id}' already exists with different content; "
            "import a reviewed draft or edit the existing profile explicitly"
        )

    registered = copy.deepcopy(profile)
    registered["manufacturer_id"] = manufacturer_id
    registered["model"] = model
    if args.firmware:
        registered["firmware"] = args.firmware.strip()
    if args.enable:
        registered["enabled"] = True
    elif connector_id not in existing_by_id:
        registered["enabled"] = False

    new_connectors = list(existing_connectors)
    if connector_id not in existing_by_id:
        new_connectors.append(registered)
    if connector_id not in manufacturer_entry["profiles"]:
        manufacturer_entry["profiles"].append(connector_id)

    new_catalog = {"schema_version": 1, "connectors": new_connectors}
    new_equipment = {"schema_version": 1, "manufacturers": entries}
    combined = _validate_catalog_and_equipment(new_catalog, new_equipment, args)
    if _fail_on_errors(combined, "Combined registration validation"):
        return 1

    _atomic_write_many((
        (catalog_path, new_catalog),
        (equipment_path, new_equipment),
    ))

    written = _load_equipment(equipment_path)
    written_catalog = _read_json(catalog_path)
    if connector_id not in {
        item.get("id") for item in written_catalog["connectors"] if isinstance(item, dict)
    }:
        raise SystemExit("Post-write verification failed: connector was not registered")
    if connector_id not in {
        item for entry in _equipment_rows(written) for item in entry["profiles"]
    }:
        raise SystemExit("Post-write verification failed: equipment association was not registered")

    if args.json:
        print(json.dumps({
            "registered": connector_id,
            "manufacturer_id": manufacturer_id,
            "model": model,
            "enabled": bool(registered.get("enabled", True)),
            "device_compatibility": "not_evaluated",
            "physical_device_check_required": True,
        }, indent=2, ensure_ascii=False))
        return 0
    print(f"Registered '{connector_id}' for {manufacturer_id} {model}.")
    print("Restart gwmon.service through the approved deployment process to reload catalogs.")
    print("Device compatibility: NOT EVALUATED; mocked tests are not hardware proof.")
    return 0


def _add_validation_args(command: argparse.ArgumentParser) -> None:
    command.add_argument("--credentials", default=str(DEFAULT_CREDENTIALS))
    command.add_argument("--schema", default=str(DEFAULT_SCHEMA))


def _add_equipment_arg(command: argparse.ArgumentParser) -> None:
    command.add_argument("--equipment", default=str(DEFAULT_EQUIPMENT))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Homerouter connector builder")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="print or append a draft connector")
    create.add_argument("--id", required=True, help="stable connector id, e.g. vendor.model")
    create.add_argument("--display-name", required=True, help="human-readable name")
    create.add_argument(
        "--output",
        help="catalog file to append to; omit to print the scaffold to stdout",
    )
    create.set_defaults(func=cmd_create)

    validate = subparsers.add_parser("validate", help="validate catalog structure")
    validate.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    _add_validation_args(validate)
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(func=cmd_validate)

    validate_draft = subparsers.add_parser(
        "validate-draft", help="validate one generated or hand-written draft"
    )
    validate_draft.add_argument("draft", help="path to a connector draft JSON file")
    _add_validation_args(validate_draft)
    validate_draft.add_argument("--json", action="store_true")
    validate_draft.set_defaults(func=cmd_validate_draft)

    import_draft = subparsers.add_parser(
        "import-draft", help="explicitly append a reviewed draft to the catalog"
    )
    import_draft.add_argument("draft", help="path to a connector draft JSON file")
    import_draft.add_argument(
        "--approve", action="store_true",
        help="required explicit approval; the imported profile stays disabled",
    )
    import_draft.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    _add_validation_args(import_draft)
    import_draft.set_defaults(func=cmd_import_draft)

    listing = subparsers.add_parser("list", help="list catalog connectors")
    listing.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    _add_validation_args(listing)
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=cmd_list)

    auth_handlers = subparsers.add_parser(
        "auth-handlers", help="list trusted authentication handler identifiers"
    )
    auth_handlers.add_argument("--json", action="store_true")
    auth_handlers.set_defaults(func=cmd_auth_handlers)

    metrics = subparsers.add_parser("metrics", help="list UniversalModel metric ids")
    metrics.add_argument("--json", action="store_true")
    metrics.set_defaults(func=cmd_metrics)

    manufacturers = subparsers.add_parser(
        "manufacturers", help="list manufacturers and their constrained profile associations"
    )
    _add_equipment_arg(manufacturers)
    manufacturers.add_argument("--json", action="store_true")
    manufacturers.set_defaults(func=cmd_manufacturers)

    models = subparsers.add_parser("models", help="list connector profiles and registrations")
    models.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    _add_equipment_arg(models)
    models.add_argument("--json", action="store_true")
    models.set_defaults(func=cmd_models)

    configure = subparsers.add_parser(
        "configure", help="apply declarative endpoint/manufacturer settings to a draft"
    )
    configure.add_argument("draft", help="path to a connector draft JSON file")
    configure.add_argument("--manufacturer")
    configure.add_argument("--model")
    configure.add_argument("--firmware")
    configure.add_argument("--endpoint-id")
    configure.add_argument("--path")
    configure.add_argument("--method", default="GET")
    configure.add_argument("--scheme")
    configure.add_argument("--format", default="json")
    configure.add_argument("--timeout", type=float, default=5.0)
    configure.add_argument("--auth-handler")
    _add_validation_args(configure)
    configure.set_defaults(func=cmd_configure)

    mapping = subparsers.add_parser(
        "map", help="map one observed response field to a UniversalModel metric"
    )
    mapping.add_argument("draft", help="path to a connector draft JSON file")
    mapping.add_argument("--endpoint", required=True)
    mapping.add_argument("--field", required=True, help="JSON path such as $.Status.Model")
    mapping.add_argument("--metric", required=True)
    mapping.add_argument("--id")
    mapping.add_argument(
        "--availability-on-missing",
        default="unavailable",
        choices=["available", "stale", "unavailable", "unsupported", "invalid", "not_loaded", "error"],
    )
    _add_validation_args(mapping)
    mapping.set_defaults(func=cmd_map)

    inspect = subparsers.add_parser(
        "inspect", help="list response field paths from a sanitized offline fixture"
    )
    inspect.add_argument("profile", help="path to a connector draft or profile JSON file")
    inspect.add_argument("--fixture", required=True, help="sanitized offline fixture JSON")
    inspect.add_argument("--endpoint", help="inspect one unauthenticated endpoint")
    inspect.add_argument("--gateway", help="override the fixture's bare gateway host")
    _add_validation_args(inspect)
    inspect.add_argument("--json", action="store_true")
    inspect.set_defaults(func=cmd_inspect)

    test = subparsers.add_parser(
        "test", help="run a redacted, credential-free dry run against an offline fixture"
    )
    test.add_argument("profile", help="path to a connector draft or profile JSON file")
    test.add_argument("--fixture", required=True, help="sanitized offline fixture JSON")
    test.add_argument("--gateway", help="override the fixture's bare gateway host")
    _add_validation_args(test)
    test.add_argument("--json", action="store_true")
    test.set_defaults(func=cmd_test)

    register = subparsers.add_parser(
        "register", help="register a reviewed profile and its manufacturer association"
    )
    register.add_argument("profile", help="path to a connector draft or profile JSON file")
    register.add_argument("--manufacturer", required=True)
    register.add_argument("--model", required=True)
    register.add_argument("--manufacturer-name", help="display name when creating a manufacturer")
    register.add_argument("--firmware")
    register.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    _add_equipment_arg(register)
    register.add_argument(
        "--approve", action="store_true", help="required explicit approval after manual review"
    )
    register.add_argument(
        "--enable", action="store_true",
        help="enable only with observed status and verified manual evidence",
    )
    _add_validation_args(register)
    register.add_argument("--json", action="store_true")
    register.set_defaults(func=cmd_register)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
