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

    def test_voip_card_comes_after_summary_and_before_wan(self):
        summary_end = self.position('id="sModel"')
        voip = self.position('id="voipPanel"')
        wan = self.position('id="wanTable"')
        self.assertLess(summary_end, voip)
        self.assertLess(voip, wan)

    def test_other_panels_keep_their_order(self):
        order = [
            'id="wanTable"',
            'id="systemTable"',
            'id="deviceTable"',
            'id="genericPanel"',
        ]
        positions = [self.position(item) for item in order]
        self.assertEqual(positions, sorted(positions))

    def test_grid_columns_match_six_tiles(self):
        self.assertIn("repeat(3, minmax(0, 1fr))", self.styles)
        self.assertIn("repeat(6, minmax(0, 1fr))", self.styles)
        self.assertNotIn("repeat(8, minmax(0, 1fr))", self.styles)


if __name__ == "__main__":
    unittest.main()
