"""Connector catalog loading, validation, identification, and suggestions.

Phase 3 is deliberately inert: nothing here performs network I/O or changes the
live dashboard. The manager answers three questions:

1. Is the catalog *structurally* valid (schema + cross-checks)?
2. Which credential suggestion belongs to a connector, without storing secrets?
3. What identification confidence do observed responses support?

Structural validity is never presented as device compatibility. A JSON profile
can describe evidence; only a physical ONT comparison can prove compatibility.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from models.enums import Availability, Domain, ValueType
from models.universal import METRICS
from profile_safety import (
    UnsafeProfileRequest,
    validate_endpoint_path,
    validate_method,
    validate_port,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_CATALOG = ROOT / "config" / "connectors.json"
DEFAULT_CREDENTIALS = ROOT / "config" / "default_credentials.json"
DEFAULT_SCHEMA = ROOT / "config" / "connector.schema.json"
DEFAULT_EQUIPMENT = ROOT / "config" / "equipment.json"
CONNECTOR_ID_RE = re.compile(r"^[a-z0-9]+(?:\.[a-z0-9_-]+)+$")

EXTRACTION_METHODS = {
    "json_path", "json_path_multi", "regex", "header",
    "literal", "list_length", "list_objects", "keyed_objects",
    "js_constructor_objects",
}
SCHEMES = {"http", "https"}
BODY_FORMATS = {"form", "json"}
CREDENTIAL_FIELDS = {
    "key", "default_username", "default_password",
    "requires_user_confirmation", "provenance",
}
SUPPORTED_TRANSFORMS = {
    "strip",
    "decode_point_encoded_ip",
    "decode_hex_escapes",
    "enabled_flag",
    "integer",
    "number",
    "split_before_at",
    "voip_status_label",
    "used_percent",
    "scale",
    "first",
}
CONDITION_OPERATORS = {"eq", "equals", "ne", "not_equals", "contains", "prefix", "regex"}
ID_OPERATORS = {"equals", "not_equals", "contains", "prefix", "regex"}
AVAILABILITY_VALUES = {item.value for item in Availability}
DOMAIN_VALUES = {item.value for item in Domain}
VALUE_TYPE_VALUES = {item.value for item in ValueType}
CONFIDENCE_RANK = {"unknown": 0, "partial": 1, "exact": 2}
SECRET_KEYS = {
    "password", "loginpd", "secret", "token", "api_key", "apikey", "credential",
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "session", "sessionid", "csrf", "csrf_token", "xsrf", "nonce",
    "phonenumber", "phone", "mobile", "msisdn", "telephone", "sipuser",
    "serial", "serialnumber", "sn", "gponsn",
}
_AUTH_VALUE_RE = re.compile(r"\b(?:Basic|Bearer|Digest|Token)\s+[A-Za-z0-9._~+/=-]{6,}", re.I)
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
                  "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24",
                  "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/4",
                  "240.0.0.0/4", "255.255.255.255/32", "::1/128")
)


class ConnectorError(Exception):
    """Raised when a catalog file cannot be read."""


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    path: str
    message: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }


@dataclass
class ValidationReport:
    connector_count: int = 0
    issues: List[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> List[ValidationIssue]:
        return [item for item in self.issues if item.severity == "error"]

    @property
    def warnings(self) -> List[ValidationIssue]:
        return [item for item in self.issues if item.severity == "warning"]

    @property
    def structural_valid(self) -> bool:
        return not self.errors

    def add(self, severity: str, code: str, path: str, message: str) -> None:
        self.issues.append(ValidationIssue(severity, code, path, message))

    def extend(self, issues: Iterable[ValidationIssue]) -> None:
        self.issues.extend(issues)

    @staticmethod
    def compatibility() -> Dict[str, Any]:
        return {
            "status": "not_evaluated",
            "reason": (
                "Profile structure does not prove compatibility with a physical "
                "ONT. Identify the device from observed responses, then compare "
                "normalized output against the legacy path before claiming support."
            ),
            "physical_device_check_required": True,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "structural_valid": self.structural_valid,
            "structural": {
                "valid": self.structural_valid,
                "connector_count": self.connector_count,
                "errors": len(self.errors),
                "warnings": len(self.warnings),
                "issues": [item.to_dict() for item in self.issues],
            },
            "device_compatibility": self.compatibility(),
        }


def _load_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConnectorError(f"Cannot read {path}: {exc}") from exc
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ConnectorError(f"Invalid JSON in {path}: {exc}") from exc


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not any(address in network for network in _PRIVATE_NETWORKS)


def _hashable(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((key, _hashable(item)) for key, item in value.items()))
    if isinstance(value, list):
        return tuple(_hashable(item) for item in value)
    return value


class SchemaValidator:
    """Small JSON-Schema interpreter for the subset used by our own schema.

    Keeping this in-house avoids adding a runtime dependency to Termux while
    still producing field-level error paths. It supports the keywords present
    in ``connector.schema.json`` only.
    """

    def __init__(self, schema: Dict[str, Any]):
        self.schema = schema

    def validate(self, instance: Any) -> List[ValidationIssue]:
        return self._walk(instance, self.schema, "$")

    def _resolve(self, ref: str) -> Dict[str, Any]:
        if not ref.startswith("#/"):
            return {}
        node: Any = self.schema
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                return {}
            node = node[part]
        return node if isinstance(node, dict) else {}

    @staticmethod
    def _type_ok(value: Any, expected: str) -> bool:
        if expected == "object":
            return isinstance(value, dict)
        if expected == "array":
            return isinstance(value, list)
        if expected == "string":
            return isinstance(value, str)
        if expected == "boolean":
            return isinstance(value, bool)
        if expected == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if expected == "null":
            return value is None
        return True

    def _walk(self, value: Any, schema: Dict[str, Any], path: str) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        if not schema:
            return issues

        if "$ref" in schema:
            return self._walk(value, self._resolve(schema["$ref"]), path)

        expected = schema.get("type")
        if expected is not None:
            expected_types = expected if isinstance(expected, list) else [expected]
            if not any(self._type_ok(value, item) for item in expected_types):
                issues.append(ValidationIssue(
                    "error", "type", path,
                    f"expected {' or '.join(str(item) for item in expected_types)}, "
                    f"got {type(value).__name__}",
                ))
                return issues

        if "const" in schema and value != schema["const"]:
            issues.append(ValidationIssue(
                "error", "const", path, f"expected {schema['const']!r}",
            ))
        if "enum" in schema and value not in schema["enum"]:
            issues.append(ValidationIssue(
                "error", "enum", path, f"value {value!r} is not in {schema['enum']!r}",
            ))

        if isinstance(value, str):
            if len(value) < schema.get("minLength", 0):
                issues.append(ValidationIssue(
                    "error", "minLength", path,
                    f"must contain at least {schema['minLength']} characters",
                ))
            pattern = schema.get("pattern")
            if pattern and re.search(pattern, value) is None:
                issues.append(ValidationIssue(
                    "error", "pattern", path, f"does not match {pattern}",
                ))

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            for keyword, label in (("minimum", ">="), ("maximum", "<=")):
                if keyword in schema and not (
                    value >= schema[keyword] if keyword == "minimum" else value <= schema[keyword]
                ):
                    issues.append(ValidationIssue(
                        "error", keyword, path, f"must be {label} {schema[keyword]}",
                    ))
            if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
                issues.append(ValidationIssue(
                    "error", "exclusiveMinimum", path,
                    f"must be > {schema['exclusiveMinimum']}",
                ))

        if isinstance(value, list):
            if len(value) < schema.get("minItems", 0):
                issues.append(ValidationIssue(
                    "error", "minItems", path,
                    f"must contain at least {schema['minItems']} item(s)",
                ))
            if schema.get("uniqueItems"):
                rendered = [_hashable(item) for item in value]
                if len(set(rendered)) != len(rendered):
                    issues.append(ValidationIssue(
                        "error", "uniqueItems", path, "items must be unique",
                    ))
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(value):
                    issues.extend(self._walk(item, item_schema, f"{path}[{index}]"))

        if isinstance(value, dict):
            if len(value) < schema.get("minProperties", 0):
                issues.append(ValidationIssue(
                    "error", "minProperties", path,
                    f"must contain at least {schema['minProperties']} property/properties",
                ))
            properties = _as_dict(schema.get("properties"))
            for key in schema.get("required", []):
                if key not in value:
                    issues.append(ValidationIssue(
                        "error", "required", path, f"missing required field '{key}'",
                    ))
            additional = schema.get("additionalProperties", True)
            for key, child in value.items():
                child_path = f"{path}.{key}"
                if key in properties:
                    issues.extend(self._walk(child, properties[key], child_path))
                elif additional is False:
                    issues.append(ValidationIssue(
                        "error", "additionalProperties", child_path,
                        f"field '{key}' is not allowed",
                    ))
                elif isinstance(additional, dict):
                    issues.extend(self._walk(child, additional, child_path))

        return issues


def _dig(data: Any, field_path: str) -> Any:
    node = data
    for part in field_path.split("."):
        match = re.match(r"^(.+)\[(\d+)\]$", part)
        if match:
            key, index = match.group(1), int(match.group(2))
            if not isinstance(node, dict) or key not in node:
                return None
            node = node[key]
            if not isinstance(node, list) or index >= len(node):
                return None
            node = node[index]
            continue
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _compare(value: Any, operator: str, expected: Any) -> bool:
    actual_text = str(value)
    expected_text = str(expected)
    if operator == "equals":
        return actual_text == expected_text
    if operator == "not_equals":
        return actual_text != expected_text
    if operator == "contains":
        return expected_text in actual_text
    if operator == "prefix":
        return actual_text.startswith(expected_text)
    if operator == "regex":
        try:
            return re.search(expected_text, actual_text) is not None
        except re.error:
            return False
    return False


def _endpoint_ids(connector: Dict[str, Any]) -> Set[str]:
    return {
        endpoint.get("id")
        for endpoint in _as_list(connector.get("endpoints"))
        if isinstance(endpoint, dict) and isinstance(endpoint.get("id"), str)
    }


class ConnectorManager:
    """Load, validate, list, and interpret connector catalog files."""

    def __init__(
        self,
        catalog_path: Optional[Path] = None,
        credentials_path: Optional[Path] = None,
        schema_path: Optional[Path] = None,
        equipment_path: Optional[Path] = None,
    ):
        self.catalog_path = Path(catalog_path) if catalog_path else DEFAULT_CATALOG
        self.credentials_path = Path(credentials_path) if credentials_path else DEFAULT_CREDENTIALS
        self.schema_path = Path(schema_path) if schema_path else DEFAULT_SCHEMA
        self.equipment_path = Path(equipment_path) if equipment_path else DEFAULT_EQUIPMENT
        # A temporary connector catalog (used by offline tools and tests) must
        # not be judged against the shipped equipment associations. Validate
        # cross-file references only for the live catalog or when a caller
        # explicitly supplies the equipment catalog to check.
        self.equipment_validation_enabled = (
            equipment_path is not None
            or self.catalog_path.resolve() == DEFAULT_CATALOG.resolve()
        )
        self.catalog = _load_json(self.catalog_path)
        self._loaded_catalog = self.catalog
        self.credentials = _load_json(self.credentials_path)
        self.schema = _load_json(self.schema_path)
        if not isinstance(self.schema, dict):
            raise ConnectorError(f"Schema in {self.schema_path} must be a JSON object")
        # The equipment catalog is optional. Runtime loading is soft-failing in
        # app.py; here an unreadable file is reported by validate() instead of
        # preventing the manager from loading a valid connector catalog.
        self.equipment_error: Optional[str] = None
        try:
            self.equipment = (
                _load_json(self.equipment_path)
                if self.equipment_path.exists() else {}
            )
        except ConnectorError as exc:
            self.equipment = None
            self.equipment_error = str(exc)

    @property
    def connectors(self) -> List[Dict[str, Any]]:
        return [
            item for item in _as_dict(self.catalog).get("connectors", [])
            if isinstance(item, dict)
        ]

    def get(self, connector_id: str) -> Optional[Dict[str, Any]]:
        return next(
            (item for item in self.connectors if item.get("id") == connector_id),
            None,
        )

    def enabled_connectors(self) -> List[Dict[str, Any]]:
        return [item for item in self.connectors if item.get("enabled", True)]

    def connectors_for_manufacturer(
        self, manufacturer_id: str, profile_ids: Iterable[Any]
    ) -> List[Dict[str, Any]]:
        """Return enabled profiles associated with one manufacturer.

        ``profile_ids`` comes from the equipment catalog and is authoritative for
        membership and order. Optional profile metadata is a consistency guard:
        when a profile declares a manufacturer it must agree with the selection.
        A profile with no metadata remains eligible for legacy catalogs.
        """

        ident = str(manufacturer_id or "").strip()
        if not ident:
            return self.enabled_connectors()
        by_id = {
            connector.get("id"): connector
            for connector in self.enabled_connectors()
            if isinstance(connector.get("id"), str)
        }
        result: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for item in profile_ids:
            if not isinstance(item, str):
                continue
            profile_id = item.strip()
            if not profile_id or profile_id in seen:
                continue
            seen.add(profile_id)
            connector = by_id.get(profile_id)
            if connector is None:
                continue
            declared = connector.get("manufacturer_id")
            if declared is not None and str(declared).strip() != ident:
                continue
            result.append(connector)
        return result

    def credential_suggestion(self, key: str) -> Optional[Dict[str, Any]]:
        suggestions = _as_dict(self.credentials).get("credential_suggestions", [])
        return next(
            (item for item in _as_list(suggestions)
             if isinstance(item, dict) and item.get("key") == key),
            None,
        )

    def list_connectors(self) -> List[Dict[str, Any]]:
        rows = []
        for connector in self.connectors:
            identification = _as_dict(connector.get("identification"))
            rows.append({
                "id": connector.get("id", ""),
                "display_name": connector.get("display_name", ""),
                "status": connector.get("status", ""),
                "enabled": connector.get("enabled", True),
                "identification_confidence": identification.get("confidence", "unknown"),
                "capabilities": list(_as_list(connector.get("capabilities"))),
                "device_compatibility": "not_evaluated",
            })
        return rows

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def validate(self) -> ValidationReport:
        report = ValidationReport(connector_count=len(self.connectors))
        report.extend(SchemaValidator(self.schema).validate(self.catalog))
        report.extend(self._validate_catalog())
        report.extend(self._validate_credentials())
        if self.catalog is self._loaded_catalog:
            report.extend(self._validate_equipment())
        return report

    def _validate_catalog(self) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        if not isinstance(_as_dict(self.catalog).get("schema_version"), int):
            issues.append(ValidationIssue(
                "error", "schema_version", "$.schema_version",
                "schema_version must be the integer 1",
            ))

        seen_ids: Set[str] = set()
        seen_signatures: Dict[Tuple[Any, ...], str] = {}
        exact_rules: List[Tuple[int, str, Dict[str, Any]]] = []
        all_ids = {
            item.get("id")
            for item in self.connectors
            if isinstance(item.get("id"), str)
        }
        credential_keys = {
            item.get("key")
            for item in _as_list(_as_dict(self.credentials).get("credential_suggestions"))
            if isinstance(item, dict)
        }

        for index, connector in enumerate(self.connectors):
            base = f"$.connectors[{index}]"
            connector_id = connector.get("id") or f"#{index}"
            if connector_id in seen_ids:
                issues.append(ValidationIssue(
                    "error", "duplicate_connector_id", f"{base}.id",
                    f"connector id '{connector_id}' is used more than once",
                ))
            seen_ids.add(connector_id)

            issues.extend(self._scan_secrets(connector, base))
            issues.extend(self._validate_evidence(connector, base))
            issues.extend(self._validate_identification(
                connector, base, connector_id, seen_signatures, exact_rules, all_ids,
                index,
            ))
            issues.extend(self._validate_auth(connector, base, credential_keys))
            issues.extend(self._validate_endpoint_runtime(connector, base))
            issues.extend(self._validate_extraction(connector, base))
            issues.extend(self._validate_validation_rules(connector, base))
            issues.extend(self._validate_dependencies(connector, base))
            issues.extend(self._validate_draft_metadata(connector, base))

            if connector.get("enabled", True) is False:
                issues.append(ValidationIssue(
                    "warning", "connector_disabled", f"{base}.enabled",
                    f"connector '{connector_id}' is disabled",
                ))

        issues.extend(self._detect_exact_conflicts(exact_rules))
        return issues

    def _validate_equipment(self) -> List[ValidationIssue]:
        """Check optional manufacturer -> connector profile associations."""

        issues: List[ValidationIssue] = []
        if not self.equipment_validation_enabled:
            return issues
        if self.equipment_error:
            return [ValidationIssue(
                "error", "equipment_catalog_unreadable", "$",
                f"equipment catalog could not be read: {self.equipment_error}",
            )]
        equipment = self.equipment
        if not isinstance(equipment, dict):
            return [ValidationIssue(
                "error", "equipment_catalog_invalid", "$",
                "equipment catalog must be a JSON object",
            )]

        if equipment.get("schema_version") not in (1, "1", "1.0.0"):
            issues.append(ValidationIssue(
                "error", "equipment_schema_version", "$.schema_version",
                "equipment schema_version must be 1",
            ))
        entries = equipment.get("manufacturers")
        if not isinstance(entries, list):
            issues.append(ValidationIssue(
                "error", "equipment_manufacturers", "$.manufacturers",
                "equipment catalog must contain a manufacturers array",
            ))
            return issues

        seen_manufacturers: Set[str] = set()
        for index, entry in enumerate(entries):
            base = f"$.manufacturers[{index}]"
            if not isinstance(entry, dict):
                issues.append(ValidationIssue(
                    "error", "equipment_manufacturer_entry", base,
                    "manufacturer entries must be objects",
                ))
                continue
            ident = str(entry.get("id") or "").strip()
            name = str(entry.get("name") or "").strip()
            if not ident:
                issues.append(ValidationIssue(
                    "error", "equipment_manufacturer_id", f"{base}.id",
                    "manufacturer id must be a non-empty string",
                ))
            if not name:
                issues.append(ValidationIssue(
                    "error", "equipment_manufacturer_name", f"{base}.name",
                    "manufacturer name must be a non-empty string",
                ))
            if ident in seen_manufacturers:
                issues.append(ValidationIssue(
                    "error", "duplicate_manufacturer_id", f"{base}.id",
                    f"manufacturer id '{ident}' is used more than once",
                ))
            seen_manufacturers.add(ident)

            profiles = entry.get("profiles", [])
            if profiles is None:
                profiles = []
            if not isinstance(profiles, list):
                issues.append(ValidationIssue(
                    "error", "equipment_profiles", f"{base}.profiles",
                    "manufacturer profiles must be an array when present",
                ))
                continue
            seen_profiles: Set[str] = set()
            for profile_index, reference in enumerate(profiles):
                path = f"{base}.profiles[{profile_index}]"
                profile_id = (
                    str(reference).strip() if isinstance(reference, str) else ""
                )
                if not profile_id or not CONNECTOR_ID_RE.fullmatch(profile_id):
                    issues.append(ValidationIssue(
                        "error", "invalid_profile_reference", path,
                        "profile references must be valid connector IDs",
                    ))
                    continue
                if profile_id in seen_profiles:
                    issues.append(ValidationIssue(
                        "error", "duplicate_profile_reference", path,
                        f"connector id '{profile_id}' is referenced more than once",
                    ))
                    continue
                seen_profiles.add(profile_id)

                connector = self.get(profile_id)
                if connector is None:
                    issues.append(ValidationIssue(
                        "error", "unknown_profile_reference", path,
                        f"connector id '{profile_id}' is not in the catalog",
                    ))
                    continue
                if connector.get("enabled", True) is False:
                    issues.append(ValidationIssue(
                        "warning", "disabled_profile_reference", path,
                        f"connector '{profile_id}' is disabled and will not be probed",
                    ))
                declared = connector.get("manufacturer_id")
                if declared is not None and str(declared).strip() != ident:
                    issues.append(ValidationIssue(
                        "error", "profile_manufacturer_mismatch", path,
                        (
                            f"connector '{profile_id}' declares manufacturer "
                            f"'{str(declared).strip()}' but is linked to '{ident}'"
                        ),
                    ))
        return issues

    def _scan_secrets(self, node: Any, path: str) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        if isinstance(node, dict):
            for key, value in node.items():
                child = f"{path}.{key}"
                if (
                    str(key).lower() in SECRET_KEYS
                    and isinstance(value, str)
                    and value.strip()
                    and not value.startswith("{")
                    and not value.startswith("<")
                ):
                    issues.append(ValidationIssue(
                        "error", "secret_value_in_catalog", child,
                        "connector files may reference secrets but must never contain them",
                    ))
                if isinstance(value, str) and not value.startswith("<redacted-"):
                    if _AUTH_VALUE_RE.search(value):
                        issues.append(ValidationIssue(
                            "error", "authorization_value_in_catalog", child,
                            "Authorization header values must be redacted placeholders",
                        ))
                    if any(_is_public_ip(match.group(0)) for match in _IPV4_RE.finditer(value)):
                        issues.append(ValidationIssue(
                            "error", "public_ip_in_catalog", child,
                            "public IP addresses must be redacted in connector catalogs",
                        ))
                    if _MAC_RE.search(value):
                        issues.append(ValidationIssue(
                            "error", "mac_address_in_catalog", child,
                            "MAC addresses must be redacted in connector catalogs",
                        ))
                issues.extend(self._scan_secrets(value, child))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                issues.extend(self._scan_secrets(value, f"{path}[{index}]"))
        return issues

    def _validate_evidence(self, connector: Dict[str, Any], base: str) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        status = connector.get("status")
        evidence = _as_dict(connector.get("evidence"))
        if status == "verified" and evidence.get("verified") is not True:
            issues.append(ValidationIssue(
                "error", "verified_status_without_evidence", f"{base}.status",
                "status 'verified' requires evidence.verified to be true",
            ))
        if status == "draft":
            issues.append(ValidationIssue(
                "warning", "draft_connector", f"{base}.status",
                "draft profiles contain placeholders and are not device-ready",
            ))
        return issues

    def _validate_identification(
        self,
        connector: Dict[str, Any],
        base: str,
        connector_id: str,
        seen_signatures: Dict[Tuple[Any, ...], str],
        exact_rules: List[Tuple[int, str, Dict[str, Any]]],
        all_ids: Set[str],
        connector_index: int,
    ) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        identification = _as_dict(connector.get("identification"))
        declared = identification.get("confidence")
        rules = _as_list(identification.get("rules"))
        best_rank = 0
        path = f"{base}.identification"

        for conf in _as_list(identification.get("conflicts_with")):
            if isinstance(conf, str) and conf not in all_ids and conf != connector_id:
                issues.append(ValidationIssue(
                    "warning", "unknown_conflict_reference", f"{path}.conflicts_with",
                    f"references unknown connector '{conf}'",
                ))

        for index, rule in enumerate(rules):
            if not isinstance(rule, dict):
                continue
            rule_path = f"{path}.rules[{index}]"
            confidence = rule.get("confidence", "unknown")
            best_rank = max(best_rank, CONFIDENCE_RANK.get(confidence, 0))
            signature = (
                rule.get("phase"), rule.get("source"), rule.get("field"),
                rule.get("operator"), str(rule.get("value")),
            )
            if signature in seen_signatures:
                issues.append(ValidationIssue(
                    "warning", "duplicate_identification_signature", rule_path,
                    "identification rule duplicates "
                    f"'{seen_signatures[signature]}' on another connector",
                ))
            else:
                seen_signatures[signature] = connector_id
            if rule.get("operator") not in ID_OPERATORS:
                issues.append(ValidationIssue(
                    "error", "unknown_identification_operator", f"{rule_path}.operator",
                    f"unsupported operator {rule.get('operator')!r}",
                ))
            if confidence == "exact":
                exact_rules.append((connector_index, connector_id, rule))

        if declared == "exact" and best_rank < CONFIDENCE_RANK["exact"]:
            issues.append(ValidationIssue(
                "error", "overclaimed_identification_confidence", f"{path}.confidence",
                "connector claims exact identification but no rule has exact confidence",
            ))
        if declared == "partial" and best_rank > CONFIDENCE_RANK["partial"]:
            issues.append(ValidationIssue(
                "warning", "understated_identification_confidence", f"{path}.confidence",
                "exact rule present but connector declares partial confidence",
            ))
        return issues

    def _detect_exact_conflicts(
        self, exact_rules: Sequence[Tuple[int, str, Dict[str, Any]]]
    ) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        for first in range(len(exact_rules)):
            for second in range(first + 1, len(exact_rules)):
                first_index, first_id, first_rule = exact_rules[first]
                second_index, second_id, second_rule = exact_rules[second]
                if first_rule.get("phase") != second_rule.get("phase"):
                    continue
                if first_rule.get("source") != second_rule.get("source"):
                    continue
                if first_rule.get("field") != second_rule.get("field"):
                    continue
                same_value = (
                    first_rule.get("operator") == second_rule.get("operator")
                    and str(first_rule.get("value")) == str(second_rule.get("value"))
                )
                different_exact_values = (
                    first_rule.get("operator") in ("equals", "prefix")
                    and second_rule.get("operator") in ("equals", "prefix")
                    and str(first_rule.get("value")) != str(second_rule.get("value"))
                )
                if same_value or different_exact_values:
                    issues.append(ValidationIssue(
                        "error", "conflicting_identification_rules",
                        f"$.connectors[{first_index}].identification.rules",
                        f"exact rules for '{first_rule.get('field')}' conflict between "
                        f"'{first_id}' and '{second_id}'",
                    ))
        return issues

    def _validate_auth(
        self,
        connector: Dict[str, Any],
        base: str,
        credential_keys: Set[Any],
    ) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        auth = _as_dict(connector.get("auth"))
        key = auth.get("credential_suggestion_key")
        if key and key not in credential_keys:
            issues.append(ValidationIssue(
                "error", "unknown_credential_suggestion", f"{base}.auth.credential_suggestion_key",
                f"no credential suggestion named '{key}'",
            ))

        endpoint_ids = _endpoint_ids(connector)
        logout_endpoint = auth.get("logout_endpoint")
        if logout_endpoint and logout_endpoint not in endpoint_ids:
            issues.append(ValidationIssue(
                "error", "unknown_logout_endpoint", f"{base}.auth.logout_endpoint",
                f"references unknown endpoint '{logout_endpoint}'",
            ))
        seen_flow_ids: Set[str] = set()
        seen_orders: Set[int] = set()
        for index, flow in enumerate(_as_list(auth.get("flows"))):
            if not isinstance(flow, dict):
                continue
            flow_path = f"{base}.auth.flows[{index}]"
            flow_id = flow.get("id")
            if flow_id in seen_flow_ids:
                issues.append(ValidationIssue(
                    "error", "duplicate_auth_flow_id", f"{flow_path}.id",
                    f"auth flow id '{flow_id}' is used more than once",
                ))
            seen_flow_ids.add(flow_id)
            order = flow.get("order")
            if order in seen_orders:
                issues.append(ValidationIssue(
                    "error", "duplicate_auth_order", f"{flow_path}.order",
                    f"auth order {order} is used more than once",
                ))
            seen_orders.add(order)
            for name, endpoint_id in _as_dict(flow.get("endpoints")).items():
                if endpoint_id not in endpoint_ids:
                    issues.append(ValidationIssue(
                        "error", "unknown_auth_endpoint", f"{flow_path}.endpoints.{name}",
                        f"references unknown endpoint '{endpoint_id}'",
                    ))
        return issues

    def _validate_endpoint_runtime(
        self, connector: Dict[str, Any], base: str
    ) -> List[ValidationIssue]:
        """Reject profile options the live executor cannot honour."""

        issues: List[ValidationIssue] = []
        default_scheme = connector.get("scheme", "http")
        if default_scheme not in SCHEMES:
            issues.append(ValidationIssue(
                "error", "unsupported_scheme", f"{base}.scheme",
                f"unsupported scheme {default_scheme!r}; use http or https",
            ))

        for index, endpoint in enumerate(_as_list(connector.get("endpoints"))):
            if not isinstance(endpoint, dict):
                continue
            path = f"{base}.endpoints[{index}]"
            try:
                validate_method(endpoint.get("method", "GET"))
            except UnsafeProfileRequest as exc:
                issues.append(ValidationIssue(
                    "error", "unsafe_endpoint_method", f"{path}.method", str(exc)
                ))
            try:
                validate_endpoint_path(endpoint.get("path"))
            except UnsafeProfileRequest as exc:
                issues.append(ValidationIssue(
                    "error", "unsafe_endpoint_path", f"{path}.path", str(exc)
                ))
            scheme = endpoint.get("scheme", default_scheme)
            if scheme not in SCHEMES:
                issues.append(ValidationIssue(
                    "error", "unsupported_scheme", f"{path}.scheme",
                    f"unsupported scheme {scheme!r}; use http or https",
                ))
            body_format = endpoint.get("body_format", "form")
            if body_format not in BODY_FORMATS:
                issues.append(ValidationIssue(
                    "error", "unsupported_body_format", f"{path}.body_format",
                    f"unsupported body format {body_format!r}; use form or json",
                ))
            if endpoint.get("method") == "POST" and body_format == "json" and not isinstance(endpoint.get("body"), dict):
                issues.append(ValidationIssue(
                    "error", "missing_json_body", f"{path}.body",
                    "a JSON POST endpoint must declare an object body",
                ))
            try:
                validate_port(endpoint.get("port"))
            except UnsafeProfileRequest as exc:
                issues.append(ValidationIssue(
                    "error", "invalid_endpoint_port", f"{path}.port", str(exc)
                ))
            if "verify_tls" in endpoint and not isinstance(endpoint.get("verify_tls"), bool):
                issues.append(ValidationIssue(
                    "error", "invalid_verify_tls", f"{path}.verify_tls",
                    "verify_tls must be a boolean when present",
                ))
        return issues

    def _validate_draft_metadata(
        self, connector: Dict[str, Any], base: str
    ) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        if connector.get("status") == "draft":
            if connector.get("enabled") is True:
                issues.append(ValidationIssue(
                    "error", "draft_must_be_disabled", f"{base}.enabled",
                    "draft profiles must be disabled until manually approved",
                ))
            if not _as_list(connector.get("manual_review")):
                issues.append(ValidationIssue(
                    "warning", "draft_needs_manual_review", f"{base}.manual_review",
                    "draft profiles should list the observations a developer must verify",
                ))
        return issues

    def _validate_extraction(self, connector: Dict[str, Any], base: str) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        endpoint_ids = _endpoint_ids(connector)
        seen_rule_ids: Set[str] = set()
        capabilities = set(_as_list(connector.get("capabilities")))

        for index, rule in enumerate(_as_list(connector.get("extraction"))):
            if not isinstance(rule, dict):
                continue
            path = f"{base}.extraction[{index}]"
            rule_id = rule.get("id")
            if rule_id in seen_rule_ids:
                issues.append(ValidationIssue(
                    "error", "duplicate_extraction_id", f"{path}.id",
                    f"extraction id '{rule_id}' is used more than once",
                ))
            seen_rule_ids.add(rule_id)

            method = rule.get("method")
            if method not in EXTRACTION_METHODS:
                issues.append(ValidationIssue(
                    "error", "unknown_extraction_method", f"{path}.method",
                    f"unsupported extraction method {method!r}",
                ))
            metric = rule.get("metric")
            collection = rule.get("collection")
            if not metric and not collection:
                issues.append(ValidationIssue(
                    "error", "missing_extraction_target", path,
                    "an extraction rule needs either 'metric' or 'collection'",
                ))
            if metric and collection:
                issues.append(ValidationIssue(
                    "error", "ambiguous_extraction_target", path,
                    "an extraction rule cannot target both a metric and a collection",
                ))
            if metric and metric not in METRICS:
                issues.append(ValidationIssue(
                    "error", "unknown_metric", f"{path}.metric",
                    f"metric '{metric}' is not in the universal model",
                ))
            elif metric:
                domain = METRICS[metric].domain.value
                if domain not in capabilities:
                    issues.append(ValidationIssue(
                        "error", "missing_capability", f"{path}.metric",
                        f"metric '{metric}' needs capability '{domain}'",
                    ))
            if collection in ("devices", "lan_ports", "ssids"):
                required_domain = (
                    "connected_devices" if collection == "devices" else "system"
                )
                if required_domain not in capabilities:
                    issues.append(ValidationIssue(
                        "error", "missing_capability", f"{path}.collection",
                        f"collection '{collection}' needs capability '{required_domain}'",
                    ))
            if method in ("list_objects", "keyed_objects") and not _as_dict(rule.get("item_fields")):
                issues.append(ValidationIssue(
                    "error", "missing_item_fields", f"{path}.item_fields",
                    f"{method} requires an item_fields map",
                ))
            endpoint_id = rule.get("endpoint")
            if endpoint_id not in endpoint_ids:
                issues.append(ValidationIssue(
                    "error", "unknown_extraction_endpoint", f"{path}.endpoint",
                    f"references unknown endpoint '{endpoint_id}'",
                ))
            value_type = rule.get("value_type")
            if value_type is not None and value_type not in VALUE_TYPE_VALUES:
                issues.append(ValidationIssue(
                    "error", "unknown_value_type", f"{path}.value_type",
                    f"unsupported value type {value_type!r}",
                ))
            availability = rule.get("availability_on_missing")
            if availability is not None and availability not in AVAILABILITY_VALUES:
                issues.append(ValidationIssue(
                    "error", "unknown_availability_state", f"{path}.availability_on_missing",
                    f"unsupported availability state {availability!r}",
                ))
            for transform_index, transform in enumerate(_as_list(rule.get("transforms"))):
                if transform not in SUPPORTED_TRANSFORMS:
                    issues.append(ValidationIssue(
                        "error", "unknown_transform",
                        f"{path}.transforms[{transform_index}]",
                        f"unsupported transform {transform!r}",
                    ))
            for transform_name in _as_dict(rule.get("transform_options")):
                if transform_name not in SUPPORTED_TRANSFORMS:
                    issues.append(ValidationIssue(
                        "error", "unknown_transform", f"{path}.transform_options",
                        f"options supplied for unsupported transform {transform_name!r}",
                    ))
            conditions: List[Tuple[str, Dict[str, Any]]] = []
            when = _as_dict(rule.get("when"))
            if when:
                conditions.append((f"{path}.when", when))
            item_filter = _as_dict(rule.get("filter"))
            if item_filter:
                conditions.append((f"{path}.filter", item_filter))
            for condition_index, condition in enumerate(_as_list(rule.get("when_all"))):
                if isinstance(condition, dict):
                    conditions.append(
                        (f"{path}.when_all[{condition_index}]", condition)
                    )
            for condition_path, condition in conditions:
                if condition.get("operator") not in CONDITION_OPERATORS:
                    issues.append(ValidationIssue(
                        "error", "unknown_condition_operator", f"{condition_path}.operator",
                        f"unsupported condition operator {condition.get('operator')!r}",
                    ))
        return issues

    def _validate_validation_rules(
        self, connector: Dict[str, Any], base: str
    ) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        for index, rule in enumerate(_as_list(connector.get("validation"))):
            if not isinstance(rule, dict):
                continue
            path = f"{base}.validation[{index}]"
            metric = rule.get("metric")
            if metric not in METRICS:
                issues.append(ValidationIssue(
                    "error", "unknown_metric", f"{path}.metric",
                    f"metric '{metric}' is not in the universal model",
                ))
            availability = rule.get("on_failure")
            if availability is not None and availability not in AVAILABILITY_VALUES:
                issues.append(ValidationIssue(
                    "error", "unknown_availability_state", f"{path}.on_failure",
                    f"unsupported availability state {availability!r}",
                ))
            options = _as_dict(rule.get("options"))
            if rule.get("rule") == "one_of" and not isinstance(options.get("values"), list):
                issues.append(ValidationIssue(
                    "error", "invalid_validation_options", f"{path}.options",
                    "one_of requires an options.values array",
                ))
        return issues

    def _validate_dependencies(self, connector: Dict[str, Any], base: str) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        endpoint_ids = _endpoint_ids(connector)
        graph: Dict[str, List[str]] = {
            endpoint_id: [] for endpoint_id in endpoint_ids
        }
        for endpoint_id, dependencies in _as_dict(connector.get("dependencies")).items():
            if endpoint_id not in endpoint_ids:
                issues.append(ValidationIssue(
                    "error", "unknown_dependency_endpoint",
                    f"{base}.dependencies.{endpoint_id}",
                    f"unknown endpoint '{endpoint_id}'",
                ))
            graph.setdefault(endpoint_id, [])
            for dependency in _as_list(dependencies):
                if dependency not in endpoint_ids:
                    issues.append(ValidationIssue(
                        "error", "unknown_dependency", f"{base}.dependencies.{endpoint_id}",
                        f"references unknown endpoint '{dependency}'",
                    ))
                graph[endpoint_id].append(dependency)

        visiting: Set[str] = set()
        visited: Set[str] = set()

        def visit(node: str, trail: Sequence[str]) -> None:
            if node in visiting:
                cycle = " -> ".join(list(trail) + [node])
                issues.append(ValidationIssue(
                    "error", "dependency_cycle", f"{base}.dependencies",
                    f"dependency cycle detected: {cycle}",
                ))
                return
            if node in visited:
                return
            visiting.add(node)
            for dependency in graph.get(node, []):
                visit(dependency, list(trail) + [node])
            visiting.discard(node)
            visited.add(node)

        for endpoint_id in graph:
            visit(endpoint_id, [])
        return issues

    def _validate_credentials(self) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        data = _as_dict(self.credentials)
        if not isinstance(data.get("schema_version"), int):
            issues.append(ValidationIssue(
                "error", "schema_version", "$.credentials.schema_version",
                "credential catalog schema_version must be the integer 1",
            ))
        suggestions = _as_list(data.get("credential_suggestions"))
        if not suggestions:
            issues.append(ValidationIssue(
                "error", "no_credential_suggestions", "$.credentials.credential_suggestions",
                "at least one safe credential suggestion is required",
            ))
        seen: Set[str] = set()
        for index, item in enumerate(suggestions):
            path = f"$.credentials.credential_suggestions[{index}]"
            if not isinstance(item, dict):
                issues.append(ValidationIssue("error", "type", path, "expected object"))
                continue
            key = item.get("key")
            for field in sorted(CREDENTIAL_FIELDS):
                if field not in item:
                    issues.append(ValidationIssue(
                        "error", "missing_credential_field", f"{path}.{field}",
                        f"app.py expects the '{field}' credential field",
                    ))
            for field in ("key", "provenance"):
                if field in item and not str(item.get(field) or "").strip():
                    issues.append(ValidationIssue(
                        "error", "empty_credential_field", f"{path}.{field}",
                        f"credential field '{field}' must not be empty",
                    ))
            # A suggestion may intentionally provide no defaults. A password
            # default without a username is ambiguous and rejected.
            username = str(item.get("default_username") or "")
            password = str(item.get("default_password") or "")
            if bool(password) and not bool(username):
                issues.append(ValidationIssue(
                    "error", "partial_credential_defaults", path,
                    "default_password cannot be set without default_username",
                ))
            if item.get("fallback_passwords") is not None and not isinstance(item.get("fallback_passwords"), list):
                issues.append(ValidationIssue(
                    "error", "invalid_fallback_passwords", f"{path}.fallback_passwords",
                    "fallback_passwords must be an array when present",
                ))
            if key in seen:
                issues.append(ValidationIssue(
                    "error", "duplicate_credential_key", f"{path}.key",
                    f"credential key '{key}' is used more than once",
                ))
            seen.add(key)
            if item.get("requires_user_confirmation") is not True:
                issues.append(ValidationIssue(
                    "error", "confirmation_required", f"{path}.requires_user_confirmation",
                    "suggestions must require explicit user confirmation",
                ))
            issues.extend(self._scan_secrets(item, path))
        return issues

    # ------------------------------------------------------------------
    # Identification
    # ------------------------------------------------------------------
    def identify(
        self,
        observations: Dict[str, Any],
        *,
        phase: Optional[str] = None,
        connector_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Score observed endpoint payloads against identification rules."""

        matches: List[Dict[str, Any]] = []
        candidates = [self.get(connector_id)] if connector_id else self.enabled_connectors()
        for connector in candidates:
            if not connector:
                continue
            rules = _as_list(_as_dict(connector.get("identification")).get("rules"))
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                if phase and rule.get("phase") != phase:
                    continue
                payload = observations.get(rule.get("source"))
                observed = _dig(payload, rule.get("field", ""))
                if observed is None:
                    continue
                if not _compare(observed, rule.get("operator"), rule.get("value")):
                    continue
                matches.append({
                    "connector_id": connector.get("id"),
                    "rule_id": rule.get("id"),
                    "confidence": rule.get("confidence", "unknown"),
                    "phase": rule.get("phase"),
                    "field": rule.get("field"),
                    "value": rule.get("value"),
                    "evidence": rule.get("evidence", ""),
                })

        if not matches:
            return {
                "confidence": "unknown",
                "matches": [],
                "profile_ids": [],
                "ambiguous": False,
                "needs_user_confirmation": True,
            }

        confidence = max(
            (item.get("confidence", "unknown") for item in matches),
            key=lambda item: CONFIDENCE_RANK.get(item, 0),
        )
        profile_ids = sorted({str(item["connector_id"]) for item in matches})
        ambiguous = len(profile_ids) > 1
        if ambiguous and confidence == "exact":
            confidence = "partial"
        return {
            "confidence": confidence,
            "matches": matches,
            "profile_ids": profile_ids,
            "ambiguous": ambiguous,
            "needs_user_confirmation": True,
        }

    def compatibility(
        self, connector_id: str, observations: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Return compatibility context without claiming compatibility from JSON."""

        connector = self.get(connector_id)
        if connector is None:
            return {"status": "not_found", "physical_device_check_required": True}
        result: Dict[str, Any] = {
            "status": "not_evaluated",
            "reason": ValidationReport.compatibility()["reason"],
            "profile_status": connector.get("status"),
            "evidence_verified": _as_dict(connector.get("evidence")).get("verified", False),
            "physical_device_check_required": True,
        }
        if observations is not None:
            identification = self.identify(observations, connector_id=connector_id)
            result["identification"] = identification
            result["status"] = (
                "identified" if identification["confidence"] == "exact"
                else "identification_incomplete"
            )
        return result
