import importlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from connector_executor import ConnectorExecutor
from tests.fixtures import (
    RecordingSession,
    manager_for_catalog,
    synthetic_connector,
    synthetic_payloads,
)


ROOT = Path(__file__).resolve().parents[1]


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
                response = self.app_module.app.test_client().get("/api/data")

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["profile_id"], "aurora.x1")
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
            for legacy_key in ("device", "internet", "optical", "generic", "meta"):
                self.assertNotIn(legacy_key, payload)

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

    def test_static_ui_contains_only_universal_model_contract(self):
        template = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        for legacy in (
            "FiberHome", "HG6145F1", "genericPanel", "modeInput",
            "get_base_info", "get_allwan_info", "get_lan_status",
            "data.device", "data.internet", "raw.get",
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

        self.assertIn("addEventListener('click'", script)
        self.assertIn("window.location.href = uri", script)
        self.assertIn("dataset.dialUri", script)
        self.assertNotIn("location.href = 'tel:", script)

    def test_five_dashboard_sections_are_present(self):
        template = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        for section in (
            "speedPanel", "wanSection", "voipSection", "systemSection",
            "devicesSection",
        ):
            self.assertIn(f'id="{section}"', template)


if __name__ == "__main__":
    unittest.main()
