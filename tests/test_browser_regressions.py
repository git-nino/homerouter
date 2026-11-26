import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]

with patch("gateway.detect_gateway", return_value={"gateway": "192.168.1.1", "model": "", "detected": False}):
    import app as app_module


class BrowserRegressionTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()
        self.original = {k: (dict(v) if isinstance(v, dict) else v) for k, v in app_module.state.items()}
        app_module.state.update({
            "connected": False, "executor": None, "credentials": None,
            "manufacturer_id": None, "username": None,
            "gateway": {"gateway": "192.168.1.1", "model": "", "detected": False},
        })

    def tearDown(self):
        app_module.state.clear()
        app_module.state.update(self.original)

    def test_manufacturers_include_supported_and_placeholder_zte(self):
        payload = self.client.get("/api/manufacturers").get_json()
        ids = [m["id"] for m in payload["manufacturers"]]
        self.assertEqual(ids, ["fiberhome", "huawei", "zte"])

    def test_login_defaults_switch_by_manufacturer(self):
        fh = self.client.get("/api/login-defaults?manufacturer_id=fiberhome").get_json()
        hw = self.client.get("/api/login-defaults?manufacturer_id=huawei").get_json()
        fh2 = self.client.get("/api/login-defaults?manufacturer_id=fiberhome").get_json()
        self.assertEqual((fh["gateway"], fh["username"], fh["password"]), ("192.168.1.1", "user", "user1234"))
        self.assertEqual((hw["gateway"], hw["username"], hw["password"]), ("192.168.100.1", "root", ""))
        self.assertEqual((fh2["gateway"], fh2["username"], fh2["password"]), ("192.168.1.1", "user", "user1234"))

    def test_unsupported_zte_connect_is_explicit_and_does_not_connect(self):
        resp = self.client.post("/api/connect", json={
            "gateway": "192.168.1.1", "username": "u", "password": "p", "manufacturer_id": "zte"
        })
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertFalse(body["ok"])
        self.assertIn("not yet supported", body["message"].lower())
        self.assertFalse(app_module.state["connected"])
        self.assertIsNone(app_module.state["executor"])

    def test_session_restores_non_default_gateway_and_manufacturer(self):
        model = MagicMock()
        model.to_dict.return_value = {
            "gateway": "10.0.100.220", "domains": {}, "devices": [], "devices_loaded": True,
            "collections": {}, "voip_lines": [], "endpoint_errors": [], "updated_at": None,
            "profile_id": "huawei.hg8145x6", "compatibility": {},
        }
        discovery = {"selected_profile": "huawei.hg8145x6", "gateway": "10.0.100.220", "detected": True, "gateway_detected": True}
        class Ex:
            def __init__(self, *a, **k): pass
            def connect(self): return None
        with patch.object(app_module, "run_discovery", return_value=discovery), \
             patch.object(app_module, "ConnectorExecutor", Ex):
            resp = self.client.post("/api/connect", json={
                "gateway": "10.0.100.220", "username": "root", "password": "x", "manufacturer_id": "huawei"
            })
        self.assertEqual(resp.status_code, 200)
        session = self.client.get("/api/session").get_json()
        self.assertTrue(session["connected"])
        self.assertEqual(session["gateway"], "10.0.100.220")
        self.assertEqual(session["manufacturer_id"], "huawei")

    def test_api_data_exposes_wan_voip_and_replaces_devices_on_poll(self):
        calls = {"n": 0}
        class Ex:
            def collect(self):
                calls["n"] += 1
                m = MagicMock()
                m.to_dict.return_value = {
                    "gateway": "10.0.100.220",
                    "domains": {
                        "wan": {"wan.status": {"availability": "available", "value": "Connected"}},
                        "voip": {"voip.registration_state": {"availability": "available", "value": "Up"}},
                    },
                    "devices": [{"id": f"d{calls['n']}"}],
                    "devices_loaded": True,
                    "collections": {}, "voip_lines": [], "endpoint_errors": [],
                    "updated_at": None, "profile_id": "huawei.hg8145x6", "compatibility": {},
                }
                return m
        app_module.state.update({
            "connected": True, "executor": Ex(), "credentials": {"username":"r","password":"x","profile_id":"huawei.hg8145x6"},
            "manufacturer_id": "huawei", "gateway": {"gateway":"10.0.100.220","model":"","detected":True},
        })
        p1 = self.client.get("/api/data").get_json()
        p2 = self.client.get("/api/data").get_json()
        self.assertEqual(p1["domains"]["wan"]["wan.status"]["value"], "Connected")
        self.assertEqual(p1["domains"]["voip"]["voip.registration_state"]["value"], "Up")
        self.assertEqual([d["id"] for d in p1["devices"]], ["d1"])
        self.assertEqual([d["id"] for d in p2["devices"]], ["d2"])  # replaced, not appended


if __name__ == "__main__":
    unittest.main()
