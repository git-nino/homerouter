import struct
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def png_size(path):
    data = path.read_bytes()[:24]
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    return struct.unpack(">II", data[16:24])


class UiPolishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        cls.script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        cls.styles = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
        cls.manifest = (ROOT / "static" / "manifest.webmanifest").read_text(encoding="utf-8")
        cls.worker = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
        start = cls.template.find('<div class="card panel" id="voipPanel">')
        end = cls.template.find("<!-- WAN + Optical -->")
        cls.voip_block = cls.template[start:end]

    def test_speed_button_uses_ookla_label(self):
        self.assertIn("Speedtest by Ookla", self.template)
        self.assertNotIn("⚡ Speedtest</button>", self.template)

    def test_logout_is_small_icon_button(self):
        self.assertIn('id="disconnectBtn" class="btn icon-btn"', self.template)
        self.assertIn("<svg", self.template[self.template.find("disconnectBtn"):])
        self.assertNotIn("⏻", self.template)
        self.assertIn(".icon-btn", self.styles)

    def test_voip_card_is_two_rows_without_description(self):
        for removed in (
            "voipNote",
            "voipRegistration",
            "footer-note",
            "Calls use your phone's dialer",
            "btn-call",
            "voip-grid",
        ):
            self.assertNotIn(removed, self.voip_block)
        self.assertIn('id="voipState"', self.voip_block)
        self.assertIn('id="voipPhone"', self.voip_block)
        self.assertIn('class="call-fab"', self.voip_block)
        self.assertLess(
            self.voip_block.find('id="voipState"'),
            self.voip_block.find('id="voipCallBtn"'),
        )
        self.assertLess(
            self.voip_block.find('id="voipPhone"'),
            self.voip_block.find('id="voipCallBtn"'),
        )

    def test_frontend_has_no_removed_voip_references(self):
        self.assertNotIn("voipNote", self.script)
        self.assertNotIn("voipRegistration", self.script)

    def test_android_style_call_button_styles_exist(self):
        self.assertIn(".call-fab", self.styles)
        self.assertIn("border-radius: 50%", self.styles)

    def test_pwa_icons_are_generated_from_dashboard_icons_router(self):
        source = ROOT / "static" / "dashboard-icons-router.svg"
        attribution = (ROOT / "docs" / "ICON_ATTRIBUTION.md").read_text(encoding="utf-8")
        self.assertTrue(source.is_file())
        self.assertIn("<svg", source.read_text(encoding="utf-8")[:200])
        self.assertIn("homarr-labs/dashboard-icons/blob/main/svg/router.svg", attribution)
        self.assertEqual(png_size(ROOT / "static" / "icon-192.png"), (192, 192))
        self.assertEqual(png_size(ROOT / "static" / "icon-512.png"), (512, 512))
        self.assertEqual(png_size(ROOT / "static" / "favicon.png"), (64, 64))
        self.assertIn("/static/icon-192.png", self.manifest)
        self.assertIn("/static/icon-512.png", self.manifest)
        self.assertIn("favicon.png", self.template)
        self.assertIn("apple-touch-icon", self.template)

    def test_service_worker_cache_includes_favicon(self):
        self.assertIn("gwmon-v7", self.worker)
        self.assertIn("/static/favicon.png", self.worker)


if __name__ == "__main__":
    unittest.main()
