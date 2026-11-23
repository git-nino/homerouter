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

    def test_five_sections_are_in_requested_order(self):
        order = [
            'id="speedPanel"',
            'id="wanSection"',
            'id="voipSection"',
            'id="systemSection"',
            'id="devicesSection"',
        ]
        positions = [self.position(item) for item in order]
        self.assertEqual(positions, sorted(positions))

    def test_legacy_generic_panel_and_auth_mode_are_removed(self):
        self.assertNotIn('id="genericPanel"', self.template)
        self.assertNotIn("genericPanel", self.script)
        self.assertNotIn('id="modeInput"', self.template)
        self.assertNotIn("modeInput", self.script)

    def test_grid_columns_match_six_tiles(self):
        self.assertIn("repeat(3, minmax(0, 1fr))", self.styles)
        self.assertIn("repeat(6, minmax(0, 1fr))", self.styles)
        self.assertNotIn("repeat(8, minmax(0, 1fr))", self.styles)


if __name__ == "__main__":
    unittest.main()
