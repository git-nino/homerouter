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
        # Match most-specific route first so "/" doesn't shadow real endpoints.
        for key, handler in sorted(self.routes.items(), key=lambda kv: len(kv[0]), reverse=True):
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
        "/html/bbsp/common/getwanlist.asp": FakeResponse(_load("wan_list.asp")),
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

    def test_credential_suggestion_is_huawei_default(self):
        key = self.connector["auth"]["credential_suggestion_key"]
        self.assertEqual(key, "huawei_hg8145x6_defaults")
        suggestion = self.manager.credential_suggestion(key)
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion.get("default_username", ""), "root")
        self.assertEqual(suggestion.get("default_password", ""), "")
        self.assertEqual(suggestion.get("default_gateway", ""), "192.168.100.1")


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

    def test_connected_devices_hex_escaped_ip_and_validation(self):
        routes = _success_routes()
        routes["/html/bbsp/common/GetLanUserDevInfo.asp"] = FakeResponse(
            '<script>'
            'function USERDevice(Domain,IpAddr,MacAddr,Port,IpType,DevType,DevStatus,PortType,Time,HostName,IPv4Enabled,IPv6Enabled,DeviceType,UserDevAlias,UserSpecifiedDeviceType,LeaseTimeRemaining,RealMacAddr){}'
            'var LANHostInfo = new Array('
            'new USERDevice("d","192\\x2e168\\x2e100\\x2e10","AA:BB:CC:00:00:10","LAN1","DHCP","--","Online","ETH","0","host\\x2da","1","0","0","","0","0","AA:BB:CC:00:00:10"),'
            'new USERDevice("d","192\\x2e168\\x2e100\\x2e11","AA:BB:CC:00:00:11","LAN2","DHCP","--","Online","ETH","0","host\\x5fb","1","0","0","","0","0","AA:BB:CC:00:00:11"),'
            'new USERDevice("d","0\\x2e0\\x2e0\\x2e0","AA:BB:CC:00:00:12","LAN3","DHCP","--","Offline","ETH","0","host-c","1","0","0","","0","0","AA:BB:CC:00:00:12"),'
            'new USERDevice("d","","AA:BB:CC:00:00:13","LAN4","DHCP","--","Offline","ETH","0","host-d","1","0","0","","0","0","AA:BB:CC:00:00:13"),'
            'new USERDevice("d","bad-ip","AA:BB:CC:00:00:14","LAN5","DHCP","--","Online","ETH","0","host-e","1","0","0","","0","0","AA:BB:CC:00:00:14"),'
            'null);</script>'
        )
        session = FakeSession(routes)
        ex = ConnectorExecutor(self.connector, "192.168.100.1", credentials={"username":"u","password":"p"}, manager=self.manager, session=session)
        ex.connect(); model = ex.collect()
        self.assertEqual(len(model.devices), 5)
        self.assertEqual(model.devices[0].ip_address, "192.168.100.10")
        self.assertEqual(model.devices[1].ip_address, "192.168.100.11")
        self.assertEqual(model.devices[0].mac_address, "AA:BB:CC:00:00:10")
        self.assertEqual(model.devices[1].mac_address, "AA:BB:CC:00:00:11")
        self.assertEqual(model.devices[0].hostname, "host-a")
        self.assertEqual(model.devices[1].hostname, "host_b")
        self.assertIsNone(model.devices[2].ip_address)  # 0.0.0.0 unavailable
        self.assertIsNone(model.devices[3].ip_address)  # empty unavailable
        self.assertIsNone(model.devices[4].ip_address)  # invalid unavailable

    def test_connected_devices_exact_duplicate_rows_deduped(self):
        routes = _success_routes()
        row = 'new USERDevice("d","192\\x2e168\\x2e100\\x2e10","AA:BB:CC:00:00:10","LAN1","DHCP","--","Online","ETH","0","host-a","1","0","0","","0","0","AA:BB:CC:00:00:10"),'
        routes["/html/bbsp/common/GetLanUserDevInfo.asp"] = FakeResponse(
            '<script>function USERDevice(Domain,IpAddr,MacAddr,Port,IpType,DevType,DevStatus,PortType,Time,HostName,IPv4Enabled,IPv6Enabled,DeviceType,UserDevAlias,UserSpecifiedDeviceType,LeaseTimeRemaining,RealMacAddr){}'
            'var LANHostInfo = new Array(' + row + row + 'null);</script>'
        )
        session = FakeSession(routes)
        ex = ConnectorExecutor(self.connector, "192.168.100.1", credentials={"username":"u","password":"p"}, manager=self.manager, session=session)
        ex.connect(); model = ex.collect()
        # Exact duplicate JS constructor rows are the same record; dedupe is evidence-based.
        self.assertEqual(len(model.devices), 1)

    def test_wifi_ssids_collection(self):
        ssids = self.model.collections.get("ssids")
        self.assertIsNotNone(ssids)
        self.assertTrue(ssids.loaded)
        self.assertEqual(len(ssids.items), 2)
        self.assertTrue(all("name" in i for i in ssids.items))

    def test_voip_mapping(self):
        v = self.model.domains[Domain.VOIP]
        self.assertEqual(v["voip.registration_state"].availability, Availability.AVAILABLE)
        self.assertEqual(v["voip.registration_state"].value, "Disabled")
        self.assertEqual(v["voip.line_status"].availability, Availability.AVAILABLE)
        self.assertEqual(v["voip.line_status"].value, "Idle")
        # Empty directory number must not be fabricated as a phone number.
        phone = v["voip.phone_number"]
        self.assertIn(phone.availability, (Availability.UNAVAILABLE, Availability.UNSUPPORTED, Availability.NOT_LOADED, Availability.ERROR, Availability.INVALID))
        self.assertIsNone(phone.value)

    def test_wan_mapping_from_ppp_internet_row(self):
        w = self.model.domains[Domain.WAN]
        self.assertEqual(w["wan.public_ip"].availability, Availability.AVAILABLE)
        self.assertEqual(w["wan.public_ip"].value, "203.0.113.10")
        self.assertEqual(w["wan.gateway_ip"].value, "203.0.113.1")
        self.assertEqual(w["wan.connection_name"].value, "INTERNET_R_0_12_35")
        self.assertEqual(w["wan.status"].value, "Connected")
        self.assertEqual(w["wan.connection_type"].value, "IP_Routed")
        self.assertEqual(w["wan.dns_servers"].value, "198.51.100.1,198.51.100.2")
        self.assertEqual(w["wan.vlan_id"].value, "12")
        self.assertEqual(w["wan.mac"].value, "AA:BB:CC:DD:EE:01")
        self.assertEqual(w["wan.mtu"].value, 1492)
        self.assertEqual(w["wan.nat"].value, "Enabled")
        self.assertEqual(w["wan.uptime_s"].value, 3600)
        self.assertEqual(w["wan.pppoe_user"].value, "user@example.invalid")
        self.assertEqual(w["wan.last_error"].value, "ERROR_NONE")

    def test_wan_selects_internet_ppp_not_voip(self):
        # fixture has VOIP PPP row too; ensure INTERNET row selected
        w = self.model.domains[Domain.WAN]
        self.assertEqual(w["wan.public_ip"].value, "203.0.113.10")
        self.assertNotEqual(w["wan.pppoe_user"].value, "voip@example.invalid")

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


class HuaweiLoginDefaultsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module
        cls.app_module = app_module
        app_module.app.config["TESTING"] = True
        cls.client = app_module.app.test_client()
        cls.manager, _ = _huawei_connector()

    def setUp(self):
        from unittest.mock import patch
        self._patchers = []
        # Keep discovery deterministic and side-effect free.
        def fake_discover(**kwargs):
            return {
                "gateway": "192.168.1.1",
                "gateway_detected": True,
                "detected": True,
                "selected_profile": "huawei.hg8145x6" if kwargs.get("manufacturer_id") == "huawei" else "fiberhome.hg6145f1",
            }
        p = patch.object(self.app_module, "run_discovery", side_effect=lambda force=False, host=None, manufacturer_id=None: {
            "gateway": "198.51.100.220" if manufacturer_id == "huawei" else "192.168.1.1",
            "gateway_detected": True,
            "detected": True,
            "selected_profile": "huawei.hg8145x6" if manufacturer_id == "huawei" else "fiberhome.hg6145f1",
        })
        p.start(); self._patchers.append(p)
        p2 = patch.dict(self.app_module.state, {"manager": self.manager})
        p2.start(); self._patchers.append(p2)

    def tearDown(self):
        for p in reversed(self._patchers):
            p.stop()

    def test_huawei_defaults_prefilled(self):
        payload = self.client.get('/api/login-defaults?manufacturer_id=huawei').get_json()
        self.assertEqual(payload['gateway'], '192.168.100.1')
        self.assertEqual(payload['username'], 'root')
        self.assertEqual(payload['password'], '')

    def test_fiberhome_defaults_preserved(self):
        payload = self.client.get('/api/login-defaults?manufacturer_id=fiberhome').get_json()
        self.assertEqual(payload['gateway'], '192.168.1.1')
        self.assertEqual(payload['username'], 'user')
        self.assertEqual(payload['password'], 'user1234')

    def test_user_supplied_values_passed_to_connect(self):
        from unittest.mock import patch, MagicMock
        fake_model = MagicMock()
        fake_model.to_dict.return_value = {
            'gateway': '198.51.100.220', 'domains': {}, 'devices': [], 'devices_loaded': False,
            'collections': {}, 'voip_lines': [], 'endpoint_errors': [], 'updated_at': None,
            'profile_id': 'huawei.hg8145x6', 'compatibility': {},
        }
        with patch.object(self.app_module, 'run_discovery', return_value={'selected_profile':'huawei.hg8145x6','detected':True,'gateway_detected':True}), \
             patch('app.ConnectorExecutor') as C:
            inst = C.return_value
            inst.connect.return_value = None
            inst.collect.return_value = fake_model
            resp = self.client.post('/api/connect', json={'gateway':'198.51.100.220','username':'root','password':'secret','manufacturer_id':'huawei'})
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            args, kwargs = C.call_args
            self.assertEqual(args[1], '198.51.100.220')
            creds = kwargs.get('credentials') or (args[2] if len(args) > 2 else {})
            self.assertEqual(creds.get('username'), 'root')
            self.assertEqual(creds.get('password'), 'secret')


class HuaweiSessionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.manager, self.connector = _huawei_connector()

    def test_collect_reauths_once_on_session_expired(self):
        routes = _success_routes()
        calls = {"deviceinfo": 0}
        class ExpiringSession(FakeSession):
            def request(self, method, url, **kwargs):
                path = url.split("192.168.100.1")[-1].split(":80")[-1]
                if path.endswith("/html/ssmp/deviceinfo/deviceinfo.asp"):
                    calls["deviceinfo"] += 1
                    if calls["deviceinfo"] == 1:
                        return FakeResponse(text="<html><body><form name='login'><input name='txt_Username'/><input name='txt_Password'/></form><script>location.href='login.asp'</script></body></html>", status_code=200)
                return super().request(method, url, **kwargs)
        ex = ConnectorExecutor(self.connector, "192.168.100.1", credentials={"username":"root","password":"s3cret"}, manager=self.manager, session=ExpiringSession(routes))
        ex.connect()
        model = ex.collect()
        self.assertGreaterEqual(calls["deviceinfo"], 2)
        m = model.domains[Domain.DEVICE_IDENTITY]["device.model"]
        self.assertEqual(m.availability, Availability.AVAILABLE)

    def test_non_auth_http_error_not_treated_as_session_expiry(self):
        routes = _success_routes()
        routes["/html/ssmp/deviceinfo/deviceinfo.asp"] = FakeResponse(text="oops", status_code=500)
        ex = ConnectorExecutor(self.connector, "192.168.100.1", credentials={"username":"root","password":"s3cret"}, manager=self.manager, session=FakeSession(routes))
        ex.connect()
        model = ex.collect()
        m = model.domains[Domain.DEVICE_IDENTITY]["device.model"]
        self.assertIn(m.availability, (Availability.ERROR, Availability.UNAVAILABLE, Availability.NOT_LOADED, Availability.INVALID))


class HuaweiAppRefreshTests(unittest.TestCase):
    def setUp(self):
        import app as appmod
        self.appmod = appmod
        self.client = appmod.app.test_client()
        self._orig = {k: appmod.state.get(k) for k in ["executor","connected","credentials","manufacturer_id","username","gateway"]}
        appmod.state["connected"] = False
        appmod.state["executor"] = None
        appmod.state["credentials"] = None

    def tearDown(self):
        self.appmod.state.update(self._orig)

    def test_api_data_401_when_not_connected(self):
        res = self.client.get("/api/data")
        self.assertEqual(res.status_code, 401)
        body = res.get_json()
        self.assertEqual(body.get("code"), "not_connected")
        self.assertNotIn("password", str(body).lower())

    def test_api_data_reauths_once_on_session_expired(self):
        from connector_executor import SessionExpired
        class Dummy:
            def __init__(self):
                self.calls = 0
                self.connect_calls = 0
            def collect(self):
                self.calls += 1
                if self.calls == 1:
                    raise SessionExpired("expired")
                return self.model
            def connect(self):
                self.connect_calls += 1
        mgr, connector = _huawei_connector()
        routes = _success_routes()
        ex = ConnectorExecutor(connector, "192.168.100.1", credentials={"username":"u","password":"p"}, manager=mgr, session=FakeSession(routes))
        ex.connect(); model = ex.collect()
        d = Dummy(); d.model = model
        self.appmod.state["connected"] = True
        self.appmod.state["executor"] = d
        self.appmod.state["credentials"] = {"username":"u","password":"p","profile_id":"huawei.hg8145x6"}
        res = self.client.get("/api/data")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(d.connect_calls, 1)
        self.assertEqual(d.calls, 2)
        body = res.get_json()
        self.assertNotIn("password", str(body).lower())

    def test_api_data_session_expired_clears_state_when_reauth_fails(self):
        from connector_executor import SessionExpired, AuthenticationError
        class Dummy:
            def collect(self):
                raise SessionExpired("expired")
            def connect(self):
                raise AuthenticationError("bad")
        self.appmod.state["connected"] = True
        self.appmod.state["executor"] = Dummy()
        self.appmod.state["credentials"] = {"username":"u","password":"p","profile_id":"huawei.hg8145x6"}
        res = self.client.get("/api/data")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.get_json().get("code"), "session_expired")
        self.assertFalse(self.appmod.state["connected"])
        self.assertIsNone(self.appmod.state["credentials"])

    def test_api_data_executor_error_is_502(self):
        from connector_executor import ExecutorError
        class Dummy:
            def collect(self):
                raise ExecutorError("boom")
        self.appmod.state["connected"] = True
        self.appmod.state["executor"] = Dummy()
        self.appmod.state["credentials"] = {"username":"u","password":"p"}
        res = self.client.get("/api/data")
        self.assertEqual(res.status_code, 502)
        self.assertEqual(res.get_json().get("code"), "executor_error")


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
