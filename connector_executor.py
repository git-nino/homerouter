"""Generic runtime executor for JSON connector profiles.

The executor is intentionally data-driven: it can issue only the endpoints,
queries, headers, bodies, extraction rules, and transforms declared in a
connector profile. It never evaluates profile text as code.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import requests
from requests.auth import AuthBase

from auth_handlers import AuthenticationError, auth_handler
from connector_manager import BODY_FORMATS, SCHEMES, ConnectorManager
from models.enums import Availability, CompatibilityState, Domain
from models.phone import normalize_phone_number
from models.universal import METRICS, ConnectedDevice, UniversalModel
from profile_safety import (
    UnsafeProfileRequest,
    build_request_url,
    is_redirect,
    normalize_gateway,
    read_bounded_text,
    validate_endpoint_path,
    validate_method,
    validate_scheme,
)


_MISSING = object()


class ExecutorError(Exception):
    """Base class for safe, profile-runtime errors."""


class EndpointError(ExecutorError):
    """An endpoint could not be requested or parsed."""


class ExtractionError(ExecutorError):
    """An extraction rule could not be applied."""


class SessionExpired(ExecutorError):
    """The gateway rejected the current authenticated session."""


class _NoAuth(AuthBase):
    """Per-request override for endpoints declared without authentication."""

    def __call__(self, prepared_request):
        return prepared_request


@dataclass
class EndpointResult:
    endpoint_id: str
    payload: Dict[str, Any]
    text: str
    headers: Dict[str, str]
    status_code: int


def _dig(data: Any, field_path: str) -> Any:
    node = data
    for part in str(field_path).split("."):
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
    actual = str(value)
    wanted = str(expected)
    if operator == "equals":
        return actual == wanted
    if operator == "not_equals":
        return actual != wanted
    if operator == "contains":
        return wanted in actual
    if operator == "prefix":
        return actual.startswith(wanted)
    if operator == "regex":
        try:
            return re.search(wanted, actual) is not None
        except re.error:
            return False
    return False


def _parse_json_text(text: str) -> Tuple[Dict[str, Any], str]:
    text = (text or "").strip()
    if "\n\n" in text:               # some firmware prepends a Content-type line
        text = text.split("\n\n", 1)[1].strip()
    if not text:
        return {}, text
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise EndpointError("invalid JSON response") from exc
    return (payload if isinstance(payload, dict) else {"value": payload}), text


def _path_tokens(expression: str) -> List[Tuple[str, Any]]:
    text = expression.strip()
    if text.startswith("$"):
        text = text[1:]
    tokens: List[Tuple[str, Any]] = []
    while text:
        if text[0] == ".":
            text = text[1:]
            match = re.match(r"^([^.\[\]]+)", text)
            if not match:
                break
            tokens.append(("key", match.group(1)))
            text = text[match.end():]
            continue
        if text[0] == "[":
            match = re.match(r"^\[\s*'([^']+)'\s*\]|^\[(\d+)\]", text)
            if not match:
                break
            if match.group(1) is not None:
                tokens.append(("key", match.group(1)))
            else:
                tokens.append(("index", int(match.group(2))))
            text = text[match.end():]
            continue
        break
    return tokens


def _path_values(data: Any, expression: str) -> List[Any]:
    if expression.strip() == "$":
        return [data]
    tokens = _path_tokens(expression)
    current = [data]
    for kind, token in tokens:
        following: List[Any] = []
        for node in current:
            if kind == "key" and isinstance(node, dict) and token in node:
                following.append(node[token])
            elif kind == "index" and isinstance(node, list) and token < len(node):
                following.append(node[token])
        current = following
        if not current:
            break
    return current


def _first(value: Any) -> Any:
    if isinstance(value, list) and value:
        return value[0]
    return value


def _is_missing(value: Any) -> bool:
    return value is _MISSING or value is None or value == ""


class ConnectorExecutor:
    """Execute one enabled connector profile against one gateway."""

    def __init__(
        self,
        connector: Dict[str, Any],
        gateway: str,
        credentials: Optional[Dict[str, Any]] = None,
        manager: Optional[ConnectorManager] = None,
        session: Optional[requests.Session] = None,
    ):
        if not connector.get("enabled", True):
            raise ExecutorError("Connector profile is disabled.")
        self.connector = connector
        self.profile_id = str(connector.get("id", ""))
        self.display_name = str(connector.get("display_name", self.profile_id))
        try:
            self.gateway = normalize_gateway(gateway)
            self.scheme = validate_scheme(connector.get("scheme", "http"))
        except UnsafeProfileRequest as exc:
            raise ExecutorError(str(exc)) from exc
        self.credentials = dict(credentials or {})
        self.username = str(self.credentials.get("username", ""))
        self.password = self.credentials.get("password", "")
        self.manager = manager or ConnectorManager()
        self.http = session or requests.Session()
        self.http.headers.setdefault("User-Agent", "Homerouter connector runtime")

        self.endpoints = {
            item.get("id"): item
            for item in connector.get("endpoints", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        self.dependencies = {
            str(key): [str(value) for value in dependencies if isinstance(value, str)]
            for key, dependencies in (connector.get("dependencies") or {}).items()
            if isinstance(dependencies, list)
        }
        self.auth = connector.get("auth") or {}
        self.flows = sorted(
            (flow for flow in self.auth.get("flows", []) if isinstance(flow, dict)),
            key=lambda flow: int(flow.get("order", 1)),
        )
        self.logout_endpoint = self.auth.get("logout_endpoint")
        self.session_spec: Dict[str, Any] = {}
        self.state: Dict[str, Any] = {}
        self.derived: Dict[str, Any] = {}
        self.endpoint_results: Dict[str, EndpointResult] = {}
        self.active_flow: Optional[Dict[str, Any]] = None
        self.authenticated = False
        self.last_model: Optional[UniversalModel] = None

    # ------------------------------------------------------------------
    # Small public helpers
    # ------------------------------------------------------------------
    @staticmethod
    def dig(data: Any, field_path: str) -> Any:
        return _dig(data, field_path)

    def flow_endpoint(self, name: str) -> Optional[str]:
        if not self.active_flow:
            return None
        value = (self.active_flow.get("endpoints") or {}).get(name)
        return str(value) if value else None

    @property
    def _auth_endpoint_ids(self) -> Set[str]:
        ids = {self.logout_endpoint} if self.logout_endpoint else set()
        for flow in self.flows:
            ids.update(str(value) for value in (flow.get("endpoints") or {}).values())
        return {item for item in ids if item}

    def _reset_auth_state(self) -> None:
        self.state = {}
        self.derived = {}
        self.endpoint_results = {}
        self.active_flow = None
        self.authenticated = False
        self.session_spec = {}
        self.http.auth = None
        for name in list(self.http.headers):
            if name.lower() == "authorization":
                del self.http.headers[name]

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    def connect(self) -> Dict[str, Any]:
        self._authenticate(force=True)
        return {
            "ok": True,
            "profile_id": self.profile_id,
            "display_name": self.display_name,
            "gateway": self.gateway,
        }

    def _authenticate(self, force: bool = False) -> None:
        if self.authenticated and not force:
            return
        for flow in self.flows:
            self._reset_auth_state()
            self.active_flow = flow
            try:
                handler = auth_handler(self, flow)
                handler.authenticate()
                handler._apply_session()
            except (AuthenticationError, EndpointError):
                continue
            self.authenticated = True
            return
        raise AuthenticationError(
            "No configured authentication flow succeeded for this profile."
        )

    def _session_expired(self) -> bool:
        field = self.session_spec.get("validity_field")
        if not field:
            return False
        invalid = {str(value) for value in self.session_spec.get("invalid_values", [])}
        if not invalid:
            return False
        for result in self.endpoint_results.values():
            value = _dig(result.payload, field)
            if value is not None and str(value) in invalid:
                return True
        return False

    # ------------------------------------------------------------------
    # Endpoint execution
    # ------------------------------------------------------------------
    def _ordered_endpoint_ids(self, candidates: Sequence[str]) -> List[str]:
        wanted = [item for item in candidates if item in self.endpoints]
        visiting: Set[str] = set()
        visited: Set[str] = set()
        order: List[str] = []

        def visit(endpoint_id: str) -> None:
            if endpoint_id in visiting or endpoint_id in visited:
                return
            visiting.add(endpoint_id)
            for dependency in self.dependencies.get(endpoint_id, []):
                if dependency in self.endpoints:
                    visit(dependency)
            visiting.discard(endpoint_id)
            visited.add(endpoint_id)
            order.append(endpoint_id)

        for endpoint_id in wanted:
            visit(endpoint_id)
        return order

    def _resolve(
        self, spec: Any, key: str = "", current_endpoint: Optional[str] = None
    ) -> Any:
        if isinstance(spec, list):
            return [self._resolve(item, key, current_endpoint) for item in spec]
        if isinstance(spec, dict) and "source" in spec:
            source = spec.get("source")
            if isinstance(source, str) and "." in source:
                prefix, dotted_field = source.split(".", 1)
                if prefix in {"user_input", "state", "derived", "seed", "endpoint_field"}:
                    spec = dict(spec)
                    spec["source"] = prefix
                    spec.setdefault("field", dotted_field)
                    source = prefix
            if source == "literal":
                return spec.get("value")
            if source == "user_input":
                field = str(spec.get("field") or key)
                if field == "password":
                    return self.password
                if field == "username":
                    return self.username
                return self.credentials.get(field)
            if source == "state":
                return self.state.get(str(spec.get("field") or key))
            if source == "derived":
                return self.derived.get(str(spec.get("field") or key))
            if source == "seed":
                seed_id = self.flow_endpoint("seed") or "seed"
                result = self.endpoint_results.get(seed_id)
                return _dig(result.payload if result else {}, str(spec.get("field") or key))
            if source == "endpoint_field":
                endpoint_id = str(spec.get("endpoint", ""))
                if endpoint_id == current_endpoint:
                    return None
                if endpoint_id not in self.endpoint_results and endpoint_id in self.endpoints:
                    for required in self._ordered_endpoint_ids([endpoint_id]):
                        if required == current_endpoint:
                            continue
                        if required not in self.endpoint_results:
                            self.execute_endpoint(required)
                result = self.endpoint_results.get(endpoint_id)
                value = _dig(result.payload if result else {}, str(spec.get("field", "")))
                mapping = spec.get("mapping") or {}
                if mapping and value is not None:
                    value = mapping.get(str(value), mapping.get("default", value))
                return value
            raise EndpointError("unsupported dynamic request source")
        if isinstance(spec, dict):
            return {
                key: self._resolve(value, key, current_endpoint)
                for key, value in spec.items()
            }
        return spec

    def _resolve_map(
        self, values: Any, current_endpoint: Optional[str] = None
    ) -> Dict[str, Any]:
        return {
            str(key): self._resolve(value, str(key), current_endpoint)
            for key, value in (values or {}).items()
        }

    def execute_endpoint(self, endpoint_id: str) -> EndpointResult:
        endpoint = self.endpoints.get(endpoint_id)
        if endpoint is None:
            raise EndpointError("unknown endpoint in connector profile")

        query = self._resolve_map(endpoint.get("query"), endpoint_id)
        body = self._resolve_map(endpoint.get("body"), endpoint_id)
        headers = self._resolve_map(endpoint.get("headers"), endpoint_id)
        timeout = float(endpoint.get("timeout_seconds", 6))
        try:
            method = validate_method(endpoint.get("method", "GET"))
            scheme = validate_scheme(endpoint.get("scheme", self.scheme))
            url = build_request_url(scheme, self.gateway, endpoint.get("path"))
        except UnsafeProfileRequest as exc:
            raise EndpointError(str(exc)) from exc
        body_format = str(endpoint.get("body_format", "form")).lower()
        if body_format not in BODY_FORMATS:
            raise EndpointError(f"unsupported body format {body_format!r}")
        request_kwargs: Dict[str, Any] = {
            "params": query or None,
            "headers": headers or None,
            "timeout": timeout,
            "verify": True,
            "allow_redirects": False,
            "stream": True,
        }
        if endpoint.get("auth") is False:
            request_kwargs["auth"] = _NoAuth()
        if method == "POST":
            if body_format == "json":
                request_kwargs["json"] = body or None
            else:
                request_kwargs["data"] = body or None

        try:
            response = self.http.request(method, url, **request_kwargs)
        except requests.RequestException as exc:
            raise EndpointError(
                f"network request failed ({type(exc).__name__})"
            ) from exc
        try:
            if is_redirect(response.status_code):
                raise EndpointError(f"unsafe HTTP {response.status_code} redirect")
            if response.status_code >= 400:
                raise EndpointError(f"HTTP {response.status_code}")
            try:
                text = read_bounded_text(response)
            except UnsafeProfileRequest as exc:
                raise EndpointError(str(exc)) from exc
            if endpoint.get("format", "json") == "json":
                payload, text = _parse_json_text(text)
            else:
                payload = {}
            result = EndpointResult(
                endpoint_id=endpoint_id,
                payload=payload,
                text=text,
                headers={str(key): str(value) for key, value in response.headers.items()},
                status_code=response.status_code,
            )
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        self.endpoint_results[endpoint_id] = result
        return result

    def discover(self) -> Dict[str, Any]:
        """Run only unauthenticated endpoints used by pre-auth identification."""

        sources = {
            str(rule.get("source"))
            for rule in (self.connector.get("identification") or {}).get("rules", [])
            if isinstance(rule, dict) and rule.get("phase") == "pre_auth"
        }
        observations: Dict[str, Any] = {}
        errors: List[str] = []
        for endpoint_id in self._ordered_endpoint_ids(sources):
            if self.endpoints[endpoint_id].get("auth") is not False:
                continue
            try:
                observations[endpoint_id] = self.execute_endpoint(endpoint_id).payload
            except EndpointError as exc:
                errors.append(f"{endpoint_id}: {exc}")
        return {"observations": observations, "errors": errors}

    # ------------------------------------------------------------------
    # Collection + normalization
    # ------------------------------------------------------------------
    def collect(self) -> UniversalModel:
        if not self.authenticated:
            raise AuthenticationError("Connect before collecting router data.")
        for attempt in range(2):
            self._clear_collection_results()
            model = self._collect_once()
            if not self._session_expired():
                model.updated_at = datetime.now(timezone.utc)
                self.last_model = model
                return model
            if attempt == 0:
                self._authenticate(force=True)
                continue
            raise SessionExpired("Gateway session expired. Please reconnect.")
        raise SessionExpired("Gateway session expired. Please reconnect.")

    def _clear_collection_results(self) -> None:
        auth_ids = self._auth_endpoint_ids
        for endpoint_id in list(self.endpoint_results):
            if endpoint_id not in auth_ids:
                del self.endpoint_results[endpoint_id]

    def _collect_once(self) -> UniversalModel:
        model = UniversalModel(gateway=self.gateway, profile_id=self.profile_id)
        model.compatibility = self._compatibility({})
        endpoint_errors: Dict[str, str] = {}

        candidates = [
            endpoint_id for endpoint_id in self.endpoints
            if endpoint_id != self.logout_endpoint
        ]
        for endpoint_id in self._ordered_endpoint_ids(candidates):
            if endpoint_id in self._auth_endpoint_ids:
                continue
            try:
                self.execute_endpoint(endpoint_id)
            except EndpointError as exc:
                endpoint_errors[endpoint_id] = str(exc)
                model.add_endpoint_error(endpoint_id, error=str(exc))

        observations = {
            endpoint_id: result.payload
            for endpoint_id, result in self.endpoint_results.items()
        }
        self._apply_extraction(model, endpoint_errors)
        self._apply_derived_aggregates(model)
        self._apply_validation(model)
        model.compatibility = self._compatibility(observations)
        firmware = model.get_metric(Domain.DEVICE_IDENTITY, "device.firmware")
        if firmware.availability is Availability.AVAILABLE:
            model.compatibility.firmware = firmware.value
        return model

    def _compatibility(self, observations: Dict[str, Any]):
        from models.universal import Compatibility

        result = self.manager.compatibility(self.profile_id, observations)
        reasons = [str(result.get("reason", ""))]
        identification = result.get("identification") or {}
        for match in identification.get("matches", []):
            if match.get("connector_id") == self.profile_id:
                reasons.append(
                    f"Matched identification rule {match.get('rule_id')} "
                    f"({match.get('confidence')} confidence)."
                )
        state = CompatibilityState.IDENTIFICATION_INCOMPLETE
        if identification.get("confidence") == "exact" and not identification.get("ambiguous"):
            state = CompatibilityState.PARTIALLY_COMPATIBLE
        return Compatibility(
            state=state,
            reasons=[reason for reason in reasons if reason],
            profile_id=self.profile_id,
        )

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------
    @staticmethod
    def _conditions_met(rule: Dict[str, Any], context: Any) -> bool:
        conditions: List[Dict[str, Any]] = []
        if isinstance(rule.get("when"), dict):
            conditions.append(rule["when"])
        conditions.extend(
            condition for condition in rule.get("when_all", [])
            if isinstance(condition, dict)
        )
        for condition in conditions:
            value = _dig(context, str(condition.get("field", "")))
            if value is None:
                return False
            if not _compare(value, condition.get("operator"), condition.get("value")):
                return False
        return True

    def _item_from_fields(
        self,
        fields: Dict[str, Any],
        source: Any,
        rule: Dict[str, Any],
        index: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        item: Dict[str, Any] = {}
        for target, spec in fields.items():
            if not isinstance(spec, dict):
                continue
            value: Any = _MISSING
            if "literal" in spec:
                value = spec["literal"]
                if index is not None and isinstance(value, str):
                    value = value.replace("{n}", str(index))
            elif "field" in spec:
                field = str(spec["field"])
                if index is not None:
                    field = field.replace("{n}", str(index))
                value = _dig(source, field)
                if _is_missing(value) and spec.get("fallback_field"):
                    fallback = str(spec["fallback_field"]).replace("{n}", str(index or ""))
                    value = _dig(source, fallback)
            else:
                value = source
            if _is_missing(value) and "default" in spec:
                value = spec["default"]
            if not _is_missing(value):
                for transform in spec.get("transforms", []):
                    value = self._transform(transform, value, rule.get("transform_options") or {})
            if not _is_missing(value):
                item[str(target)] = value
        return item or None

    def _extract(self, rule: Dict[str, Any], result: EndpointResult) -> Any:
        method = rule.get("method")
        expression = str(rule.get("expression", ""))
        if method == "literal":
            return expression
        if method == "header":
            return result.headers.get(expression, _MISSING)
        if method == "regex":
            matches = re.findall(expression, result.text, re.MULTILINE)
            if not matches:
                return _MISSING
            return matches if len(matches) > 1 else matches[0]
        if method in ("xpath", "css"):
            raise ExtractionError(
                f"extraction method {method!r} is not available in this runtime"
            )

        if method == "json_path_multi":
            values: List[Any] = []
            for part in expression.split(","):
                matches = _path_values(result.payload, part.strip())
                if not matches:
                    return _MISSING
                values.append(matches[0])
            return values

        values = _path_values(result.payload, expression)
        if not values:
            return _MISSING
        value = values[0]

        if method == "json_path":
            return value
        if method == "list_length":
            return len(value) if isinstance(value, list) else _MISSING
        if method == "list_objects":
            if not isinstance(value, list):
                return _MISSING
            items = []
            for entry in value:
                if not isinstance(entry, dict):
                    continue
                item = self._item_from_fields(
                    rule.get("item_fields") or {}, entry, rule
                )
                if item and self._conditions_met(rule, item):
                    items.append(item)
            return items
        if method == "keyed_objects":
            if not isinstance(value, dict):
                return _MISSING
            range_spec = rule.get("range") or {}
            start = int(range_spec.get("start", 1))
            maximum = int(range_spec.get("max", 64))
            end = range_spec.get("end")
            count_field = range_spec.get("count_field")
            if count_field:
                count = _dig(result.payload, str(count_field))
                try:
                    end = start + int(count) - 1 if count is not None else None
                except (TypeError, ValueError):
                    end = None
            if end is None:
                end = start + maximum - 1
            end = min(int(end), start + maximum - 1)
            items = []
            for index in range(start, end + 1):
                item = self._item_from_fields(
                    rule.get("item_fields") or {}, value, rule, index=index
                )
                if item and self._conditions_met(rule, item):
                    items.append(item)
            return items
        raise ExtractionError(f"unsupported extraction method {method!r}")

    @staticmethod
    def _transform(name: str, value: Any, options: Dict[str, Any]) -> Any:
        if name == "strip":
            return str(value).strip()
        if name == "decode_point_encoded_ip":
            return str(value).replace("_point_", ".")
        if name == "split_before_at":
            return str(value).split("@", 1)[0]
        if name == "integer":
            try:
                return int(str(value).strip())
            except (TypeError, ValueError):
                try:
                    return int(float(str(value).strip()))
                except (TypeError, ValueError):
                    return None
        if name == "number":
            try:
                return float(str(value).strip())
            except (TypeError, ValueError):
                return None
        if name == "enabled_flag":
            mapping = (options.get("enabled_flag") or {}).get("values") or {}
            default = (options.get("enabled_flag") or {}).get("default")
            return mapping.get(str(value), default if default is not None else str(value))
        if name == "voip_status_label":
            mapping = (options.get("voip_status_label") or {}).get("codes") or {}
            default = (options.get("voip_status_label") or {}).get("unknown", "unknown")
            return mapping.get(str(value), default)
        if name == "used_percent":
            if not isinstance(value, (list, tuple)) or len(value) < 2:
                return None
            try:
                total = float(value[0])
                free = float(value[1])
            except (TypeError, ValueError):
                return None
            if total <= 0:
                return None
            return round((total - free) / total * 100, 1)
        raise ExtractionError(f"unsupported transform {name!r}")

    def _apply_extraction(
        self, model: UniversalModel, endpoint_errors: Dict[str, str]
    ) -> None:
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for rule in self.connector.get("extraction", []):
            if not isinstance(rule, dict):
                continue
            key = rule.get("metric") or f"collection:{rule.get('collection')}"
            groups.setdefault(str(key), []).append(rule)

        for key, rules in groups.items():
            rules = sorted(rules, key=lambda item: int(item.get("priority", 50)))
            target_metric = rules[0].get("metric")
            target_collection = rules[0].get("collection")
            succeeded = False
            last_error: Optional[str] = None
            availability_on_missing = str(
                rules[0].get("availability_on_missing", "unsupported")
            )
            for rule in rules:
                endpoint_id = str(rule.get("endpoint", ""))
                if endpoint_id in endpoint_errors:
                    last_error = endpoint_errors[endpoint_id]
                    continue
                result = self.endpoint_results.get(endpoint_id)
                if result is None or not self._conditions_met(rule, result.payload):
                    continue
                try:
                    raw = self._extract(rule, result)
                except ExtractionError as exc:
                    last_error = str(exc)
                    continue
                if _is_missing(raw):
                    continue
                try:
                    value = raw
                    for transform in rule.get("transforms", []):
                        value = self._transform(
                            transform, value, rule.get("transform_options") or {}
                        )
                except ExtractionError as exc:
                    last_error = str(exc)
                    continue
                if _is_missing(value):
                    continue

                if target_metric:
                    spec = METRICS.get(str(target_metric))
                    if spec is None:
                        continue
                    model.set_metric(
                        spec.domain,
                        spec.id,
                        value=value,
                        source_value=raw,
                        source=endpoint_id,
                    )
                else:
                    model.set_collection(
                        str(target_collection), list(value), source=endpoint_id
                    )
                succeeded = True
                break

            if succeeded:
                continue
            availability = Availability.ERROR.value if last_error else availability_on_missing
            if target_metric:
                spec = METRICS.get(str(target_metric))
                if spec is not None:
                    model.set_metric(
                        spec.domain,
                        spec.id,
                        value=None,
                        availability=availability,
                        source_value=None,
                        source=rules[0].get("endpoint"),
                        error=last_error,
                    )
            elif target_collection:
                model.set_collection(
                    str(target_collection),
                    [],
                    loaded=False,
                    source=rules[0].get("endpoint"),
                    error=last_error,
                )

    def _apply_derived_aggregates(self, model: UniversalModel) -> None:
        devices_collection = model.collections.get("devices")
        if devices_collection is not None:
            if devices_collection.loaded:
                devices = []
                for item in devices_collection.items:
                    identifier = (
                        item.get("id") or item.get("mac_address") or item.get("ip_address")
                    )
                    if not identifier:
                        continue
                    devices.append(ConnectedDevice(
                        id=str(identifier),
                        hostname=item.get("hostname"),
                        ip_address=item.get("ip_address"),
                        mac_address=item.get("mac_address"),
                        link_type=item.get("link_type"),
                        address_source=item.get("address_source"),
                        uptime_s=item.get("uptime_s"),
                        raw=dict(item.get("raw") or item),
                    ))
                model.set_devices(devices, source=devices_collection.source)
            else:
                model.set_devices(
                    None,
                    loaded=False,
                    source=devices_collection.source,
                    error=devices_collection.error,
                )

        voip_ids = model.domains.get(Domain.VOIP, {})
        if voip_ids:
            phone = model.get_metric(Domain.VOIP, "voip.phone_number")
            registration = model.get_metric(Domain.VOIP, "voip.registration_state")
            line_status = model.get_metric(Domain.VOIP, "voip.line_status")
            model.set_primary_voip_line(
                phone_number=phone.value,
                phone_availability=phone.availability,
                phone_source_value=phone.source_value,
                registration_state=registration.value,
                registration_availability=registration.availability,
                line_status=line_status.value,
                line_status_availability=line_status.availability,
                source=phone.source,
                error=phone.error,
            )

    @staticmethod
    def _valid_value(rule: Dict[str, Any], value: Any) -> Tuple[bool, str]:
        name = rule.get("rule")
        options = rule.get("options") or {}
        if name == "non_empty":
            return (not _is_missing(value), "value is empty")
        if name == "ipv4":
            try:
                ipaddress.IPv4Address(str(value))
                return True, ""
            except ipaddress.AddressValueError:
                return False, "value is not an IPv4 address"
        if name == "ipv6":
            try:
                ipaddress.IPv6Address(str(value))
                return True, ""
            except ipaddress.AddressValueError:
                return False, "value is not an IPv6 address"
        if name == "mac":
            valid = bool(re.fullmatch(r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}", str(value)))
            return valid, "value is not a MAC address"
        if name in ("number", "duration_seconds"):
            try:
                number = float(str(value))
                if name == "duration_seconds" and number < 0:
                    return False, "duration cannot be negative"
                return True, ""
            except (TypeError, ValueError):
                return False, "value is not numeric"
        if name == "dbm_range":
            try:
                number = float(str(value))
                lower = float(options.get("min", -200))
                upper = float(options.get("max", 200))
                return lower <= number <= upper, "value is outside the expected range"
            except (TypeError, ValueError):
                return False, "value is not numeric"
        if name == "one_of":
            allowed = {str(item) for item in options.get("values", [])}
            return str(value) in allowed, "value is not one of the allowed values"
        if name == "phone_e164":
            return normalize_phone_number(value) is not None, "value is not a valid phone number"
        return True, ""

    def _apply_validation(self, model: UniversalModel) -> None:
        for rule in self.connector.get("validation", []):
            if not isinstance(rule, dict):
                continue
            metric_id = str(rule.get("metric", ""))
            spec = METRICS.get(metric_id)
            if spec is None:
                continue
            metric = model.domains[spec.domain].get(metric_id)
            if metric is None or metric.availability not in (Availability.AVAILABLE, Availability.STALE):
                continue
            valid, reason = self._valid_value(rule, metric.value)
            if valid:
                continue
            availability = rule.get("on_failure", Availability.INVALID.value)
            model.set_metric(
                spec.domain,
                metric_id,
                value=None,
                availability=availability,
                source_value=metric.source_value,
                source=metric.source,
                observed_at=metric.observed_at,
                error=reason,
            )

    # ------------------------------------------------------------------
    # Explicit logout
    # ------------------------------------------------------------------
    def logout(self) -> bool:
        if not self.logout_endpoint:
            return False
        was_authenticated = self.authenticated
        if not was_authenticated:
            return False
        try:
            for dependency in self.dependencies.get(self.logout_endpoint, []):
                if dependency not in self.endpoint_results:
                    self.execute_endpoint(dependency)
            self.execute_endpoint(self.logout_endpoint)
        except EndpointError:
            return False
        finally:
            self._reset_auth_state()
        return True
