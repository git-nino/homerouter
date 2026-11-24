"""Regression tests for page-scoped Speedtest result display.

The backend keeps the last Speedtest result for diagnostics and API
compatibility. These tests prove that a fresh dashboard page no longer replays
it, while the engine, worker and /api/speedtest contract are unchanged.
"""

import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from models.universal import UniversalModel  # noqa: E402

with patch("gateway.detect_gateway", return_value={
    "gateway": "192.168.1.1", "model": "", "detected": False,
}):
    import app as app_module  # noqa: E402


DONE = {
    "status": "done",
    "progress": "",
    "result": {
        "download": 94.2, "upload": 41.7, "ping": 12.3, "jitter": 2.1,
        "server": "Test server", "sponsor": "Test ISP", "country": "Test country",
    },
    "error": None,
    "started_at": "2026-11-24T06:00:00+00:00",
    "completed_at": "2026-11-24T06:01:00+00:00",
}


class FakeExecutor:
    def collect(self):
        return UniversalModel()


class SpeedtestResultResetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        cls.app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        cls.worker_source = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")

    def setUp(self):
        self.original_state = dict(app_module.state)
        self.client = app_module.app.test_client()

    def tearDown(self):
        app_module.state.clear()
        app_module.state.update(self.original_state)

    # ---------- browser lifecycle ----------
    def test_browser_lifecycle_behavior(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        harness = ROOT / "tests" / "speedtest_reset_browser.test.js"
        result = subprocess.run(
            [node, str(harness)], capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_display_reset_is_page_scoped_and_does_not_touch_backend(self):
        script = self.script
        self.assertIn("let speedResultFromThisPage = false;", script)
        self.assertIn("function resetSpeedDisplay()", script)
        self.assertIn("if (!speedResultFromThisPage) return;", script)
        self.assertIn("speedResultFromThisPage = true;", script)

        init = script[script.index("async function init()"):]
        self.assertIn("resetSpeedDisplay();", init[:init.index("await loadManufacturers()")])
        enter = script[script.index("function enterDashboard()"):]
        self.assertIn("resetSpeedDisplay();", enter[:enter.index("show('loginView'")])
        self.assertIn("resetSpeedDisplay();", script[script.index("function leaveDashboard("):])

        reset_body = script[
            script.index("function resetSpeedDisplay()"):script.index("function metric(")
        ]
        self.assertNotIn("fetch(", reset_body)
        self.assertNotIn("/api/", reset_body)
        self.assertNotIn("localStorage", reset_body)

        # The reset clears only Speedtest display nodes and its own timers.
        for node_id in (
            "speedState", "spDown", "spUp", "spPing", "spJitter", "spServer",
            "speedError", "speedProgress", "speedResult", "speedIdle",
        ):
            self.assertIn("'" + node_id + "'", reset_body, node_id)

    def test_no_browser_storage_or_stale_status_poll_on_load(self):
        for storage in ("localStorage", "sessionStorage", "indexedDB",
                        "document.cookie"):
            self.assertNotIn(storage, self.script)
        init = self.script[self.script.index("async function init()"):]
        self.assertNotIn("/api/speedtest/status", init)
        self.assertNotIn("pollSpeed()", init)
        # Service worker never caches API responses.
        self.assertIn("startsWith('/api/')", self.worker_source)

    # ---------- backend contract preserved ----------
    def test_speedtest_api_and_worker_contract_is_unchanged(self):
        rules = {rule.rule for rule in app_module.app.url_map.iter_rules()}
        self.assertIn("/api/speedtest", rules)
        self.assertIn("/api/speedtest/status", rules)
        self.assertFalse(any("reset" in rule for rule in rules))

        with patch.object(app_module, "_speed_worker") as worker:
            response = self.client.post("/api/speedtest")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["ok"])
            worker.assert_called_once()
            status = self.client.get("/api/speedtest/status").get_json()
            self.assertEqual(status["status"], "starting")
            self.assertNotIn("token", status)

        with patch.dict(app_module.state, {"speedtest": dict(DONE)}):
            status = self.client.get("/api/speedtest/status").get_json()
            self.assertEqual(status["status"], "done")
            self.assertEqual(status["result"]["download"], 94.2)
            self.assertEqual(status["completed_at"], DONE["completed_at"])

        for marker in ("_speed_worker", "SPEEDTEST_TIMEOUT", "_speed_token",
                       "threading.Thread(target=_speed_worker"):
            self.assertIn(marker, self.app_source)

    def test_completed_result_is_still_available_to_api_data(self):
        with patch.dict(app_module.state, {
            "executor": FakeExecutor(), "connected": True,
            "speedtest": dict(DONE),
        }):
            response = self.client.get("/api/data")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        domain = payload["domains"]["speed_test"]
        self.assertEqual(domain["speed_test.status"]["value"], "done")
        self.assertEqual(domain["speed_test.download_mbps"]["value"], 94.2)
        self.assertEqual(domain["speed_test.upload_mbps"]["value"], 41.7)
        self.assertEqual(domain["speed_test.ping_ms"]["value"], 12.3)
        self.assertEqual(domain["speed_test.completed_at"]["value"],
                         DONE["completed_at"])


if __name__ == "__main__":
    unittest.main()
