import copy
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import auth_handlers
import connector_executor
import connector_manager
from connector_executor import ConnectorExecutor, EndpointError, SessionExpired
from gateway import fhencrypt
from models.enums import Availability
from tests.fixtures import (
    RecordingSession,
    find_call,
    manager_for_catalog,
    synthetic_connector,
    synthetic_payloads,
)


class SyntheticProfileExecutorTests(unittest.TestCase):
    def setUp(self):
        self.catalog = {"schema_version": 1, "connectors": [synthetic_connector()]}
        self.payloads = synthetic_payloads()

    def _executor(self, manager, session=None):
        session = session or RecordingSession(self.payloads)
        executor = ConnectorExecutor(
            manager.get("aurora.x1"),
            "192.168.5.1",
            {"username": "tester", "password": "unit-test-password"},
            manager=manager,
            session=session,
        )
        return executor, session

    def test_profile_loads_validates_and_selects_from_discovery_observation(self):
        with manager_for_catalog(self.catalog) as manager:
            report = manager.validate()
            self.assertTrue(
                report.structural_valid,
                [item.to_dict() for item in report.issues],
            )
            self.assertEqual(report.connector_count, 1)
            profile = manager.get("aurora.x1")
            self.assertEqual(profile["display_name"], "Aurora X1 (synthetic)")

            result = manager.identify(
                {"challenge": {"Vendor": "Aurora X1"}}, phase="pre_auth"
            )
            self.assertEqual(result["confidence"], "exact")
            self.assertEqual(result["profile_ids"], ["aurora.x1"])
            self.assertFalse(result["ambiguous"])

    def test_endpoints_use_dependencies_auth_and_dynamic_request_values(self):
        with manager_for_catalog(self.catalog) as manager:
            executor, session = self._executor(manager)
            executor.connect()

            preflight = find_call(session.calls, "challenge")
            login = find_call(session.calls, "login")
            self.assertIsNotNone(preflight)
            self.assertIsNotNone(login)
            self.assertEqual(login["data"]["token"], "C-123")
            self.assertEqual(login["data"]["username"], "tester")
            self.assertEqual(executor.state["session"], "S-9")

            model = executor.collect()
            status = find_call(session.calls, "status")
            self.assertEqual(status["params"]["session"], "S-9")
            self.assertEqual(status["params"]["view"], "full")

            self.assertTrue(executor.logout())
            logout = find_call(session.calls, "logout")
            self.assertEqual(logout["data"]["session"], "S-9")
            self.assertFalse(executor.authenticated)
            self.assertIsNotNone(model.get_metric("wan", "wan.public_ip"))

    def test_extraction_serialization_and_no_fabricated_values(self):
        with manager_for_catalog(self.catalog) as manager:
            executor, _ = self._executor(manager)
            executor.connect()
            payload = json.loads(json.dumps(executor.collect().to_dict()))

            self.assertEqual(payload["profile_id"], "aurora.x1")
            self.assertEqual(payload["gateway"], "192.168.5.1")
            self.assertIn("domains", payload)
            self.assertIn("collections", payload)
            self.assertIn("endpoint_errors", payload)
            self.assertIn("updated_at", payload)

            wan = payload["domains"]["wan"]["wan.public_ip"]
            self.assertEqual(wan["availability"], "available")
            self.assertEqual(wan["value"], "198.51.100.7")
            self.assertEqual(wan["source"], "status")

            rx = payload["domains"]["optical"]["optical.rx_power_dbm"]
            self.assertEqual(rx["availability"], "unsupported")
            self.assertIsNone(rx["value"])
            self.assertIsNone(rx["source_value"])

            count = payload["domains"]["connected_devices"]["devices.count"]
            self.assertEqual(count["value"], 1)
            self.assertEqual(len(payload["devices"]), 1)
            self.assertEqual(payload["devices"][0]["hostname"], "laptop")
            self.assertEqual(payload["collections"]["devices"]["count"], 1)

            self.assertEqual(
                payload["call_action"]["dial_uri"], "tel:5551234"
            )
            self.assertTrue(payload["call_action"]["enabled"])

            for legacy_key in ("device", "internet", "optical", "generic", "meta"):
                self.assertNotIn(legacy_key, payload)

    def test_endpoint_errors_are_reported_without_inventing_values(self):
        with manager_for_catalog(self.catalog) as manager:
            session = RecordingSession(self.payloads)
            session.failed_endpoints.add("status")
            executor, _ = self._executor(manager, session)
            executor.connect()
            payload = executor.collect().to_dict()

            self.assertEqual(len(payload["endpoint_errors"]), 1)
            self.assertEqual(payload["endpoint_errors"][0]["endpoint"], "status")
            self.assertIn("HTTP 500", payload["endpoint_errors"][0]["error"])

            public_ip = payload["domains"]["wan"]["wan.public_ip"]
            self.assertEqual(public_ip["availability"], "error")
            self.assertIsNone(public_ip["value"])

    def test_expired_profile_session_is_reauthenticated_once(self):
        valid_status = self.payloads["status"]
        expired_status = dict(valid_status)
        expired_status["authenticated"] = False
        overrides = {"status": [expired_status, valid_status]}

        with manager_for_catalog(self.catalog) as manager:
            session = RecordingSession(self.payloads, payload_overrides=overrides)
            executor, _ = self._executor(manager, session)
            executor.connect()
            payload = executor.collect().to_dict()

            self.assertEqual(len([c for c in session.calls if c["name"] == "login"]), 2)
            self.assertEqual(len([c for c in session.calls if c["name"] == "status"]), 2)
            self.assertEqual(
                payload["domains"]["wan"]["wan.public_ip"]["availability"],
                "available",
            )

    def test_profile_expressions_are_never_executed(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "profile-code-was-executed"
            connector = copy.deepcopy(synthetic_connector())
            connector["extraction"][1]["expression"] = (
                "$.__import__('os').system('touch %s')" % marker
            )
            session = RecordingSession(self.payloads)
            with manager_for_catalog(
                {"schema_version": 1, "connectors": [connector]}
            ) as manager:
                executor = ConnectorExecutor(
                    connector,
                    "192.168.5.1",
                    {"username": "tester", "password": "unit-test-password"},
                    manager=manager,
                    session=session,
                )
                executor.connect()
                executor.collect()

            self.assertFalse(marker.exists())

        for module in (connector_executor, auth_handlers, connector_manager):
            source = Path(module.__file__).read_text(encoding="utf-8")
            self.assertNotIn("eval(", source)
            self.assertNotIn("exec(", source)
            self.assertNotIn("__import__(", source)

    def test_unsupported_extraction_method_is_rejected_at_runtime(self):
        connector = synthetic_connector()
        connector["extraction"][1]["method"] = "xpath"
        with manager_for_catalog(
            {"schema_version": 1, "connectors": [connector]}
        ) as manager:
            session = RecordingSession(self.payloads)
            executor = ConnectorExecutor(
                connector,
                "192.168.5.1",
                {"username": "tester", "password": "unit-test-password"},
                manager=manager,
                session=session,
            )
            executor.connect()
            model = executor.collect()
            metric = model.get_metric("device_identity", "device.model")
            self.assertEqual(metric.availability, Availability.ERROR)
            self.assertIn("not available", metric.error)


class FiberHomeExecutorTests(unittest.TestCase):
    def _payloads(self):
        return {
            "get_acs_random": {
                "acsRandom": "abcdefghijklmnop",
                "sessionid": "SESS",
            },
            "do_login": {
                "login_result": 0,
                "sessionid": "SESS",
                "session_valid": 1,
            },
            "get_login_user": {"login_user": "2"},
            "do_logout": {},
        }

    def test_aes_handler_delegates_crypto_and_logout_uses_observed_role(self):
        session = RecordingSession(self._payloads())
        from connector_manager import ConnectorManager

        manager = ConnectorManager()
        executor = ConnectorExecutor(
            manager.get("fiberhome.hg6145f1"),
            "192.168.1.1",
            {"username": "user", "password": "secret"},
            manager=manager,
            session=session,
        )
        executor.connect()

        login = find_call(session.calls, "do_login")
        self.assertIsNotNone(login)
        self.assertEqual(login["data"]["sessionid"], "SESS")
        self.assertEqual(
            login["data"]["loginpd"], fhencrypt("secret", "abcdefghijklmnop")
        )
        self.assertNotIn("secret", json.dumps(login["data"]))

        self.assertTrue(executor.logout())
        role_call = find_call(session.calls, "get_login_user")
        logout = find_call(session.calls, "do_logout")
        self.assertIsNotNone(role_call)
        self.assertIsNotNone(logout)
        self.assertLess(
            session.calls.index(role_call), session.calls.index(logout)
        )
        self.assertEqual(logout["data"]["username"], "admin")
        self.assertEqual(logout["data"]["sessionid"], "SESS")

    def test_disabled_profile_cannot_be_executed(self):
        connector = synthetic_connector()
        connector["enabled"] = False
        with self.assertRaises(Exception):
            ConnectorExecutor(connector, "192.168.5.1", {})


if __name__ == "__main__":
    unittest.main()
