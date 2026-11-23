import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DashboardOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        cls.script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        cls.styles = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

    def position(self, needle, text=None):
        text = self.template if text is None else text
        pos = text.find(needle)
        self.assertNotEqual(pos, -1, f"missing {needle}")
        return pos

    def test_summary_tiles_are_in_requested_order(self):
        order = [
            'id="sPublicIp"',
            'id="sUptime"',
            'id="sTx"',
            'id="sRx"',
            'id="sDevices"',
            'id="sModel"',
        ]
        positions = [self.position(item) for item in order]
        self.assertEqual(positions, sorted(positions))

    def test_cpu_and_memory_tiles_are_removed(self):
        for needle in ('id="sCpu"', 'id="sMem"', 'id="barCpu"', 'id="barMem"'):
            self.assertNotIn(needle, self.template)
        self.assertNotIn("setBar(", self.script)
        self.assertNotIn(".bar i", self.styles)

    def test_restored_dashboard_order_is_preserved(self):
        order = [
            'id="speedPanel"',
            'id="statGrid"',
            'id="voipPanel"',
            'id="wanTable"',
            'id="ponTable"',
            'id="systemTable"',
            'id="ssidList"',
            'id="deviceTable"',
        ]
        positions = [self.position(item) for item in order]
        self.assertEqual(positions, sorted(positions))

    def test_previous_visual_signature_is_restored(self):
        for needle in (
            'GATEWAY<span class="brand-accent">MONITOR</span>',
            '<h1>Connect to gateway</h1>',
            'Credentials are prefilled',
            '⚡ Speedtest by Ookla',
            '🌐 Internet / WAN',
            '🔦 Optical fiber / PON',
            '🖥️ System',
            '📱 Connected devices',
            'Auto-refresh every 5 seconds',
        ):
            self.assertIn(needle, self.template, needle)

        for needle in (
            '--bg: #0b0f16',
            '--panel: #151b26',
            '--accent: #00d1b2',
            '--accent-2: #ffb454',
            '.stat-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));',
            '.panel-grid { display: grid; grid-template-columns: 1fr;',
            '.call-fab {',
        ):
            self.assertIn(needle, self.styles, needle)

    def test_legacy_generic_panel_and_auth_mode_are_removed(self):
        self.assertNotIn('id="genericPanel"', self.template)
        self.assertNotIn("genericPanel", self.script)
        self.assertNotIn('id="modeInput"', self.template)
        self.assertNotIn("modeInput", self.script)
        self.assertNotIn("profileInput", self.template)

    def test_grid_columns_match_six_tiles(self):
        self.assertIn("repeat(3, minmax(0, 1fr))", self.styles)
        self.assertIn("repeat(6, minmax(0, 1fr))", self.styles)
        self.assertNotIn("repeat(8, minmax(0, 1fr))", self.styles)


if __name__ == "__main__":
    unittest.main()
