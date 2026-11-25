"""Phase 2 M1 contract freeze tests.

These tests freeze contracts that already exist in the running application.
They do not change runtime behavior. Values below are derived from the current
implementation and the existing test fixtures; no new application behavior is
introduced.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from auth_handlers import HANDLERS  # noqa: E402
from connector_executor import ConnectorExecutor  # noqa: E402
from connector_manager import ConnectorManager  # noqa: E402
from gateway import fhencrypt  # noqa: E402
from models.enums import Availability, Domain  # noqa: E402
from models.universal import METRICS, UniversalModel  # noqa: E402
from tests.fixtures import (  # noqa: E402
    RecordingSession,
    find_call,
    manager_for_catalog,
    synthetic_connector,
    synthetic_payloads,
)

with patch("gateway.detect_gateway", return_value={
    "gateway": "192.168.1.1", "model": "", "detected": False,
}):
    import app as app_module  # noqa: E402


DETECTION = {"gateway": "192.168.1.1", "model": "", "detected": True}
IDLE_SPEEDTEST = {
    "status": "idle", "progress": "", "result": None, "error": None,
}
DONE_SPEEDTEST = {
    "status": "done",
    "progress": "",
    "result": {
        "download": 94.2,
        "upload": 41.7,
        "ping": 12.3,
        "jitter": 2.1,
        "server": "Test server",
        "sponsor": "Test ISP",
        "country": "Test country",
    },
    "error": None,
    "started_at": "2026-11-24T06:00:00+00:00",
    "completed_at": "2026-11-24T06:01:00+00:00",
}

EXPECTED_DOMAINS = [
    "device_identity", "wan", "optical", "voip", "system",
    "connected_devices", "speed_test",
]
EXPECTED_AVAILABILITY = [
    "available", "stale", "unavailable", "unsupported", "invalid",
    "not_loaded", "error",
]
EXPECTED_METRIC_IDS = {
    "device.manufacturer", "device.model", "device.firmware",
    "device.hardware", "device.serial", "device.gpon_sn", "device.operator",
    "device.mac_base", "device.mac_wan",
    "wan.connection_name", "wan.connection_type", "wan.status",
    "wan.public_ip", "wan.gateway_ip", "wan.dns_servers", "wan.vlan_id",
    "wan.pppoe_user", "wan.mtu", "wan.nat", "wan.uptime_s",
    "wan.last_error", "wan.mac",
    "optical.tx_power_dbm", "optical.rx_power_dbm",
    "optical.temperature_c", "optical.bias_current_ma",
    "optical.supply_voltage_v", "optical.pon_mode", "optical.onu_id",
    "optical.olt_vendor", "optical.registration_state",
    "optical.auth_status", "optical.loid", "optical.bytes_sent",
    "optical.bytes_received", "optical.packets_sent",
    "optical.packets_received",
    "voip.phone_number", "voip.registration_state", "voip.line_status",
    "voip.provider",
    "system.uptime_s", "system.cpu_percent", "system.memory_percent",
    "system.flash_percent", "system.lan_port_count", "system.uplink_mode",
    "system.kernel", "system.compile_time",
    "devices.count",
    "speed_test.status", "speed_test.download_mbps",
    "speed_test.upload_mbps", "speed_test.ping_ms", "speed_test.jitter_ms",
    "speed_test.server", "speed_test.sponsor", "speed_test.country",
    "speed_test.started_at", "speed_test.completed_at", "speed_test.error",
}
EXPECTED_AUTH_HANDLERS = {
    "none", "http_basic", "http_digest", "form", "session", "token",
    "fiberhome_aes",
}
EXPECTED_PUBLIC_DATA_KEYS = {
    "gateway", "domains", "devices", "devices_loaded", "collections",
    "voip_lines", "call_action", "endpoint_errors", "updated_at",
}
EXPECTED_METRIC_KEYS = {
    "id", "domain", "label", "type", "unit", "value", "source_value",
    "availability", "source", "observed_at", "error",
}
EXPECTED_DISCOVERY_KEYS = {
    "gateway", "detected", "gateway_detected", "detected_device",
    "selected_profile", "identification_confidence", "evidence",
    "credential_suggestion", "profiles", "discovery_errors",
    "physical_device_check_required",
}
EXPECTED_PROFILE_ROW_KEYS = {
    "id", "display_name", "status", "identification_confidence",
    "capabilities", "device_compatibility",
}
EXPECTED_SUGGESTION_KEYS = {
    "label", "username_source", "password_source",
    "requires_user_confirmation", "provenance",
}
EXPECTED_LOGIN_DEFAULTS_KEYS = {
    "gateway", "gateway_detected", "supported", "username", "password",
    "message",
}
EXPECTED_SPEEDTEST_STATUS_KEYS = {
    "status", "progress", "result", "error", "started_at", "completed_at",
}
EXPECTED_SPEEDTEST_RESULT_KEYS = {
    "download", "upload", "ping", "jitter", "server", "sponsor", "country",
}
AES_SEED = "abcdefghijklmnopqrstuv"
AES_CIPHERTEXT = (
    "EE46080EA2CD73D22949926CE91A76D0C573FD0F32515DCADE46133111EA03FA"
)


class Phase2ContractTests(unittest.TestCase):
    def setUp(self):
        self.original_state = dict(app_module.state)
        self.client = app_module.app.test_client()

    def tearDown(self):
        app_module.state.clear()
        app_module.state.update(self.original_state)

    def _connected_synthetic_executor(self):
        catalog = {"schema_version": 1, "connectors": [synthetic_connector()]}
        manager_context = manager_for_catalog(catalog)
        manager = manager_context.__enter__()
        self.addCleanup(manager_context.__exit__, None, None, None)
        session = RecordingSession(synthetic_payloads())
        executor = ConnectorExecutor(
            manager.get("aurora.x1"),
            "192.168.5.1",
            {"username": "tester", "password": "unit-test-password"},
            manager=manager,
            session=session,
        )
        executor.connect()
        return manager, executor

    def _connect(self, **overrides):
        payload = {
            "gateway": "192.168.1.1",
            "username": "tester",
            "password": "unit-test-password",
        }
        payload.update(overrides)
        discovery = {
            "gateway": "192.168.1.1",
            "gateway_detected": True,
            "detected": True,
            "detected_device": "HG6145F1",
            "selected_profile": "fiberhome.hg6145f1",
        }

        class RecordingExecutor:
            def __init__(self, profile, host, credentials, manager=None, session=None):
                self.profile = profile
                self.host = host
                self.credentials = credentials

            def connect(self):
                return {}

        # Direct assignment preserves route state changes for assertions;
        # setUp/tearDown restore the original state after each test.
        app_module.state["manager"] = ConnectorManager()
        app_module.state["discovery"] = None
        with patch.object(app_module, "run_discovery", return_value=discovery), \
                patch.object(app_module, "ConnectorExecutor", RecordingExecutor):
            return self.client.post("/api/connect", json=payload)

    # ---------- 1. /api/data and metric IDs ----------
    def test_api_data_structure_and_stable_metric_ids(self):
        _, executor = self._connected_synthetic_executor()
        with patch.dict(app_module.state, {
            "executor": executor,
            "connected": True,
            "speedtest": dict(IDLE_SPEEDTEST),
        }):
            response = self.client.get("/api/data")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(set(payload), EXPECTED_PUBLIC_DATA_KEYS)
        self.assertEqual(set(payload["domains"]), set(EXPECTED_DOMAINS))
        self.assertEqual(set(METRICS), EXPECTED_METRIC_IDS)

        for domain, metrics in payload["domains"].items():
            self.assertIn(domain, EXPECTED_DOMAINS)
            for metric_id, metric in metrics.items():
                self.assertIn(metric_id, EXPECTED_METRIC_IDS)
                self.assertEqual(set(metric), EXPECTED_METRIC_KEYS)
                self.assertEqual(metric["id"], metric_id)
                self.assertEqual(metric["domain"], domain)
                self.assertEqual(METRICS[metric_id].domain.value, domain)

        emitted = {
            metric_id for metrics in payload["domains"].values()
            for metric_id in metrics
        }
        self.assertTrue({
            "device.manufacturer", "device.model", "wan.public_ip",
            "wan.status", "optical.tx_power_dbm", "voip.phone_number",
            "system.uptime_s", "devices.count", "speed_test.status",
        }.issubset(emitted))
        self.assertEqual(payload["gateway"], "192.168.5.1")
        self.assertTrue(payload["devices_loaded"])

    # ---------- 2. model vocabulary ----------
    def test_model_domains_and_availability_vocabulary_are_frozen(self):
        self.assertEqual([domain.value for domain in Domain], EXPECTED_DOMAINS)
        self.assertEqual(
            [availability.value for availability in Availability],
            EXPECTED_AVAILABILITY,
        )
        self.assertEqual(len(EXPECTED_DOMAINS), 7)
        self.assertEqual(len(EXPECTED_METRIC_IDS), 61)
        for spec in METRICS.values():
            self.assertIn(spec.domain.value, EXPECTED_DOMAINS)

    # ---------- 3. auth handler identifiers ----------
    def test_authentication_handler_identifiers_are_frozen(self):
        self.assertTrue(EXPECTED_AUTH_HANDLERS.issubset(set(HANDLERS)))
        self.assertIn("huawei_nonce_form", HANDLERS)
        for identifier, handler in HANDLERS.items():
            self.assertEqual(handler.handler_type, identifier)

    # ---------- 4. FiberHome AES vector ----------
    def test_fiberhome_aes_vector_and_handler_flow(self):
        encrypted = fhencrypt("contract-password", AES_SEED)
        self.assertEqual(encrypted, AES_CIPHERTEXT)

        payloads = {
            "get_acs_random": {"acsRandom": AES_SEED, "sessionid": "SESS"},
            "do_login": {
                "login_result": 0, "sessionid": "SESS", "session_valid": 1,
            },
            "get_login_user": {"login_user": "2"},
            "do_logout": {},
        }
        session = RecordingSession(payloads)
        manager = ConnectorManager()
        executor = ConnectorExecutor(
            manager.get("fiberhome.hg6145f1"),
            "192.168.1.1",
            {"username": "user", "password": "contract-password"},
            manager=manager,
            session=session,
        )
        executor.connect()

        self.assertEqual(executor.derived["encrypted_password"], AES_CIPHERTEXT)
        self.assertEqual(executor.state["sessionid"], "SESS")
        login = find_call(session.calls, "do_login")
        self.assertIsNotNone(login)
        self.assertEqual(login["data"]["loginpd"], AES_CIPHERTEXT)
        self.assertEqual(login["data"]["sessionid"], "SESS")
        self.assertNotIn("contract-password", json.dumps(login["data"]))

    # ---------- 5. /api/discovery ----------
    def test_discovery_route_response_structure(self):
        probe = {
            "observations": {"challenge": {"Vendor": "Aurora X1"}},
            "errors": [],
        }
        catalog = {"schema_version": 1, "connectors": [synthetic_connector()]}
        with manager_for_catalog(catalog) as manager, \
                patch.dict(app_module.state, {
                    "manager": manager, "discovery": None, "gateway": DETECTION,
                }), \
                patch.object(app_module, "detect_gateway", return_value=DETECTION), \
                patch.object(
                    app_module.ConnectorExecutor, "discover", return_value=probe
                ), \
                patch.object(
                    app_module.ConnectorExecutor, "connect",
                    side_effect=AssertionError("Discovery must not authenticate."),
                ):
            response = self.client.get("/api/discovery")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(set(payload), EXPECTED_DISCOVERY_KEYS)
        self.assertEqual(payload["selected_profile"], "aurora.x1")
        self.assertEqual(payload["identification_confidence"], "exact")
        self.assertEqual(payload["detected_device"], "Aurora X1")
        self.assertTrue(payload["physical_device_check_required"])
        self.assertEqual(payload["discovery_errors"], [])
        self.assertEqual(len(payload["profiles"]), 1)
        self.assertEqual(
            set(payload["profiles"][0]), EXPECTED_PROFILE_ROW_KEYS
        )
        self.assertEqual(
            payload["profiles"][0]["device_compatibility"], "not_evaluated"
        )
        self.assertEqual(
            set(payload["credential_suggestion"]), EXPECTED_SUGGESTION_KEYS
        )

    # ---------- 6. /api/login-defaults ----------
    def test_login_defaults_response_is_compatibility_safe(self):
        discovery = {
            "gateway": "192.168.1.1",
            "gateway_detected": True,
            "detected": True,
            "selected_profile": "fiberhome.hg6145f1",
            "detected_device": "HG6145F1",
            "identification_confidence": "partial",
            "evidence": "internal",
        }
        with patch.object(app_module, "run_discovery", return_value=discovery), \
                patch.object(
                    app_module.ConnectorExecutor, "connect",
                    side_effect=AssertionError("Defaults must not authenticate."),
                ):
            response = self.client.get("/api/login-defaults")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(set(payload), EXPECTED_LOGIN_DEFAULTS_KEYS)
        self.assertEqual(payload["gateway"], "192.168.1.1")
        self.assertTrue(payload["gateway_detected"])
        self.assertTrue(payload["supported"])
        self.assertEqual(payload["username"], 'user')
        self.assertEqual(payload["password"], 'user1234')
        self.assertIsNone(payload["message"])
        for hidden in (
            "profile", "profile_id", "selected_profile", "evidence",
            "manufacturer", "confidence", "discovery_errors", "profiles",
        ):
            self.assertNotIn(hidden, payload)

    def test_login_defaults_unsupported_message_is_preserved(self):
        discovery = {
            "gateway": "10.0.0.1",
            "gateway_detected": True,
            "detected": True,
            "selected_profile": None,
        }
        with patch.object(app_module, "run_discovery", return_value=discovery):
            payload = self.client.get("/api/login-defaults").get_json()

        self.assertEqual(set(payload), EXPECTED_LOGIN_DEFAULTS_KEYS)
        self.assertFalse(payload["supported"])
        self.assertEqual(payload["message"], app_module.UNSUPPORTED_GATEWAY_MESSAGE)
        self.assertEqual(payload["username"], '')
        self.assertEqual(payload["password"], '')

    # ---------- 7. VoIP call action ----------
    def test_voip_call_action_and_tel_uri_behavior(self):
        empty_action = UniversalModel().primary_call_action().to_dict()
        self.assertEqual(empty_action, {
            "enabled": False,
            "dial_uri": None,
            "reason": "Phone number has not been loaded.",
        })

        model = UniversalModel()
        line = model.set_primary_voip_line(
            phone_number="5551234",
            phone_availability=Availability.AVAILABLE,
        )
        self.assertEqual(line.call_action.to_dict(), {
            "enabled": True,
            "dial_uri": "tel:5551234",
            "reason": None,
        })

        invalid = UniversalModel()
        invalid_line = invalid.set_primary_voip_line(
            phone_number="javascript:alert(1)",
            phone_availability=Availability.AVAILABLE,
        )
        self.assertEqual(invalid_line.call_action.to_dict(), {
            "enabled": False,
            "dial_uri": None,
            "reason": "Phone number failed validation.",
        })

        _, executor = self._connected_synthetic_executor()
        with patch.dict(app_module.state, {
            "executor": executor,
            "connected": True,
            "speedtest": dict(IDLE_SPEEDTEST),
        }):
            payload = self.client.get("/api/data").get_json()
        self.assertEqual(payload["call_action"], {
            "enabled": True,
            "dial_uri": "tel:5551234",
            "reason": None,
        })

    # ---------- 8. Speedtest API and worker ----------
    def test_speedtest_api_worker_and_model_injection(self):
        class FakeExecutor:
            def collect(self):
                return UniversalModel()

        rules = {rule.rule for rule in app_module.app.url_map.iter_rules()}
        self.assertIn("/api/speedtest", rules)
        self.assertIn("/api/speedtest/status", rules)
        self.assertEqual(app_module.SPEEDTEST_TIMEOUT, 120)

        with patch.object(app_module, "_speed_worker") as worker:
            response = self.client.post("/api/speedtest")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), {"ok": True})
            worker.assert_called_once()
            status = self.client.get("/api/speedtest/status").get_json()
            self.assertEqual(
                set(status), EXPECTED_SPEEDTEST_STATUS_KEYS
            )
            self.assertEqual(status["status"], "starting")
            self.assertNotIn("token", status)

        with patch.dict(app_module.state, {"speedtest": dict(DONE_SPEEDTEST)}):
            status = self.client.get("/api/speedtest/status").get_json()
            self.assertEqual(set(status), EXPECTED_SPEEDTEST_STATUS_KEYS)
            self.assertEqual(status["status"], "done")
            self.assertEqual(
                set(status["result"]), EXPECTED_SPEEDTEST_RESULT_KEYS
            )
            self.assertEqual(status["result"]["download"], 94.2)

            with patch.dict(app_module.state, {
                "executor": FakeExecutor(), "connected": True,
            }):
                payload = self.client.get("/api/data").get_json()
            domain = payload["domains"]["speed_test"]
            self.assertEqual(domain["speed_test.status"]["value"], "done")
            self.assertEqual(domain["speed_test.download_mbps"]["value"], 94.2)
            self.assertEqual(domain["speed_test.upload_mbps"]["value"], 41.7)
            self.assertEqual(domain["speed_test.ping_ms"]["value"], 12.3)
            self.assertEqual(domain["speed_test.jitter_ms"]["value"], 2.1)
            self.assertEqual(domain["speed_test.server"]["value"], "Test server")
            self.assertEqual(domain["speed_test.sponsor"]["value"], "Test ISP")
            self.assertEqual(domain["speed_test.country"]["value"], "Test country")
            self.assertEqual(
                domain["speed_test.completed_at"]["value"],
                DONE_SPEEDTEST["completed_at"],
            )

    # ---------- 9. manufacturer selector ----------
    def test_manufacturer_catalog_endpoint_exposes_only_supported_manufacturers(self):
        catalog = json.loads(
            (ROOT / "config" / "equipment.json").read_text(encoding="utf-8")
        )
        response = self.client.get("/api/manufacturers")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(set(payload), {"ok", "manufacturers", "message"})
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["message"], "")
        # Only manufacturers with at least one usable enabled connector profile are advertised.
        self.assertEqual(
            payload["manufacturers"],
            [{"id": item["id"], "name": item["name"]}
             for item in catalog["manufacturers"] if item.get("profiles")],
        )
        self.assertNotIn("zte", [m["id"] for m in payload["manufacturers"]])

    def test_known_manufacturer_is_stored_and_reported(self):
        response = self._connect(manufacturer_id="huawei")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["manufacturer_id"], "huawei")
        self.assertEqual(app_module.state["manufacturer_id"], "huawei")
        session = self.client.get("/api/session").get_json()
        self.assertTrue(session["connected"])
        self.assertEqual(session["manufacturer_id"], "huawei")
        for hidden in ("profile", "profile_id", "physical_device_check_required"):
            self.assertNotIn(hidden, response.get_json())

    def test_legacy_manufacturer_alias_without_usable_profile_is_rejected(self):
        response = self._connect(manufacturer="zte")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])

    def test_legacy_connect_without_manufacturer_id_still_works(self):
        response = self._connect()

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["manufacturer_id"], "")
        self.assertEqual(app_module.state["manufacturer_id"], "")
        session = self.client.get("/api/session").get_json()
        self.assertTrue(session["connected"])
        self.assertEqual(session["manufacturer_id"], "")

    def test_unknown_manufacturer_is_rejected(self):
        response = self._connect(manufacturer_id="not-in-catalog")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])
        self.assertFalse(app_module.state["connected"])


if __name__ == "__main__":
    unittest.main()
