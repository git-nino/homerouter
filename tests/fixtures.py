"""Reusable, secret-free fixtures for connector integration tests."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional
from urllib.parse import urlparse

from connector_manager import DEFAULT_CREDENTIALS, DEFAULT_SCHEMA, ConnectorManager


class FakeResponse:
    """Minimal ``requests.Response`` surface used by the executor."""

    def __init__(
        self,
        payload: Dict[str, Any],
        status_code: int = 200,
        headers: Optional[Dict[str, str]] = None,
    ):
        self.status_code = status_code
        self.text = json.dumps(payload)
        self.headers = headers or {"Content-Type": "application/json"}


class RecordingSession:
    """Records requests and returns deterministic payloads keyed by ajaxmethod."""

    def __init__(
        self,
        payloads: Dict[str, Dict[str, Any]],
        *,
        status_overrides: Optional[Dict[str, Any]] = None,
        payload_overrides: Optional[Dict[str, Any]] = None,
    ):
        self.headers: Dict[str, str] = {}
        self.auth: Any = None
        self.calls = []
        self.payloads = {key: dict(value) for key, value in payloads.items()}
        self.status_overrides = dict(status_overrides or {})
        self.payload_overrides = dict(payload_overrides or {})
        self.counts: Dict[str, int] = {}
        self.failed_endpoints = set()

    def request(self, method, url, **kwargs):
        params = dict(kwargs.get("params") or {})
        data = dict(kwargs.get("data") or {})
        name = str(
            params.get("ajaxmethod")
            or data.get("ajaxmethod")
            or urlparse(url).path
        )
        index = self.counts.get(name, 0)
        self.counts[name] = index + 1
        self.calls.append({
            "method": method,
            "path": urlparse(url).path,
            "name": name,
            "params": params,
            "data": data,
            "headers": dict(kwargs.get("headers") or {}),
        })
        if name in self.failed_endpoints:
            return FakeResponse({}, status_code=500)

        status = self.status_overrides.get(name, 200)
        if isinstance(status, list):
            status = status[min(index, len(status) - 1)]

        if name in self.payload_overrides:
            variants = self.payload_overrides[name]
            payload = dict(variants[min(index, len(variants) - 1)])
        else:
            payload = dict(self.payloads.get(name, {}))
        return FakeResponse(payload, status_code=status)


def find_call(calls, name):
    return next((call for call in calls if call["name"] == name), None)


def synthetic_connector() -> Dict[str, Any]:
    """A second manufacturer profile with no FiberHome-specific fields."""

    return {
        "id": "aurora.x1",
        "display_name": "Aurora X1 (synthetic)",
        "status": "observed",
        "enabled": True,
        "evidence": {
            "source": "Synthetic integration-test fixture.",
            "verified": True,
            "notes": ["No physical device is implied by this fixture."],
        },
        "identification": {
            "confidence": "exact",
            "rules": [
                {
                    "id": "preauth-vendor",
                    "phase": "pre_auth",
                    "source": "challenge",
                    "field": "Vendor",
                    "operator": "equals",
                    "value": "Aurora X1",
                    "confidence": "exact",
                    "weight": 80,
                    "evidence": "Synthetic integration-test fixture.",
                }
            ],
        },
        "auth": {
            "flows": [
                {
                    "id": "session-login",
                    "type": "session",
                    "order": 1,
                    "endpoints": {"preflight": "challenge", "login": "login"},
                    "success": {"field": "ok", "allowed": [True]},
                    "session": {
                        "id_field": "session",
                        "store": True,
                        "validity_field": "authenticated",
                        "invalid_values": [False],
                    },
                }
            ],
            "logout_endpoint": "logout",
        },
        "endpoints": [
            {
                "id": "challenge",
                "method": "GET",
                "path": "/cgi-bin/ajax",
                "query": {"ajaxmethod": "challenge"},
                "format": "json",
                "timeout_seconds": 5,
                "auth": False,
                "description": "Pre-auth vendor identifier and login token.",
            },
            {
                "id": "login",
                "method": "POST",
                "path": "/cgi-bin/ajax",
                "body": {
                    "ajaxmethod": "login",
                    "username": {"source": "user_input"},
                    "password": {"source": "user_input"},
                    "token": {
                        "source": "endpoint_field",
                        "endpoint": "challenge",
                        "field": "token",
                    },
                },
                "format": "json",
                "timeout_seconds": 5,
                "auth": False,
                "description": "Session login using a token from the preflight endpoint.",
            },
            {
                "id": "status",
                "method": "GET",
                "path": "/cgi-bin/ajax",
                "query": {
                    "ajaxmethod": "status",
                    "session": {"source": "state.session"},
                    "view": {"source": "literal", "value": "full"},
                },
                "format": "json",
                "timeout_seconds": 5,
                "description": "Synthetic normalized status payload.",
            },
            {
                "id": "clients",
                "method": "GET",
                "path": "/cgi-bin/ajax",
                "query": {
                    "ajaxmethod": "clients",
                    "session": {"source": "state.session"},
                },
                "format": "json",
                "timeout_seconds": 5,
                "description": "Synthetic connected-client list.",
            },
            {
                "id": "logout",
                "method": "POST",
                "path": "/cgi-bin/ajax",
                "body": {
                    "ajaxmethod": "logout",
                    "session": {"source": "state.session"},
                },
                "format": "json",
                "timeout_seconds": 5,
                "description": "Explicit logout.",
            },
        ],
        "extraction": [
            {
                "id": "identity-manufacturer",
                "metric": "device.manufacturer",
                "endpoint": "challenge",
                "method": "literal",
                "expression": "Aurora",
            },
            {
                "id": "identity-model",
                "metric": "device.model",
                "endpoint": "status",
                "method": "json_path",
                "expression": "$.Model",
            },
            {
                "id": "identity-firmware",
                "metric": "device.firmware",
                "endpoint": "status",
                "method": "json_path",
                "expression": "$.Firmware",
            },
            {
                "id": "wan-public-ip",
                "metric": "wan.public_ip",
                "endpoint": "status",
                "method": "json_path",
                "expression": "$.WanIP",
                "availability_on_missing": "unavailable",
            },
            {
                "id": "wan-status",
                "metric": "wan.status",
                "endpoint": "status",
                "method": "json_path",
                "expression": "$.Link",
                "availability_on_missing": "unavailable",
            },
            {
                "id": "wan-uptime",
                "metric": "wan.uptime_s",
                "endpoint": "status",
                "method": "json_path",
                "expression": "$.Uptime",
                "transforms": ["integer"],
                "availability_on_missing": "unavailable",
            },
            {
                "id": "optical-tx",
                "metric": "optical.tx_power_dbm",
                "endpoint": "status",
                "method": "json_path",
                "expression": "$.TxPower",
                "transforms": ["number"],
                "availability_on_missing": "unavailable",
            },
            {
                "id": "optical-rx-missing",
                "metric": "optical.rx_power_dbm",
                "endpoint": "status",
                "method": "json_path",
                "expression": "$.NoReceivePowerField",
                "transforms": ["number"],
                "availability_on_missing": "unsupported",
            },
            {
                "id": "voip-number",
                "metric": "voip.phone_number",
                "endpoint": "status",
                "method": "json_path",
                "expression": "$.Phone",
                "availability_on_missing": "unavailable",
            },
            {
                "id": "voip-registration",
                "metric": "voip.registration_state",
                "endpoint": "status",
                "method": "json_path",
                "expression": "$.RegState",
                "availability_on_missing": "unavailable",
            },
            {
                "id": "system-uptime",
                "metric": "system.uptime_s",
                "endpoint": "status",
                "method": "json_path",
                "expression": "$.Uptime",
                "transforms": ["integer"],
                "availability_on_missing": "unavailable",
            },
            {
                "id": "system-memory",
                "metric": "system.memory_percent",
                "endpoint": "status",
                "method": "json_path",
                "expression": "$.MemoryUsed",
                "transforms": ["number"],
                "availability_on_missing": "unsupported",
            },
            {
                "id": "clients-list",
                "collection": "devices",
                "endpoint": "clients",
                "method": "list_objects",
                "expression": "$.clients",
                "item_fields": {
                    "id": {"field": "mac", "target": "id"},
                    "hostname": {"field": "host", "target": "hostname"},
                    "ip_address": {"field": "ip", "target": "ip_address"},
                    "mac_address": {"field": "mac", "target": "mac_address"},
                    "link_type": {"field": "link", "target": "link_type"},
                    "address_source": {"field": "source", "target": "address_source"},
                    "uptime_s": {
                        "field": "uptime",
                        "target": "uptime_s",
                        "transforms": ["integer"],
                    },
                    "raw": {"target": "raw"},
                },
                "availability_on_missing": "unavailable",
            },
        ],
        "validation": [
            {"metric": "device.model", "rule": "non_empty", "on_failure": "invalid"},
            {"metric": "wan.public_ip", "rule": "ipv4", "on_failure": "invalid"},
            {
                "metric": "voip.phone_number",
                "rule": "phone_e164",
                "on_failure": "invalid",
            },
            {
                "metric": "system.uptime_s",
                "rule": "duration_seconds",
                "on_failure": "invalid",
            },
        ],
        "dependencies": {
            "login": ["challenge"],
            "status": ["login"],
            "clients": ["login"],
            "logout": ["login"],
        },
        "capabilities": [
            "device_identity",
            "wan",
            "optical",
            "voip",
            "system",
            "connected_devices",
        ],
        "limitations": ["Synthetic fixture: no physical device is associated."],
    }


def synthetic_payloads() -> Dict[str, Dict[str, Any]]:
    return {
        "challenge": {"Vendor": "Aurora X1", "token": "C-123"},
        "login": {"ok": True, "session": "S-9", "authenticated": True},
        "status": {
            "Model": "X1",
            "Firmware": "1.0",
            "WanIP": "198.51.100.7",
            "Link": "Connected",
            "Uptime": "3600",
            "TxPower": -18.5,
            "MemoryUsed": 42.0,
            "Phone": "5551234",
            "RegState": "IDLE",
            "authenticated": True,
        },
        "clients": {
            "clients": [
                {
                    "mac": "AA:BB:CC:DD:EE:01",
                    "host": "laptop",
                    "ip": "192.168.5.20",
                    "link": "wifi",
                    "source": "DHCP",
                    "uptime": "120",
                }
            ]
        },
        "logout": {"ok": True},
    }


@contextmanager
def manager_for_catalog(catalog_data: Dict[str, Any]) -> Iterator[ConnectorManager]:
    """Write a temporary catalog and return a manager using the shipped schema."""

    import tempfile

    with tempfile.TemporaryDirectory() as temp:
        catalog_path = Path(temp) / "connectors.json"
        catalog_path.write_text(
            json.dumps(catalog_data, indent=2) + "\n", encoding="utf-8"
        )
        yield ConnectorManager(catalog_path, DEFAULT_CREDENTIALS, DEFAULT_SCHEMA)
