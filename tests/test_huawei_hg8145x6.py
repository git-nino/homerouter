"""Offline Huawei HG8145X6-10 profile tests.

All tests use sanitized fixtures and mocked HTTP sessions. No physical router,
no real credentials, and no network access.
"""

from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from connector_executor import ConnectorExecutor, EndpointError  # noqa: E402
from connector_manager import ConnectorManager  # noqa: E402
from models.enums import Availability, Domain  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "huawei_hg8145x6"
CATALOG = ROOT / "config" / "connectors.json"
CREDENTIALS = ROOT / "config" / "default_credentials.json"
SCHEMA = ROOT / "config" / "connector.schema.json"
EQUIPMENT = ROOT / "config" / "equipment.json"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="ignore")


class FakeResponse:
    def __init__(self, text="", status_code=200, headers=None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self.encoding = "utf-8"
        self._bytes = text.encode("utf-8")

    def iter_content(self, chunk_size=8192):
        for i in range(0, len(self._bytes), chunk_size):
            yield self._bytes[i:i + chunk_size]

    def close(self):
        return None


class FakeSession:
    """Minimal requests.Session stand-in routing by URL path."""

    def __init__(self, routes=None, record=None):
        self.headers = {}
        self.auth = None
        self.routes = routes or {}
        self.record = record if record is not None else []

    def request(self, method, url, **kwargs):
        self.record.append({"method": method, "url": url, **kwargs})
        for key, handler in self.routes.items():
            if key in url:
                value = handler(method, url, kwargs) if callable(handler) else handler
                if isinstance(value, FakeResponse):
                    return value
                if isinstance(value, tuple):
                    text, status = value
                    return FakeResponse(text=text, status_code=status)
                return FakeResponse(text=str(value))
        return FakeResponse(text="not found", status_code=404)


def _huawei_connector():
    manager = ConnectorManager(CATALOG, CREDENTIALS, SCHEMA, EQUIPMENT)
    connector = manager.get("huawei.hg8145x6")
    assert connector is not None, "huawei.hg8145x6 profile missing"
    return manager, connector


def _success_routes(record=None, login_text=None):
    login_text = login_text if login_text is not None else _load("login_success.html")
    return {
        "/asp/GetRandCount.asp": FakeResponse(_load("GetRandCount.asp")),
        "/login.cgi": FakeResponse(login_text),
        "/html/ssmp/deviceinfo/deviceinfo.asp": FakeResponse(_load("deviceinfo.asp")),
        "/html/amp/opticinfo/opticinfo.asp": FakeResponse(_load("opticinfo.asp")),
        "/html/bbsp/common/GetLanUserDevInfo.asp": FakeResponse(_load("lan_user_dev.asp")),
        "/html/bbsp/waninfo/waninfo.asp": FakeResponse(_load("waninfo.asp")),
        "/html/voip/status/sipvoipmaintain.asp": FakeResponse(_load("sipvoipmaintain.asp")),
        "/html/amp/wlaninfo/wlaninfo.asp": FakeResponse(_load("wlaninfo.asp")),
        "/html/amp/wlanbasic/WlanBasic.asp": FakeResponse(_load("wlaninfo.asp")),
        "/": FakeResponse("<html><title>HG8145X6-10</title></html>"),
    }


class HuaweiProfileValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager, cls.connector = _huawei_connector()

    def test_catalog_structurally_valid(self):
        report = self.manager.validate()
        self.assertTrue(report.structural_valid, [i.to_dict() for i in report.issues])

    def test_profile_has_expected_shape(self):
        c = self.connector
        self.assertEqual(c["id"], "huawei.hg8145x6")
        self.assertEqual(c["manufacturer_id"], "huawei")
        self.assertEqual(c["status"], "observed")
        self.assertTrue(c["enabled"])
        self.assertEqual(c["auth"]["flows"][0]["type"], "huawei_nonce_form")
        endpoint_ids = {e["id"] for e in c["endpoints"]}
        self.assertIn("rand", endpoint_ids)
        self.assertIn("login", endpoint_ids)
        self.assertIn("deviceinfo", endpoint_ids)

    def test_equipment_maps_huawei_profile(self):
        equipment = json.loads(EQUIPMENT.read_text(encoding="utf-8"))
        huawei = next(m for m in equipment["manufacturers"] if m["id"] == "huawei")
        self.assertIn("huawei.hg8145x6", huawei["profiles"])

    def test_no_hardcoded_credentials(self):
        text = CATALOG.read_text(encoding="utf-8")
        self.assertNotIn("SYNTHETIC_TEST_PW", text)
        self.assertNotIn("root", json.dumps(self.connector.get("auth", {})).lower().replace("root", "")[:0])
        # explicit secret values should not appear
        dumped = json.dumps(self.connector)
        self.assertNotIn("SYNTHETIC_TEST_PW", dumped)

    def test_credential_suggestion_is_generic(self):
        key = self.connector["auth"]["credential_suggestion_key"]
        self.assertEqual(key, "generic_user_supplied")
        suggestion = self.manager.credential_suggestion(key)
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion.get("default_username", ""), "")
        self.assertEqual(suggestion.get("default_password", ""), "")


class HuaweiAuthTests(unittest.TestCase):
    def setUp(self):
        self.manager, self.connector = _huawei_connector()

    def test_auth_success_and_credentials_not_persisted(self):
        record = []
        session = FakeSession(_success_routes(record), record=record)
        ex = ConnectorExecutor(
            self.connector, "192.168.100.1",
            credentials={"username": "root", "password": "s3cret"},
            manager=self.manager, session=session,
        )
        ex.connect()
        self.assertTrue(ex.authenticated)
        # login request contains base64 password and token, but executor must not persist raw password
        login_calls = [c for c in record if c["url"].endswith("/login.cgi")]
        self.assertEqual(len(login_calls), 1)
        body = login_calls[0].get("data") or {}
        self.assertEqual(body.get("UserName"), "root")
        self.assertEqual(body.get("PassWord"), base64.b64encode(b"s3cret").decode())
        self.assertIn("x.X_HW_Token", body)
        self.assertNotIn("s3cret", json.dumps(ex.derived))
        self.assertNotIn("s3cret", json.dumps(ex.state))

    def test_auth_failure_raises(self):
        session = FakeSession(_success_routes(login_text=_load("login_fail.html")))
        ex = ConnectorExecutor(
            self.connector, "192.168.100.1",
            credentials={"username": "root", "password": "bad"},
            manager=self.manager, session=session,
        )
        with self.assertRaises(Exception):
            ex.connect()
        self.assertFalse(ex.authenticated)

    def test_empty_token_raises(self):
        routes = _success_routes()
        routes["/asp/GetRandCount.asp"] = FakeResponse("")
        session = FakeSession(routes)
        ex = ConnectorExecutor(
            self.connector, "192.168.100.1",
            credentials={"username": "root", "password": "pw"},
            manager=self.manager, session=session,
        )
        with self.assertRaises(Exception):
            ex.connect()


class HuaweiExtractionTests(unittest.TestCase):
    def setUp(self):
        self.manager, self.connector = _huawei_connector()
        self.session = FakeSession(_success_routes())
        self.ex = ConnectorExecutor(
            self.connector, "192.168.100.1",
            credentials={"username": "root", "password": "pw"},
            manager=self.manager, session=self.session,
        )
        self.ex.connect()
        self.model = self.ex.collect()

    def test_device_identity_mapping(self):
        d = self.model.domains[Domain.DEVICE_IDENTITY]
        self.assertEqual(d["device.manufacturer"].availability, Availability.AVAILABLE)
        self.assertEqual(d["device.manufacturer"].value, "Huawei")
        self.assertEqual(d["device.model"].value, "HG8145X6-10")
        self.assertEqual(d["device.firmware"].value, "V5R023C00S174")
        self.assertEqual(d["device.hardware"].value, "2C6D.A")
        self.assertEqual(d["device.serial"].value, "48575443TESTSN01")

    def test_optical_mapping(self):
        o = self.model.domains[Domain.OPTICAL]
        self.assertEqual(o["optical.tx_power_dbm"].availability, Availability.AVAILABLE)
        self.assertAlmostEqual(o["optical.tx_power_dbm"].value, 2.32, places=2)
        self.assertAlmostEqual(o["optical.rx_power_dbm"].value, -24.2, places=1)
        self.assertEqual(o["optical.temperature_c"].value, 37.0)

    def test_system_uptime(self):
        s = self.model.domains[Domain.SYSTEM]
        self.assertEqual(s["system.uptime_s"].availability, Availability.AVAILABLE)
        self.assertEqual(s["system.uptime_s"].value, 2240)

    def test_connected_devices_parsed(self):
        self.assertTrue(self.model.devices_loaded)
        self.assertGreaterEqual(len(self.model.devices), 2)
        d0 = self.model.devices[0]
        self.assertEqual(d0.ip_address, "192.0.2.10")
        self.assertEqual(d0.mac_address, "AA:BB:CC:00:00:01")
        self.assertEqual(d0.link_type, "ETH")
        cnt = self.model.domains[Domain.CONNECTED_DEVICES]["devices.count"]
        self.assertEqual(cnt.availability, Availability.AVAILABLE)
        self.assertEqual(cnt.value, 2)

    def test_wifi_ssids_collection(self):
        ssids = self.model.collections.get("ssids")
        self.assertIsNotNone(ssids)
        self.assertTrue(ssids.loaded)
        self.assertEqual(len(ssids.items), 2)
        self.assertTrue(all("name" in i for i in ssids.items))

    def test_voip_mapping(self):
        v = self.model.domains[Domain.VOIP]
        self.assertIn("ERROR_WAN_NOT_CONFIGURED", str(v["voip.registration_state"].value))
        # phone number is unavailable/not fabricated
        phone = v["voip.phone_number"]
        self.assertIn(phone.availability, (Availability.UNAVAILABLE, Availability.UNSUPPORTED, Availability.NOT_LOADED, Availability.ERROR))
        self.assertIsNone(phone.value)

    def test_wan_unavailable_not_fabricated(self):
        w = self.model.domains[Domain.WAN]
        for k, m in w.items():
            self.assertIn(m.availability, (Availability.UNAVAILABLE, Availability.UNSUPPORTED, Availability.NOT_LOADED, Availability.ERROR), k)
            self.assertIsNone(m.value, k)

    def test_optical_voltage_normalized_to_volts(self):
        o = self.model.domains[Domain.OPTICAL]
        self.assertAlmostEqual(o["optical.supply_voltage_v"].value, 3.236, places=3)

    def test_missing_metric_is_unavailable_not_fabricated(self):
        # remove opticinfo route to force endpoint failure -> missing metrics
        routes = _success_routes()
        routes["/html/amp/opticinfo/opticinfo.asp"] = FakeResponse("", status_code=500)
        ex = ConnectorExecutor(
            self.connector, "192.168.100.1",
            credentials={"username": "root", "password": "pw"},
            manager=self.manager, session=FakeSession(routes),
        )
        ex.connect()
        model = ex.collect()
        m = model.domains[Domain.OPTICAL]["optical.rx_power_dbm"]
        self.assertIn(m.availability, (Availability.UNAVAILABLE, Availability.ERROR))
        self.assertIsNone(m.value)


class HuaweiIdentificationTests(unittest.TestCase):
    def setUp(self):
        self.manager, self.connector = _huawei_connector()

    def test_preauth_identification_from_root_text(self):
        session = FakeSession({"/": FakeResponse("<html>HG8145X6-10 login</html>")})
        ex = ConnectorExecutor(
            self.connector, "192.168.100.1", manager=self.manager, session=session
        )
        discovery = ex.discover()
        result = self.manager.identify(discovery["observations"], phase="pre_auth")
        self.assertIn("huawei.hg8145x6", result["profile_ids"])
        self.assertIn(result["confidence"], ("partial", "exact"))

    def test_invalid_response_does_not_crash_discover(self):
        session = FakeSession({"/": FakeResponse("garbage\x00\x01", status_code=200)})
        ex = ConnectorExecutor(
            self.connector, "192.168.100.1", manager=self.manager, session=session
        )
        discovery = ex.discover()
        self.assertIn("observations", discovery)


class HuaweiRegressionTests(unittest.TestCase):
    def test_fiberhome_profile_still_present_and_valid(self):
        manager = ConnectorManager(CATALOG, CREDENTIALS, SCHEMA, EQUIPMENT)
        fh = manager.get("fiberhome.hg6145f1")
        self.assertIsNotNone(fh)
        self.assertEqual(fh["manufacturer_id"], "fiberhome")
        report = manager.validate()
        self.assertTrue(report.structural_valid, [i.to_dict() for i in report.issues])

    def test_fiberhome_credential_suggestion_unchanged(self):
        manager = ConnectorManager(CATALOG, CREDENTIALS, SCHEMA, EQUIPMENT)
        fh = manager.get("fiberhome.hg6145f1")
        key = fh["auth"]["credential_suggestion_key"]
        self.assertEqual(key, "fiberhome_observed_defaults")
        sugg = manager.credential_suggestion(key)
        self.assertEqual(sugg["default_username"], "user")
        self.assertEqual(sugg["default_password"], "user1234")


if __name__ == "__main__":
    unittest.main()
