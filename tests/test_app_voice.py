import importlib
import unittest
from unittest.mock import patch


class FakeClient:
    def __init__(self, logout_result=(True, "Disconnected from router.")):
        self.logout_result = logout_result
        self.logged_out = False

    def logout(self):
        self.logged_out = True
        return self.logout_result


class AppVoiceTests(unittest.TestCase):
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

    def tearDown(self):
        self.app_module.state.clear()
        self.app_module.state.update(self.original_state)

    def test_build_view_exposes_voice_data_and_call_action(self):
        raw = {
            "get_voice_status": {
                "voice_status": {
                    "voice_port_num": 1,
                    "ServerType": "1",
                    "RegAndCallStatus1": "2",
                    "DirectoryNumber1": "213000000000",
                    "URI1": "",
                }
            }
        }
        view = self.app_module.build_view(raw)

        self.assertEqual(view["voip"]["phone_number"], "213000000000")
        self.assertEqual(view["voip"]["registration_state"], "IDLE")
        self.assertTrue(view["call_action"]["enabled"])
        self.assertEqual(view["call_action"]["dial_uri"], "tel:213000000000")

    def test_build_view_has_disabled_call_action_without_phone(self):
        view = self.app_module.build_view({})
        self.assertFalse(view["call_action"]["enabled"])
        self.assertIsNone(view["call_action"]["dial_uri"])

    def test_disconnect_logs_out_from_router_and_clears_state(self):
        client = FakeClient()
        self.app_module.state["client"] = client
        self.app_module.state["credentials"] = {"username": "user"}
        self.app_module.state["connected"] = True

        response = self.app_module.app.test_client().post("/api/disconnect")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["router_logout"])
        self.assertTrue(client.logged_out)
        self.assertIsNone(self.app_module.state["client"])
        self.assertIsNone(self.app_module.state["credentials"])
        self.assertFalse(self.app_module.state["connected"])

    def test_disconnect_still_reports_failure_when_router_logout_fails(self):
        client = FakeClient(logout_result=(False, "Router logout request failed."))
        self.app_module.state["client"] = client
        self.app_module.state["connected"] = True

        response = self.app_module.app.test_client().post("/api/disconnect")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(payload["router_logout"])
        self.assertIn("could not be ended", payload["message"])
        self.assertIsNone(self.app_module.state["client"])
        self.assertFalse(self.app_module.state["connected"])


if __name__ == "__main__":
    unittest.main()
