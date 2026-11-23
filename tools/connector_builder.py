"""Connector Builder CLI.

    python -m tools.connector_builder create --id vendor.model --display-name "Vendor Model"
    python -m tools.connector_builder validate
    python -m tools.connector_builder list

``create`` prints a draft scaffold unless ``--output`` is supplied. Drafts are
explicitly marked as unobserved; no endpoint, credential, or value is treated
as evidence until it has been seen on a real device and the status is updated.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from connector_manager import (
    DEFAULT_CATALOG,
    DEFAULT_CREDENTIALS,
    DEFAULT_SCHEMA,
    ConnectorManager,
)


CONNECTOR_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:\.[a-z0-9_-]+)+$")


def build_scaffold(connector_id: str, display_name: str) -> Dict[str, Any]:
    """Return an inert, secret-free draft connector."""

    return {
        "id": connector_id,
        "display_name": display_name,
        "status": "draft",
        "enabled": False,
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
        "capabilities": ["device_identity"],
        "limitations": [
            "Draft scaffold is not device-ready and is disabled by default."
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

    for name, handler, help_text in (
        ("validate", cmd_validate, "validate catalog structure"),
        ("list", cmd_list, "list catalog connectors"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--catalog", default=str(DEFAULT_CATALOG))
        command.add_argument("--credentials", default=str(DEFAULT_CREDENTIALS))
        command.add_argument("--schema", default=str(DEFAULT_SCHEMA))
        if name == "validate":
            command.add_argument("--json", action="store_true")
        else:
            command.add_argument("--json", action="store_true")
        command.set_defaults(func=handler)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
