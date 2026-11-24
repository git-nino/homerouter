import importlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from auth_handlers import AuthenticationError
from connector_executor import ConnectorExecutor
from tests.fixtures import (
    RecordingSession,
    manager_for_catalog,
    synthetic_connector,
    synthetic_payloads,
)


ROOT = Path(__file__).resolve().parents[1]
UNSUPPORTED = (
    "Unsupported or unidentified gateway. This device does not yet have a "
    "compatible connector profile."
)


class FakeExecutor:
    def __init__(self, profile_id=None):
        self.profile_id = profile_id


class GenericDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        detection = {
            "gateway": "192.168.1.1",
            "model": "HG6145F1",
            "detected": True,
        }
        with patch("gateway.detect_gateway", return_value=detection):
            cls.app_module = importlib.import_module("app")

    def setUp(self):
        self.original_state = dict(self.app_module.state)
        self.catalog = {"schema_version": 1, "connectors": [synthetic_connector()]}
        self.payloads = synthetic_payloads()
        self.client = self.app_module.app.test_client()

    def tearDown(self):
        self.app_module.state.clear()
        self.app_module.state.update(self.original_state)

    def _connected_executor(self, manager):
        session = RecordingSession(self.payloads)
        executor = ConnectorExecutor(
            manager.get("aurora.x1"),
            "192.168.5.1",
            {"username": "tester", "password": "unit-test-password"},
            manager=manager,
            session=session,
        )
        executor.connect()
        return executor, session

    def test_api_gateway_detection_still_works(self):
        detection = {"gateway": "10.9.8.7", "model": "HG6145F1", "detected": True}
        with patch.object(self.app_module, "detect_gateway", return_value=detection) as detect, \
             patch.object(
                 self.app_module.ConnectorExecutor,
                 "discover",
                 return_value={"observations": {}, "errors": []},
             ):
            result = self.app_module.run_discovery(force=True)

        detect.assert_called_once_with()
        self.assertEqual(result["gateway"], "10.9.8.7")
        self.assertTrue(result["gateway_detected"])

    def test_api_data_serves_second_manufacturer_through_universal_model(self):
        with manager_for_catalog(self.catalog) as manager:
            executor, _ = self._connected_executor(manager)
            speedtest = {
                "status": "idle", "progress": "", "result": None, "error": None
            }
            with patch.dict(
                self.app_module.state,
                {"executor": executor, "connected": True, "speedtest": speedtest},
            ):
                response = self.client.get("/api/data")

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(
                payload["domains"]["device_identity"]["device.manufacturer"]["value"],
                "Aurora",
            )
            self.assertEqual(
                payload["domains"]["wan"]["wan.public_ip"]["value"],
                "198.51.100.7",
            )
            self.assertEqual(payload["devices"][0]["hostname"], "laptop")
            self.assertTrue(payload["call_action"]["enabled"])
            self.assertTrue(json.loads(json.dumps(payload)))
            for internal in ("profile_id", "compatibility"):
                self.assertNotIn(internal, payload)
            for legacy in ("device", "internet", "optical", "generic", "meta"):
                self.assertNotIn(legacy, payload)

    def test_discovery_selects_profile_without_authenticating(self):
        detection = {
            "gateway": "10.0.0.1",
            "model": "Aurora X1",
            "detected": True,
        }
        probe = {
            "observations": {"challenge": {"Vendor": "Aurora X1"}},
            "errors": [],
        }
        with manager_for_catalog(self.catalog) as manager:
            with patch.object(self.app_module, "detect_gateway", return_value=detection), \
                 patch.object(
                     self.app_module.ConnectorExecutor, "discover", return_value=probe
                 ), \
                 patch.object(
                     self.app_module.ConnectorExecutor,
                     "connect",
                     side_effect=AssertionError("Discovery must not authenticate."),
                 ):
                with patch.dict(
                    self.app_module.state,
                    {"manager": manager, "discovery": None, "gateway": detection},
                ):
                    result = self.app_module.run_discovery(force=True)

            self.assertEqual(result["selected_profile"], "aurora.x1")
            self.assertEqual(result["detected_device"], "Aurora X1")
            self.assertEqual(result["identification_confidence"], "exact")
            self.assertTrue(result["physical_device_check_required"])
            self.assertEqual(result["gateway"], "10.0.0.1")

    def test_unknown_firmware_does_not_auto_select_a_profile(self):
        detection = {"gateway": "10.0.0.1", "model": "", "detected": True}
        probe = {
            "observations": {"challenge": {"Vendor": "Unknown Device"}},
            "errors": [],
        }
        with manager_for_catalog(self.catalog) as manager:
            with patch.object(self.app_module, "detect_gateway", return_value=detection), \
                 patch.object(
                     self.app_module.ConnectorExecutor, "discover", return_value=probe
                 ):
                with patch.dict(
                    self.app_module.state,
                    {"manager": manager, "discovery": None, "gateway": detection},
                ):
                    result = self.app_module.run_discovery(force=True)

            self.assertIsNone(result["selected_profile"])
            self.assertIn("Unknown firmware", result["evidence"])
            self.assertTrue(result["credential_suggestion"]["requires_user_confirmation"])

    def test_login_defaults_are_prefilled_without_authenticating(self):
        discovery = {
            "gateway": "192.168.1.1",
            "gateway_detected": True,
            "detected": True,
            "selected_profile": "fiberhome.hg6145f1",
            "detected_device": "HG6145F1",
            "identification_confidence": "partial",
            "evidence": "internal",
        }
        with patch.object(self.app_module, "run_discovery", return_value=discovery), \
             patch.object(
                 self.app_module.ConnectorExecutor,
                 "connect",
                 side_effect=AssertionError("Login defaults must not authenticate."),
             ):
            response = self.client.get("/api/login-defaults")

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["gateway"], "192.168.1.1")
        self.assertEqual(payload["username"], 'user')
        self.assertEqual(payload["password"], 'user1234')
        self.assertTrue(payload["supported"])
        self.assertIsNone(payload["message"])
        for internal in (
            "profile", "profile_id", "selected_profile", "evidence",
            "manufacturer", "confidence", "discovery_errors",
        ):
            self.assertNotIn(internal, payload)

    def test_unsupported_gateway_login_defaults_are_simple(self):
        discovery = {
            "gateway": "10.0.0.1",
            "gateway_detected": True,
            "detected": True,
            "selected_profile": None,
        }
        with patch.object(self.app_module, "run_discovery", return_value=discovery):
            response = self.client.get("/api/login-defaults")

        payload = response.get_json()
        self.assertFalse(payload["supported"])
        self.assertEqual(payload["message"], UNSUPPORTED)
        self.assertEqual(payload["username"], '')
        self.assertEqual(payload["password"], '')
        for internal in ("profile", "evidence", "confidence", "profiles"):
            self.assertNotIn(internal, payload)

    def test_credential_defaults_are_config_backed_with_provenance(self):
        config = json.loads(
            (ROOT / "config" / "default_credentials.json").read_text(encoding="utf-8")
        )
        suggestion = next(
            item for item in config["credential_suggestions"]
            if item["key"] == "fiberhome_observed_defaults"
        )
        self.assertEqual(suggestion["default_username"], "user")
        self.assertEqual(suggestion["default_password"], "user1234")
        self.assertNotIn("fallback_passwords", suggestion)
        self.assertIn("3bd1d55", suggestion["provenance"])
        generic = next(
            item for item in config["credential_suggestions"]
            if item["key"] == "generic_user_supplied"
        )
        self.assertEqual(generic["default_username"], "")
        self.assertEqual(generic["default_password"], "")
        self.assertNotIn("user1234", json.dumps(generic))

        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("user1234", app_source)
        self.assertNotIn(("user123" + "456"), app_source)

    def test_connect_resolves_connector_internally_and_ignores_client_profile_id(self):
        discovery = {
            "gateway": "192.168.5.1",
            "gateway_detected": True,
            "selected_profile": "aurora.x1",
            "detected_device": "Aurora X1",
        }
        attempts = []

        class RecordingExecutor:
            def __init__(self, profile, host, credentials, manager=None, session=None):
                attempts.append({
                    "profile_id": profile.get("id"),
                    "host": host,
                    "credentials": credentials,
                })

            def connect(self):
                return {}

        with manager_for_catalog(self.catalog) as manager:
            with patch.dict(self.app_module.state, {"manager": manager}), \
                 patch.object(self.app_module, "run_discovery", return_value=discovery), \
                 patch.object(self.app_module, "ConnectorExecutor", RecordingExecutor):
                response = self.client.post("/api/connect", json={
                    "gateway": "192.168.5.1",
                    "username": "tester",
                    "password": "unit-test-password",
                    "profile_id": "bogus-profile",
                })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["profile_id"], "aurora.x1")
        self.assertEqual(attempts[0]["credentials"]["username"], "tester")
        payload = response.get_json()
        self.assertEqual(payload["message"], "Connected.")
        for internal in ("profile", "profile_id", "physical_device_check_required"):
            self.assertNotIn(internal, payload)

    def test_user_override_credentials_are_used(self):
        discovery = {
            "gateway": "192.168.5.1",
            "gateway_detected": True,
            "selected_profile": "aurora.x1",
        }
        attempts = []

        class RecordingExecutor:
            def __init__(self, profile, host, credentials, manager=None, session=None):
                attempts.append(credentials)

            def connect(self):
                return {}

        with manager_for_catalog(self.catalog) as manager:
            with patch.dict(self.app_module.state, {"manager": manager}), \
                 patch.object(self.app_module, "run_discovery", return_value=discovery), \
                 patch.object(self.app_module, "ConnectorExecutor", RecordingExecutor):
                response = self.client.post("/api/connect", json={
                    "gateway": "192.168.5.1",
                    "username": "someone-else",
                    "password": "typed-by-user",
                })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(attempts, [{
            "username": "someone-else", "password": "typed-by-user"
        }])

    def test_fiberhome_connect_uses_submitted_password_once(self):
        discovery = {
            "gateway": "192.168.1.1",
            "gateway_detected": True,
            "selected_profile": "fiberhome.hg6145f1",
        }
        passwords = []

        def fake_connect(self):
            passwords.append(self.credentials["password"])
            raise AuthenticationError("bad credentials")

        with patch.object(self.app_module, "run_discovery", return_value=discovery), \
             patch.object(self.app_module.ConnectorExecutor, "connect", fake_connect):
            response = self.client.post("/api/connect", json={
                "gateway": "192.168.1.1",
                "username": "user",
                "password": 'user1234',
            })

        self.assertEqual(response.status_code, 401)
        self.assertEqual(passwords, ['user1234'])

    def test_unsupported_connect_does_not_authenticate(self):
        discovery = {
            "gateway": "10.0.0.1",
            "gateway_detected": True,
            "selected_profile": None,
        }
        with patch.object(self.app_module, "run_discovery", return_value=discovery), \
             patch.object(
                 self.app_module.ConnectorExecutor,
                 "connect",
                 side_effect=AssertionError("Unsupported gateway must not authenticate."),
             ):
            response = self.client.post("/api/connect", json={
                "gateway": "10.0.0.1",
                "username": "user",
                "password": "user1234",
            })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], UNSUPPORTED)

    def test_session_payload_has_no_connector_details(self):
        with patch.dict(self.app_module.state, {
            "connected": True,
            "executor": FakeExecutor("aurora.x1"),
        }):
            response = self.client.get("/api/session")

        payload = response.get_json()
        self.assertTrue(payload["connected"])
        for internal in ("profile", "profile_id", "physical_device_check_required"):
            self.assertNotIn(internal, payload)

    def test_static_ui_contains_only_universal_model_contract(self):
        template = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        for legacy in (
            "FiberHome", "HG6145F1", "genericPanel", "modeInput",
            "profileInput", "profile_id", "selected_profile",
            "get_base_info", "get_allwan_info", "get_lan_status",
            "data.device", "data.internet", "raw.get",
            "discoveryBox", "detectedDevice", "credentialHint",
        ):
            self.assertNotIn(legacy, template, legacy)
            self.assertNotIn(legacy, script, legacy)

        for legacy in ("RouterClient", "build_view", "get_base_info", "get_allwan_info"):
            self.assertNotIn(legacy, app_source, legacy)

        metric_ids = [
            "device.manufacturer", "device.model", "wan.public_ip", "wan.status",
            "wan.uptime_s", "optical.tx_power_dbm", "optical.rx_power_dbm",
            "voip.phone_number", "voip.registration_state", "system.uptime_s",
            "system.memory_percent", "devices.count",
        ]
        for metric_id in metric_ids:
            self.assertIn(metric_id, script, metric_id)

        for interaction in (
            "addEventListener('submit'", "addEventListener('click'",
            "window.location.href = uri", "dataset.dialUri",
            "setInterval(refreshData, 5000)", "/api/disconnect",
            "/api/speedtest", "/api/login-defaults",
        ):
            self.assertIn(interaction, script, interaction)
        self.assertNotIn("location.href = 'tel:", script)
        self.assertIn("stale — refresh failed", script)

    def test_restored_dashboard_sections_are_present(self):
        template = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        for section in (
            "speedPanel", "statGrid", "voipPanel", "wanTable", "ponTable",
            "systemTable", "lanTable", "deviceTable",
        ):
            self.assertIn(f'id="{section}"', template)

    def test_pwa_assets_are_served_locally(self):
        for path, content_type in (
            ("/static/favicon.png", "image/png"),
            ("/static/icon-192.png", "image/png"),
            ("/static/icon-512.png", "image/png"),
            ("/static/dashboard-icons-router.svg", "image/svg+xml"),
            ("/static/manifest.webmanifest", "application/manifest+json"),
        ):
            response = self.client.get(path)
            try:
                self.assertEqual(response.status_code, 200, path)
                self.assertIn(content_type, response.headers.get("Content-Type", ""), path)
            finally:
                response.close()


if __name__ == "__main__":
    unittest.main()
