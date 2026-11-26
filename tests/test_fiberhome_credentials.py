"""Regression tests for the single, user-controlled login credential."""

import importlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from auth_handlers import AuthenticationError
from tests.fixtures import manager_for_catalog, synthetic_connector


ROOT = Path(__file__).resolve().parents[1]
GOOD_PASSWORD = 'user1234'
OBSOLETE_PASSWORD = ('user123' + '456')


class FiberHomeCredentialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        detection = {"gateway": "192.168.1.1", "model": "", "detected": False}
        with patch("gateway.detect_gateway", return_value=detection):
            cls.app_module = importlib.import_module("app")

    def setUp(self):
        self.original_state = dict(self.app_module.state)
        self.client = self.app_module.app.test_client()

    def tearDown(self):
        self.app_module.state.clear()
        self.app_module.state.update(self.original_state)

    def _connect(self, password, manager, attempts):
        discovery = {
            "gateway": "192.168.1.1",
            "gateway_detected": True,
            "selected_profile": "aurora.x1",
        }

        def fake_connect(self):
            attempts.append(self.credentials["password"])
            raise AuthenticationError("bad credentials")

        with patch.object(self.app_module, "run_discovery", return_value=discovery), \
             patch.object(self.app_module.ConnectorExecutor, "connect", fake_connect):
            return self.client.post("/api/connect", json={
                "gateway": "192.168.1.1",
                "username": "user",
                "password": password,
            })

    def test_only_one_default_password_is_configured(self):
        config = json.loads(
            (ROOT / "config" / "default_credentials.json").read_text(encoding="utf-8")
        )
        suggestion = next(
            item for item in config["credential_suggestions"]
            if item["key"] == "fiberhome_observed_defaults"
        )
        self.assertEqual(suggestion["default_username"], "user")
        self.assertEqual(suggestion["default_password"], GOOD_PASSWORD)
        self.assertNotIn("fallback_passwords", suggestion)
        payload = json.dumps(config)
        self.assertNotIn(OBSOLETE_PASSWORD, payload)

    def test_login_defaults_return_the_single_correct_password(self):
        discovery = {
            "gateway": "192.168.1.1",
            "gateway_detected": True,
            "selected_profile": "fiberhome.hg6145f1",
        }
        with patch.object(self.app_module, "run_discovery", return_value=discovery):
            payload = self.client.get("/api/login-defaults").get_json()
        self.assertEqual(payload["username"], "user")
        self.assertEqual(payload["password"], GOOD_PASSWORD)
        self.assertNotIn(OBSOLETE_PASSWORD, json.dumps(payload))

    def test_submitted_password_is_used_exactly_once(self):
        attempts = []
        catalog = {"schema_version": 1, "connectors": [synthetic_connector()]}
        with manager_for_catalog(catalog) as manager:
            self.app_module.state["manager"] = manager
            response = self._connect(GOOD_PASSWORD, manager, attempts)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(attempts, [GOOD_PASSWORD])

    def test_unsupported_manufacturer_gets_no_credentials(self):
        discovery = {
            "gateway": "10.0.0.1",
            "gateway_detected": True,
            "selected_profile": None,
        }
        with patch.object(self.app_module, "run_discovery", return_value=discovery):
            payload = self.client.get(
                "/api/login-defaults?manufacturer_id=unknown_vendor"
            ).get_json()
        self.assertEqual(payload["username"], "")
        self.assertEqual(payload["password"], "")
        self.assertNotIn(GOOD_PASSWORD, json.dumps(payload))

        config = json.loads(
            (ROOT / "config" / "default_credentials.json").read_text(encoding="utf-8")
        )
        generic = next(
            item for item in config["credential_suggestions"]
            if item["key"] == "generic_user_supplied"
        )
        self.assertEqual(generic["default_username"], "")
        self.assertEqual(generic["default_password"], "")
        self.assertNotIn(GOOD_PASSWORD, json.dumps(generic))

    def test_no_silent_credential_substitution_in_app(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("fallback_passwords", source)
        self.assertNotIn(GOOD_PASSWORD, source)
        self.assertNotIn(OBSOLETE_PASSWORD, source)

    def test_frontend_preserves_user_edited_credentials(self):
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function applyLoginDefault(", script)
        self.assertIn("Never replace a value the user has edited.", script)
        self.assertNotIn("$('userInput').value = data.username", script)
        self.assertIn("field.value = value || ''", script)
        self.assertNotIn("$('passInput').value = data.password", script)


if __name__ == "__main__":
    unittest.main()
