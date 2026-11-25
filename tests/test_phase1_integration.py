"""Phase 1 integration checks against the canonical universal architecture.

The older Phase 1 workspace used the pre-modular RouterClient design. These
tests keep its user-visible contract while exercising the current
ConnectorManager -> ConnectorExecutor -> UniversalModel pipeline.
"""

import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.fixtures import (  # noqa: E402
    RecordingSession,
    manager_for_catalog,
    synthetic_connector,
    synthetic_payloads,
)

# app.py probes the gateway at import time; keep tests offline.
with patch("gateway.detect_gateway", return_value={
    "gateway": "192.168.1.1", "model": "", "detected": False,
}):
    import app as app_module  # noqa: E402


DETECTION = {"gateway": "192.168.1.1", "model": "", "detected": False}
DISCOVERY = {
    "gateway": "192.168.1.1",
    "gateway_detected": True,
    "detected": True,
    "detected_device": "Aurora X1",
    "selected_profile": "aurora.x1",
}


class RecordingExecutor:
    """Stands in for ConnectorExecutor without touching the network."""

    instances = []

    def __init__(self, profile, host, credentials, manager=None, session=None):
        self.profile = profile
        self.host = host
        self.credentials = credentials
        self.logged_out = False
        RecordingExecutor.instances.append(self)

    def connect(self):
        return {}

    def logout(self):
        self.logged_out = True
        return True

    def collect(self):
        raise AssertionError("collect() must be patched for this test")


def png_size(path):
    with path.open("rb") as fh:
        header = fh.read(24)
    assert header[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    return struct.unpack(">II", header[16:24])


class Phase1DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        cls.script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        cls.styles = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
        cls.manifest = json.loads(
            (ROOT / "static" / "manifest.webmanifest").read_text(encoding="utf-8")
        )
        cls.worker = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")

    def setUp(self):
        self.original_state = dict(app_module.state)
        RecordingExecutor.instances = []
        self.client = app_module.app.test_client()

    def tearDown(self):
        app_module.state.clear()
        app_module.state.update(self.original_state)

    def _connect(self, manufacturer_id=None, extra=None):
        payload = {
            "gateway": "192.168.1.1",
            "username": "tester",
            "password": "unit-test-password",
        }
        if manufacturer_id is not None:
            payload["manufacturer_id"] = manufacturer_id
        payload.update(extra or {})
        catalog = {"schema_version": 1, "connectors": [synthetic_connector()]}
        with manager_for_catalog(catalog) as manager:
            # Direct assignment keeps connection state changes made by the
            # route (patch.dict would restore the whole state on exit).
            app_module.state["manager"] = manager
            app_module.state["discovery"] = None
            with patch.object(app_module, "run_discovery", return_value=DISCOVERY), \
                 patch.object(app_module, "ConnectorExecutor", RecordingExecutor):
                response = self.client.post("/api/connect", json=payload)
        return response

    # ---------- dashboard ----------
    def test_dashboard_loads(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for marker in (
            'id="loginView"', 'id="loginForm"', 'id="dashView"',
            'id="speedPanel"', 'id="voipPanel"', 'id="deviceTable"',
        ):
            self.assertIn(marker, html, marker)
        self.assertIn("--bg: #0b0f16", self.styles)

    def test_speedtest_button_appears_once_inside_its_card(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertEqual(html.count('id="speedBtn"'), 1)
        self.assertNotIn('id="speedBtn"', html[:html.index("</header>")])
        panel = html.index('id="speedPanel"')
        button = html.index('id="speedBtn"')
        self.assertLess(panel, button)
        self.assertLess(button, html.index("<!-- headline stat cards -->"))
        self.assertIn('stroke-width="2"', html)
        self.assertIn('d="m12 14 4-4"', html)
        self.assertIn('d="M3.34 19a10 10 0 1 1 17.32 0"', html)
        self.assertNotIn("⚡ Speedtest</button>", html)

    def test_manufacturer_selector_precedes_gateway(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="manufacturerInput"', html)
        self.assertLess(
            html.index('id="manufacturerInput"'),
            html.index('id="gatewayInput"'),
        )
        select_start = html.index('<select id="manufacturerInput"')
        select_html = html[select_start:html.index("</select>", select_start)]
        for name in ("FiberHome", "Huawei", "ZTE"):
            self.assertNotIn(name, select_html)
        self.assertIn("/api/manufacturers", self.script)

    def test_lucide_icons_and_dark_theme_are_preserved(self):
        html = self.client.get("/").get_data(as_text=True)
        for path in (
            'd="m16 17 5-5-5-5"', 'd="M21 12H9"',
            'd="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"',
            'd="M12 20h.01"',
            '<rect width="20" height="8" x="2" y="14" rx="2" />',
        ):
            self.assertIn(path, html, path)
        self.assertNotIn("⏻", html)
        self.assertIn(".icon-btn", self.styles)
        self.assertIn("--panel: #151b26", self.styles)

    # ---------- manufacturer catalog ----------
    def test_catalog_endpoint_returns_configured_manufacturers(self):
        catalog = json.loads(
            (ROOT / "config" / "equipment.json").read_text(encoding="utf-8")
        )
        response = self.client.get("/api/manufacturers")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(
            payload["manufacturers"],
            [{"id": item["id"], "name": item["name"]}
             for item in catalog["manufacturers"] if item.get("profiles")],
        )
        self.assertEqual(payload["message"], "")

    def test_broken_and_empty_catalog_degrade_gracefully(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "equipment.json"
            broken.write_text("{ not json", encoding="utf-8")
            with patch.object(app_module, "EQUIPMENT_CATALOG", broken):
                payload = self.client.get("/api/manufacturers").get_json()
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["manufacturers"], [])
            self.assertTrue(payload["message"])

            empty = Path(tmp) / "equipment.json"
            empty.write_text(
                json.dumps({"schema_version": 1, "manufacturers": []}),
                encoding="utf-8",
            )
            with patch.object(app_module, "EQUIPMENT_CATALOG", empty):
                payload = self.client.get("/api/manufacturers").get_json()
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["manufacturers"], [])

    def test_placeholder_manufacturer_without_usable_profile_not_advertised(self):
        catalog = json.loads((ROOT / "config" / "equipment.json").read_text(encoding="utf-8"))
        zte = [m for m in catalog.get("manufacturers", []) if m.get("id") == "zte"]
        self.assertTrue(zte, "ZTE placeholder expected in equipment catalog")
        self.assertEqual(zte[0].get("profiles"), [])
        payload = self.client.get("/api/manufacturers").get_json()
        ids = [m["id"] for m in payload.get("manufacturers", [])]
        self.assertNotIn("zte", ids)
        self.assertIn("fiberhome", ids)
        self.assertIn("huawei", ids)

    # ---------- connection / session ----------
    def test_manufacturer_id_reaches_backend_and_session(self):
        response = self._connect(manufacturer_id="huawei")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["manufacturer_id"], "huawei")
        self.assertEqual(payload["gateway"], "192.168.1.1")
        self.assertEqual(app_module.state["manufacturer_id"], "huawei")
        session = self.client.get("/api/session").get_json()
        self.assertTrue(session["connected"])
        self.assertEqual(session["manufacturer_id"], "huawei")

    def test_legacy_connection_without_manufacturer_still_works(self):
        response = self._connect(extra={"manufacturer": None})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(response.get_json()["manufacturer_id"], "")
        self.assertTrue(self.client.get("/api/session").get_json()["connected"])

    def test_manufacturer_alias_without_usable_profile_is_rejected(self):
        response = self._connect(extra={"manufacturer": "zte"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])

    def test_unknown_manufacturer_is_rejected(self):
        response = self._connect(manufacturer_id="not-in-catalog")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])

    def test_disconnect_clears_session_and_manufacturer(self):
        self.assertEqual(self._connect(manufacturer_id="fiberhome").status_code, 200)
        response = self.client.post("/api/disconnect")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["router_logout"])
        session = self.client.get("/api/session").get_json()
        self.assertFalse(session["connected"])
        self.assertIsNone(session["manufacturer_id"])
        self.assertIsNone(app_module.state["executor"])
        self.assertIsNone(app_module.state["credentials"])

    # ---------- preserved critical functionality ----------
    def test_router_discovery_is_preserved(self):
        rules = {rule.rule for rule in app_module.app.url_map.iter_rules()}
        for route in (
            "/api/discovery", "/api/login-defaults", "/api/gateway",
            "/api/connect", "/api/data", "/api/disconnect", "/api/session",
            "/api/speedtest", "/api/speedtest/status", "/api/manufacturers",
        ):
            self.assertIn(route, rules, route)
        discovery = {
            "gateway": "10.0.0.1", "detected": True, "gateway_detected": True,
        }
        probe = {"observations": {"challenge": {"Vendor": "Aurora X1"}}, "errors": []}
        catalog = {"schema_version": 1, "connectors": [synthetic_connector()]}
        with manager_for_catalog(catalog) as manager, \
             patch.dict(app_module.state, {"manager": manager, "discovery": None}), \
             patch.object(app_module, "detect_gateway", return_value=discovery), \
             patch.object(app_module.ConnectorExecutor, "discover", return_value=probe), \
             patch.object(
                 app_module.ConnectorExecutor, "connect",
                 side_effect=AssertionError("Discovery must not authenticate."),
             ):
            result = app_module.run_discovery(force=True)
        self.assertEqual(result["selected_profile"], "aurora.x1")
        self.assertTrue(result["physical_device_check_required"])

    def test_authentication_handlers_are_preserved(self):
        import auth_handlers
        self.assertTrue(issubclass(auth_handlers.BasicAuthHandler,
                                   auth_handlers.BaseAuthHandler))
        self.assertTrue(issubclass(auth_handlers.FiberHomeAesAuthHandler,
                                   auth_handlers.BaseAuthHandler))
        for name in ("session", "http_basic", "http_digest", "token",
                     "fiberhome_aes"):
            self.assertIn(name, auth_handlers.HANDLERS)

    def test_universal_model_and_normalized_data_are_preserved(self):
        catalog = {"schema_version": 1, "connectors": [synthetic_connector()]}
        payloads = synthetic_payloads()
        with manager_for_catalog(catalog) as manager:
            executor = app_module.ConnectorExecutor(
                manager.get("aurora.x1"), "192.168.5.1",
                {"username": "tester", "password": "unit-test-password"},
                manager=manager, session=RecordingSession(payloads),
            )
            executor.connect()
            speed = {"status": "idle", "progress": "", "result": None, "error": None}
            with patch.dict(app_module.state, {
                "executor": executor, "connected": True, "speedtest": speed,
            }):
                response = self.client.get("/api/data")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(
            data["domains"]["device_identity"]["device.manufacturer"]["value"],
            "Aurora",
        )
        self.assertEqual(data["domains"]["wan"]["wan.public_ip"]["value"],
                         "198.51.100.7")
        self.assertTrue(data["call_action"]["enabled"])
        self.assertNotIn("profile_id", data)
        self.assertNotIn("compatibility", data)

    def test_voip_tel_dialing_is_preserved(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="voipCallBtn"', html)
        self.assertIn('class="call-fab"', html)
        self.assertIn("dataset.dialUri", self.script)
        self.assertIn("window.location.href = uri", self.script)
        self.assertNotIn("location.href = 'tel:", self.script)
        self.assertIn(".call-fab", self.styles)

    def test_speedtest_routes_and_polling_are_preserved(self):
        with patch.object(app_module, "_speed_worker"):
            response = self.client.post("/api/speedtest")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["ok"])
            status = self.client.get("/api/speedtest/status").get_json()
            self.assertEqual(status["status"], "starting")
            self.assertNotIn("token", status)
        self.assertIn("'/api/speedtest/status'", self.script)
        self.assertIn("setInterval(pollSpeed, 1000)", self.script)
        self.assertIn("$('speedBtn').addEventListener('click'", self.script)

    # ---------- PWA ----------
    def test_manifest_and_icon_assets_are_served(self):
        self.assertEqual(self.manifest["name"], "FTTH viewer")
        self.assertEqual(self.manifest["short_name"], "FTTH viewer")
        self.assertEqual(self.manifest["theme_color"], "#052b21")
        icons = {item["src"]: item for item in self.manifest["icons"]}
        self.assertEqual(icons["/static/icon-192.png"]["purpose"], "any")
        self.assertEqual(icons["/static/icon-512.png"]["purpose"], "any")
        self.assertEqual(icons["/static/icon-maskable-192.png"]["purpose"], "maskable")
        self.assertEqual(icons["/static/icon-maskable-512.png"]["purpose"], "maskable")
        for name, size in (
            ("icon-192.png", (192, 192)),
            ("icon-512.png", (512, 512)),
            ("icon-maskable-192.png", (192, 192)),
            ("icon-maskable-512.png", (512, 512)),
            ("apple-touch-icon.png", (180, 180)),
            ("favicon.png", (64, 64)),
        ):
            self.assertEqual(png_size(ROOT / "static" / name), size, name)
        master = (ROOT / "static/icon-master.svg").read_text(encoding="utf-8")
        for color in ("#0d5b43", "#b9ff78", "#ffffff"):
            self.assertIn(color, master)
        self.assertNotIn("<text", master)

        for path in (
            "/static/manifest.webmanifest", "/static/sw.js",
            "/static/icon-192.png", "/static/icon-512.png",
            "/static/icon-maskable-192.png", "/static/icon-maskable-512.png",
            "/static/apple-touch-icon.png", "/static/icon-master.svg",
            "/static/favicon.png", "/static/dashboard-icons-router.svg",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            response.close()

    def test_service_worker_is_v9_with_preserved_shell(self):
        self.assertIn("const CACHE = 'gwmon-v9';", self.worker)
        self.assertNotIn("const CACHE = 'gwmon-v8';", self.worker)
        for resource in (
            "/", "/static/style.css", "/static/app.js",
            "/static/manifest.webmanifest", "/static/favicon.png",
            "/static/apple-touch-icon.png", "/static/icon-192.png",
            "/static/icon-512.png", "/static/icon-maskable-192.png",
            "/static/icon-maskable-512.png",
        ):
            self.assertIn("'" + resource + "'", self.worker, resource)
        self.assertIn("clients.claim()", self.worker)
        self.assertIn("caches.delete(key)", self.worker)
        self.assertIn("startsWith('/api/')", self.worker)

    # ---------- syntax ----------
    def test_python_sources_compile(self):
        for path in sorted(ROOT.rglob("*.py")):
            if ".git" in path.parts or "__pycache__" in path.parts:
                continue
            with self.subTest(path=path.relative_to(ROOT)):
                compile(path.read_text(encoding="utf-8"), str(path), "exec")

    def test_javascript_sources_are_syntactically_valid(self):
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        for name in ("app.js", "sw.js"):
            with self.subTest(name=name):
                result = subprocess.run(
                    [node, "--check", str(ROOT / "static" / name)],
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
