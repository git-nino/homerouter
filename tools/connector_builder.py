"""Connector Builder CLI.

    python -m tools.connector_builder create --id vendor.model --display-name "Vendor Model"
    python -m tools.connector_builder validate
    python -m tools.connector_builder validate-draft output/vendor.model.draft.json
    python -m tools.connector_builder import-draft output/vendor.model.draft.json --approve
    python -m tools.connector_builder list

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
import copy
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from connector_manager import (
    DEFAULT_CATALOG,
    DEFAULT_CREDENTIALS,
    DEFAULT_SCHEMA,
    ConnectorManager,
    ValidationIssue,
)


CONNECTOR_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:\.[a-z0-9_-]+)+$")


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


def _add_validation_args(command: argparse.ArgumentParser) -> None:
    command.add_argument("--credentials", default=str(DEFAULT_CREDENTIALS))
    command.add_argument("--schema", default=str(DEFAULT_SCHEMA))


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

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
