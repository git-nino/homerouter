"""Sanitized HAR analysis and draft connector generation.

This module is the reference implementation for the output contract produced by
``tools/windows/Capture-ONT.ps1``. The Windows tool performs the same work with
built-in PowerShell only; this Python helper exists for automated tests and for
developers who prefer the CLI. It never performs network I/O.
"""

from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlparse

from connector_manager import BODY_FORMATS, SCHEMES
from models.universal import METRICS


PLACEHOLDERS = {
    "password": "<redacted-password>",
    "token": "<redacted-token>",
    "session": "<redacted-session>",
    "auth": "<redacted-auth>",
    "phone": "<redacted-phone>",
    "public_ip": "<redacted-public-ip>",
    "serial": "<redacted-serial>",
    "mac": "<redacted-mac>",
    "hostname": "<redacted-hostname>",
    "request": "<redacted-request-body>",
}

_SENSITIVE_KEY_RULES: Tuple[Tuple[str, re.Pattern], ...] = (
    ("password", re.compile(r"(password|passwd|pwd|loginpd|secret|credential|pppoe_pass)", re.I)),
    ("token", re.compile(r"(token|api[-_]?key|csrf|xsrf|nonce|signature)", re.I)),
    ("session", re.compile(r"(session|sessionid|sid|cookie|set[-_]?cookie)", re.I)),
    ("auth", re.compile(r"(authorization|auth)", re.I)),
    ("phone", re.compile(r"(phone|mobile|msisdn|tel(?:ephone)?|sipuser)", re.I)),
    ("serial", re.compile(r"(serial|^sn$|gpon[-_]?sn)", re.I)),
    ("mac", re.compile(r"(mac|hwaddr|hardwareaddr)", re.I)),
    ("hostname", re.compile(r"(hostname|host_name|devicename|clientname|alias|username|user_name|^host$)", re.I)),
)

_HEADER_REPLACEMENTS = {
    "authorization": "auth",
    "proxy-authorization": "auth",
    "cookie": "session",
    "set-cookie": "session",
    "user-agent": "hostname",
    "host": "hostname",
    "referer": "hostname",
    "origin": "hostname",
    "x-forwarded-for": "hostname",
    "forwarded": "hostname",
    "via": "hostname",
}


_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                  "127.0.0.0/8", "169.254.0.0/16", "::1/128")
)

_MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
_SERIAL_RE = re.compile(r"\b(?=[A-Z0-9]{10,}\b)(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{10,}\b")
_AUTH_HEADER_RE = re.compile(r"^(Basic|Bearer|Digest|Token)\s+.+$", re.I)

_METRIC_KEYS: Tuple[Tuple[str, re.Pattern], ...] = (
    ("device.manufacturer", re.compile(r"(manufacturer|vendor|make)", re.I)),
    ("device.model", re.compile(r"(model|productname|devicemodel)", re.I)),
    ("device.firmware", re.compile(r"(firmware|softwareversion|version)", re.I)),
    ("device.hardware", re.compile(r"(hardware|hwversion|hwver)", re.I)),
    ("wan.status", re.compile(r"(wanstatus|linkstatus|connectionstatus|wanstate)", re.I)),
    ("wan.public_ip", re.compile(r"(wanip|publicip|internetip|wanaddr)", re.I)),
    ("wan.uptime_s", re.compile(r"(wanuptime|connectionuptime|uptime)", re.I)),
    ("optical.rx_power_dbm", re.compile(r"(rxpower|rxpowerdbm|receivepower)", re.I)),
    ("optical.tx_power_dbm", re.compile(r"(txpower|txpowerdbm|transmitpower)", re.I)),
    ("optical.temperature_c", re.compile(r"(temperature|opticaltemp|tempc)", re.I)),
    ("voip.phone_number", re.compile(r"(phonenumber|telnumber|sipuser|voipnumber)", re.I)),
    ("voip.registration_state", re.compile(r"(registration|regstate|voipstatus)", re.I)),
    ("system.uptime_s", re.compile(r"(uptime)", re.I)),
    ("system.cpu_percent", re.compile(r"(cpuusage|cpupercent|cpu)", re.I)),
    ("system.memory_percent", re.compile(r"(memoryused|memorypercent|memory)", re.I)),
    ("devices.count", re.compile(r"(clients|devices|stations|lanhosts|clientlist)", re.I)),
)

_CAPABILITY_BY_DOMAIN = {
    "device_identity": "device_identity",
    "wan": "wan",
    "optical": "optical",
    "voip": "voip",
    "system": "system",
    "connected_devices": "connected_devices",
    "speed_test": "speed_test",
}

_LOGIN_PATH_RE = re.compile(r"(login|signin|auth|session)", re.I)
_SEED_PATH_RE = re.compile(r"(random|challenge|seed|nonce)", re.I)


def _slug(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")
    return text[:48] or "item"


def _is_private_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in _PRIVATE_NETWORKS)


def _digit_count(value: str) -> int:
    return sum(char.isdigit() for char in value)


def _redact_string(text: str, counts: Dict[str, int]) -> str:
    if text.startswith("<redacted-"):
        return text
    result = text

    def replace_ip(match: re.Match) -> str:
        candidate = match.group(0)
        if _is_private_address(candidate):
            return candidate
        counts["public_ip"] = counts.get("public_ip", 0) + 1
        return PLACEHOLDERS["public_ip"]

    result = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", replace_ip, result)
    result = _MAC_RE.sub(lambda _m: _counted(counts, "mac"), result)
    result = _SERIAL_RE.sub(lambda _m: _counted(counts, "serial"), result)

    def replace_phone(match: re.Match) -> str:
        token = match.group(0)
        if _digit_count(token) < 7:
            return token
        counts["phone"] = counts.get("phone", 0) + 1
        return PLACEHOLDERS["phone"]

    result = re.sub(r"(?<![\w])\+?\d[\d\s().-]{5,}\d(?![\w])", replace_phone, result)
    return result


def _counted(counts: Dict[str, int], kind: str) -> str:
    counts[kind] = counts.get(kind, 0) + 1
    return PLACEHOLDERS[kind]


def _sensitive_kind(key: Any) -> Optional[str]:
    text = str(key or "")
    for kind, pattern in _SENSITIVE_KEY_RULES:
        if pattern.search(text):
            return kind
    return None


def sanitize(value: Any, key: str = "", counts: Optional[Dict[str, int]] = None) -> Any:
    """Recursively replace sensitive values while preserving structure."""

    counts = counts if counts is not None else {}
    kind = _sensitive_kind(key)
    if kind:
        return _counted(counts, kind)
    if isinstance(value, dict):
        return {str(name): sanitize(child, str(name), counts) for name, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(child, key, counts) for child in value]
    if isinstance(value, str):
        if _AUTH_HEADER_RE.match(value.strip()):
            return _counted(counts, "auth")
        if key.lower() in ("cookie", "set-cookie", "proxy-authorization"):
            return _counted(counts, "session")
        return _redact_string(value, counts)
    return value


def _sanitize_headers(headers: Any, counts: Dict[str, int]) -> Dict[str, Any]:
    """Keep observable header names, but replace identity-bearing values."""

    result: Dict[str, Any] = {}
    for name, value in _as_dict(headers).items():
        key = str(name)
        lowered = key.lower()
        if lowered in _HEADER_REPLACEMENTS:
            kind = _HEADER_REPLACEMENTS[lowered]
            result[key] = _counted(counts, kind)
        else:
            result[key] = sanitize(value, key, counts)
    return result


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _header_map(value: Any) -> Dict[str, Any]:
    """Accept both HAR header arrays and simple JSON-object headers."""

    if isinstance(value, dict):
        return {str(name): item for name, item in value.items()}
    result: Dict[str, Any] = {}
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict) and "name" in item:
            result[str(item["name"])] = item.get("value", "")
    return result


def parse_har(source: Any) -> List[Dict[str, Any]]:
    """Parse a HAR 1.1/1.2 dict, file path, or JSON string into entries."""

    if isinstance(source, dict):
        document = source
    else:
        if isinstance(source, Path) or (
            isinstance(source, str) and "\n" not in source and Path(source).is_file()
        ):
            text = Path(source).read_text(encoding="utf-8-sig")
        else:
            text = str(source)
        try:
            document = json.loads(text)
        except ValueError as exc:
            raise ValueError("HAR file is not valid JSON") from exc
    if not isinstance(document, dict) or not isinstance(document.get("log"), dict):
        raise ValueError("HAR file has no 'log' object")
    entries = document["log"].get("entries")
    if not isinstance(entries, list):
        raise ValueError("HAR file has no 'log.entries' array")
    return [entry for entry in entries if isinstance(entry, dict)]


def _host_of(url: str) -> str:
    parsed = urlparse(url if "://" in url else "http://" + url)
    return (parsed.hostname or "").lower()


def filter_entries(
    entries: Iterable[Dict[str, Any]], gateway: str, hostname: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Keep only requests to the explicitly selected router host."""

    allowed = {_host_of(gateway)}
    if hostname:
        allowed.add(_host_of(hostname))
    allowed.discard("")
    kept = []
    for entry in entries:
        request = entry.get("request") or {}
        if _host_of(str(request.get("url", ""))) in allowed:
            kept.append(entry)
    return kept


def _content_format(mime: str) -> str:
    lowered = mime.lower()
    if "json" in lowered:
        return "json"
    if "xml" in lowered:
        return "xml"
    if "html" in lowered:
        return "html"
    return "text"


def _safe_json(text: Any) -> Tuple[Optional[Any], bool]:
    if not isinstance(text, str) or not text.strip():
        return None, False
    try:
        return json.loads(text), True
    except ValueError:
        return None, False


def _entry_detail(entry: Dict[str, Any], counts: Dict[str, int]) -> Dict[str, Any]:
    request = entry.get("request") or {}
    response = entry.get("response") or {}
    url = str(request.get("url", ""))
    parsed = urlparse(url if "://" in url else "http://" + url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    raw_headers = _header_map(request.get("headers"))
    request_headers = _sanitize_headers(raw_headers, counts)
    auth_scheme = ""
    for name, value in raw_headers.items():
        if str(name).lower() != "authorization":
            continue
        match = re.match(r"\s*(basic|digest|bearer|token)\b", str(value), re.I)
        if match:
            auth_scheme = match.group(1).lower()
            counts["auth"] = counts.get("auth", 0) + 1
            break
    body_text = (request.get("postData") or {}).get("text")
    body_json, body_is_json = _safe_json(body_text)
    if body_is_json:
        body = sanitize(body_json, "body", counts)
    elif isinstance((request.get("postData") or {}).get("params"), list):
        body = sanitize(
            {str(item.get("name", "")): item.get("value")
             for item in request["postData"]["params"] if isinstance(item, dict)},
            "body", counts,
        )
    elif body_text:
        body = {"raw": PLACEHOLDERS["request"]}
        counts["request"] = counts.get("request", 0) + 1
    else:
        body = {}

    content = response.get("content") or {}
    mime = str(content.get("mimeType", ""))
    response_json, response_is_json = _safe_json(content.get("text"))
    response_payload = sanitize(response_json, "response", counts) if response_is_json else None
    return {
        "method": str(request.get("method", "GET")).upper() or "GET",
        "url": url,
        "path": parsed.path or "/",
        "scheme": parsed.scheme if parsed.scheme in SCHEMES else "http",
        "query": sanitize(query, "query", counts),
        "headers": request_headers,
        "auth_scheme": auth_scheme,
        "body": body if isinstance(body, dict) else {},
        "body_is_json": body_is_json,
        "request_content_type": str(
            (request.get("postData") or {}).get("mimeType", "")
        ),
        "status": int(response.get("status") or 0),
        "content_type": mime,
        "format": _content_format(mime),
        "response": response_payload,
        "started": str(entry.get("startedDateTime", "")),
    }


def _walk_fields(value: Any, path: str = "$") -> Iterable[Tuple[str, str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            yield str(key), child_path, child
            yield from _walk_fields(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_fields(child, f"{path}[{index}]")


def _classify_auth(details: List[Dict[str, Any]]) -> Tuple[str, Optional[str], bool, List[str], List[str]]:
    classification = "none"
    proposed: Optional[str] = "none"
    manual = False
    sequence: List[str] = []
    dynamic: List[str] = []
    headers_text = json.dumps([item["headers"] for item in details]).lower()
    bodies_text = json.dumps([item["body"] for item in details]).lower()
    schemes = " ".join(item.get("auth_scheme", "") for item in details)

    if "basic" in schemes:
        classification, proposed = "http_basic", "http_basic"
    elif "digest" in schemes:
        classification, proposed = "http_digest", "http_digest"
    elif "bearer" in schemes or "token" in schemes:
        classification, proposed = "token", "token"
    elif re.search(r"(accesstoken|access_token|auth[-_]?token|api[-_]?token|\btoken\b)", bodies_text):
        classification, proposed = "token", "token"
    elif re.search(r"(set-cookie|\bsession\b|sessionid)", headers_text + bodies_text):
        classification, proposed = "cookie_session", "session"
    elif _LOGIN_PATH_RE.search(" ".join(item["path"] for item in details)):
        classification, proposed = "html_form", "form"
    if re.search(r"(nonce|challenge|loginpd|encrypted|acsrandom)", bodies_text):
        classification, proposed, manual = "proprietary_challenge_response", None, True

    for item in details:
        item_body = json.dumps(item["body"]).lower()
        login_like = (
            _LOGIN_PATH_RE.search(item["path"])
            or re.search(r"(do_login|signin|authenticate|\"login\")", item_body)
        )
        if login_like:
            sequence.append(f"{item['method']} {item['path']} (login-like)")
        elif _SEED_PATH_RE.search(item["path"]):
            sequence.append(f"{item['method']} {item['path']} (challenge/seed-like)")
    for key, _path, _value in _walk_fields([item["body"] for item in details]):
        if re.search(r"(token|session|nonce|challenge|csrf|seed)", key, re.I):
            dynamic.append(key)
    return classification, proposed, manual, sequence, sorted(set(dynamic))


def _identifier_candidates(details: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    patterns = (
        ("manufacturer", re.compile(r"(manufacturer|vendor|make)", re.I)),
        ("model", re.compile(r"(model|productname|devicemodel)", re.I)),
        ("firmware", re.compile(r"(firmware|softwareversion|version)", re.I)),
        ("hardware", re.compile(r"(hardware|hwversion|hwver)", re.I)),
    )
    results: List[Dict[str, Any]] = []
    seen = set()
    for detail in details:
        for key, path, value in _walk_fields(detail["response"]):
            if not isinstance(value, (str, int, float, bool)) or str(value).startswith("<redacted-"):
                continue
            for label, pattern in patterns:
                if not pattern.search(key):
                    continue
                signature = (label, str(value))
                if signature in seen:
                    continue
                seen.add(signature)
                results.append({
                    "field": key,
                    "path": path,
                    "value": value,
                    "confidence": "partial",
                    "manual_review": True,
                    "evidence": f"Observed {label}-like response field {path}.",
                })
                break
    return results


def _metric_candidates(details: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    seen = set()
    for detail in details:
        endpoint_id = detail["id"]
        for key, path, value in _walk_fields(detail["response"]):
            for metric_id, pattern in _METRIC_KEYS:
                if not pattern.search(key):
                    continue
                if isinstance(value, dict) or str(value).startswith("<redacted-"):
                    continue
                signature = (metric_id, path)
                if signature in seen:
                    continue
                seen.add(signature)
                is_list = isinstance(value, list)
                results.append({
                    "metric": metric_id,
                    "endpoint": endpoint_id,
                    "field": key,
                    "path": path,
                    "method": "json_path",
                    "expression": path,
                    "confidence": "partial",
                    "manual_review": True,
                    "evidence": f"Observed candidate field {path} in {endpoint_id}; meaning not assumed.",
                })
                if is_list:
                    results[-1]["expression"] = path
                break
    return results


def _infer_dependencies(details: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    dependencies: Dict[str, List[str]] = {}
    dynamic_key = re.compile(r"(session|token|challenge|nonce|csrf|seed|stamp)", re.I)
    for index, detail in enumerate(details):
        request_keys = {
            key for container in (detail["query"], detail["body"], detail["headers"])
            for key in _walk_fields(container) if dynamic_key.search(key[0])
        }
        if not request_keys:
            continue
        for earlier in details[:index]:
            response_keys = {key for key, _path, _value in _walk_fields(earlier["response"])}
            if request_keys & response_keys:
                dependencies.setdefault(detail["id"], [])
                if earlier["id"] not in dependencies[detail["id"]]:
                    dependencies[detail["id"]].append(earlier["id"])
    return dependencies


def analyze_har(
    har_source: Any,
    gateway: str,
    *,
    hostname: Optional[str] = None,
    connector_id: Optional[str] = None,
    display_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Analyze a HAR and return a draft profile plus evidence documents."""

    entries = parse_har(har_source)
    counts: Dict[str, int] = {}
    selected = filter_entries(entries, gateway, hostname)
    details = [_entry_detail(entry, counts) for entry in selected]

    used: Dict[str, int] = {}
    for detail in details:
        base = f"{_slug(detail['method'])}_{_slug(detail['path'])}"
        used[base] = used.get(base, 0) + 1
        detail["id"] = base if used[base] == 1 else f"{base}_{used[base]:02d}"

    endpoints: List[Dict[str, Any]] = []
    for index, detail in enumerate(details, start=1):
        body_format = "json" if detail["body_is_json"] or "json" in detail["request_content_type"].lower() else "form"
        if body_format not in BODY_FORMATS:            # defensive, currently unreachable
            body_format = "form"
        endpoint: Dict[str, Any] = {
            "id": detail["id"],
            "method": detail["method"],
            "path": detail["path"],
            "scheme": detail["scheme"],
            "format": detail["format"],
            "timeout_seconds": 5,
            "auth": not all(
                name.lower() not in ("authorization", "cookie", "set-cookie")
                for name in detail["headers"]
            ),
            "description": f"Observed HAR request {index}: {detail['method']} {detail['path']}.",
        }
        if detail["query"]:
            endpoint["query"] = detail["query"]
        if detail["method"] == "POST":
            endpoint["body_format"] = body_format
            endpoint["body"] = detail["body"] or {}
        if detail["headers"]:
            endpoint["headers"] = detail["headers"]
        endpoints.append(endpoint)

    classification, proposed, manual_auth, sequence, dynamic_values = _classify_auth(details)
    identifiers = _identifier_candidates(details)
    metric_candidates = _metric_candidates(details)
    dependencies = _infer_dependencies(details)

    capabilities = sorted({
        _CAPABILITY_BY_DOMAIN[METRICS[candidate["metric"]].domain.value]
        for candidate in metric_candidates
        if candidate["metric"] in METRICS
    }) or ["unclassified"]

    if isinstance(har_source, dict):
        har_name = "inline.har"
    elif Path(str(har_source)).is_file():
        har_name = Path(str(har_source)).name
    else:
        har_name = "inline.har"
    har_name = _slug(har_name)
    model_value = next(
        (str(item["value"]) for item in identifiers if re.search(r"model", item["field"], re.I)),
        None,
    )
    slug_source = _slug(model_value or gateway or har_name)
    resolved_id = connector_id or f"observed.{slug_source}"
    resolved_name = display_name or (
        f"{model_value} (HAR capture, draft)" if model_value else "Observed router (HAR capture, draft)"
    )

    identification_rule = {
        "id": "observed-identifier",
        "phase": "pre_auth",
        "source": endpoints[0]["id"] if endpoints else "root",
        "field": identifiers[0]["field"] if identifiers else "__OBSERVE_FIELD__",
        "operator": "equals",
        "value": identifiers[0]["value"] if identifiers else "__MANUAL_REVIEW_REQUIRED__",
        "confidence": "unknown",
        "evidence": identifiers[0]["evidence"] if identifiers else "No non-sensitive identifier was observed; add one manually.",
    }

    auth_flow = {
        "id": "observed-auth-flow",
        "type": proposed or "none",
        "order": 1,
        "success": {"field": "__OBSERVE_SUCCESS_FIELD__", "allowed": [True]},
    }
    if endpoints and proposed in ("session", "token", "form"):
        auth_flow["endpoints"] = {
            "login": next(
                (item["id"] for item in endpoints if _LOGIN_PATH_RE.search(item["path"])),
                endpoints[-1]["id"],
            )
        }

    manual_review = [
        "Confirm every endpoint path, query field and request body field against the source HAR.",
        "Confirm the authentication handler, success field and session/token field names.",
        "Confirm that each universal metric candidate has the assumed meaning before enabling it.",
        "Replace placeholder identification values with a stable, non-sensitive observed value.",
        "Validate the disabled draft on a physical ONT before changing status or enabling it.",
    ]
    if manual_auth:
        manual_review.insert(
            1,
            "Implement the observed proprietary authentication sequence in auth_handlers.py before use.",
        )

    draft: Dict[str, Any] = {
        "id": resolved_id,
        "display_name": resolved_name,
        "status": "draft",
        "enabled": False,
        "generated_by": "Capture-ONT.ps1 / tools.har_analyzer",
        "source_har": har_name,
        "requires_manual_auth_handler": manual_auth,
        "evidence": {
            "source": f"Sanitized HAR analysis of selected host {gateway}; {len(selected)} matching request(s).",
            "verified": False,
            "notes": [
                "Generated from observed browser traffic only; no endpoint or value was invented.",
                "Raw HAR files were not copied into this profile or its evidence bundle.",
            ],
        },
        "identification": {
            "confidence": "unknown",
            "rules": [identification_rule],
        },
        "auth": {
            "credential_suggestion_key": "generic_user_supplied",
            "flows": [auth_flow],
        },
        "endpoints": endpoints,
        "extraction": [],
        "validation": [],
        "dependencies": dependencies,
        "capabilities": capabilities,
        "limitations": [
            "Draft profile is disabled and requires manual review before any live use.",
            "Metric mappings are candidates inferred from field names and response paths, not proven semantics.",
            "Authentication success criteria are placeholders until confirmed from the HAR.",
        ],
        "auth_observations": {
            "classification": classification,
            "proposed_handler": proposed or "manual_handler_required",
            "sequence": sequence,
            "dynamic_values": dynamic_values,
            "notes": [
                "Authentication classification is based only on captured headers, bodies and paths.",
            ],
            "evidence": f"Observed across {len(selected)} request(s) to the selected host.",
        },
        "identification_candidates": identifiers,
        "extraction_candidates": metric_candidates,
        "metric_mapping_candidates": metric_candidates,
        "manual_review": manual_review,
    }

    sequence_doc = [
        {
            "endpoint": item["id"],
            "method": item["method"],
            "path": item["path"],
            "status": item["status"],
            "content_type": item["content_type"],
        }
        for item in details
    ]
    return {
        "draft": draft,
        "sequence": sequence_doc,
        "endpoints": endpoints,
        "details": details,
        "redaction_counts": counts,
        "selected_host": gateway,
        "filtered_entries": len(selected),
        "total_entries": len(entries),
        "auth_observations": draft["auth_observations"],
        "identification_candidates": identifiers,
        "metric_mapping_candidates": metric_candidates,
    }


def write_outputs(analysis: Dict[str, Any], output_dir: Path) -> Dict[str, str]:
    """Write the draft profile and sanitized evidence bundle."""

    draft = analysis["draft"]
    output_dir = Path(output_dir)
    evidence_dir = output_dir / f"{draft['id']}.evidence"
    endpoints_dir = evidence_dir / "endpoints"
    endpoints_dir.mkdir(parents=True, exist_ok=True)

    draft_path = output_dir / f"{draft['id']}.draft.json"
    draft_path.write_text(json.dumps(draft, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    summary = {
        "selected_host": analysis["selected_host"],
        "source_har": draft["source_har"],
        "total_entries": analysis["total_entries"],
        "filtered_entries": analysis["filtered_entries"],
        "sequence": analysis["sequence"],
        "auth_observations": analysis["auth_observations"],
        "identification_candidates": analysis["identification_candidates"],
        "redaction_counts": analysis["redaction_counts"],
        "warning": "Raw HAR content is never copied into this evidence bundle.",
    }
    (evidence_dir / "capture-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (evidence_dir / "redaction-report.json").write_text(
        json.dumps(analysis["redaction_counts"], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    for detail in analysis["details"]:
        record = {
            "id": detail["id"],
            "method": detail["method"],
            "path": detail["path"],
            "status": detail["status"],
            "content_type": detail["content_type"],
            "request": {
                "query": detail["query"],
                "headers": detail["headers"],
                "body": detail["body"],
            },
            "response": detail["response"],
            "response_fields": sorted({key for key, _path, _value in _walk_fields(detail["response"])}),
        }
        (endpoints_dir / f"{detail['id']}.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    return {"draft": str(draft_path), "evidence": str(evidence_dir)}


__all__ = [
    "PLACEHOLDERS", "analyze_har", "filter_entries", "parse_har",
    "sanitize", "write_outputs",
]
