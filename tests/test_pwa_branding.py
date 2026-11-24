"""PWA branding checks for the FTTH viewer identity and icon set."""

import json
import re
import struct
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def png_size(path):
    data = path.read_bytes()[:24]
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    return struct.unpack(">II", data[16:24])


class PwaBrandingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(
            (STATIC / "manifest.webmanifest").read_text(encoding="utf-8")
        )
        cls.html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        cls.worker = (STATIC / "sw.js").read_text(encoding="utf-8")
        cls.svg = (STATIC / "icon-master.svg").read_text(encoding="utf-8")

    def test_manifest_identity(self):
        self.assertEqual(self.manifest["name"], "FTTH viewer")
        self.assertEqual(self.manifest["short_name"], "FTTH viewer")
        self.assertEqual(self.manifest["theme_color"], "#052b21")
        self.assertEqual(self.manifest["background_color"], "#052b21")

    def test_manifest_icons_exist_and_match_declared_sizes(self):
        purposes = set()
        for icon in self.manifest["icons"]:
            self.assertEqual(icon["type"], "image/png")
            path = ROOT / icon["src"].lstrip("/")
            self.assertTrue(path.is_file(), icon["src"])
            declared = tuple(int(value) for value in icon["sizes"].split("x"))
            self.assertEqual(png_size(path), declared, icon["src"])
            purposes.add(icon["purpose"])
        self.assertEqual(purposes, {"any", "maskable"})

    def test_required_icon_assets_have_expected_sizes(self):
        expected = {
            "icon-192.png": (192, 192),
            "icon-512.png": (512, 512),
            "icon-maskable-192.png": (192, 192),
            "icon-maskable-512.png": (512, 512),
            "apple-touch-icon.png": (180, 180),
            "favicon.png": (64, 64),
        }
        for name, size in expected.items():
            self.assertEqual(png_size(STATIC / name), size, name)

    def test_master_svg_is_valid_and_text_free(self):
        root = ET.fromstring(self.svg)
        self.assertTrue(root.tag.endswith("svg"))
        self.assertIn("FTTH viewer", root.attrib.get("aria-label", ""))
        self.assertNotIn("<text", self.svg)
        for color in ("#0d5b43", "#b9ff78", "#ffffff"):
            self.assertIn(color, self.svg)

    def test_html_metadata_uses_new_identity(self):
        self.assertIn("<title>FTTH viewer</title>", self.html)
        self.assertIn('<meta name="theme-color" content="#052b21">', self.html)
        self.assertIn(
            '<meta name="apple-mobile-web-app-title" content="FTTH viewer">',
            self.html,
        )
        self.assertIn("filename='apple-touch-icon.png'", self.html)
        brand = re.search(r'<div class="brand">(.*?)</div>', self.html, re.S)
        self.assertIsNotNone(brand)
        brand_text = re.sub(r"<[^>]+>", "", brand.group(1)).strip()
        self.assertEqual(brand_text, "◈ FTTH viewer")

    def test_service_worker_cache_is_v9_and_references_branding_assets(self):
        self.assertIn("const CACHE = 'gwmon-v9';", self.worker)
        self.assertNotIn("gwmon-v8", self.worker)
        shell = self.worker.split("const SHELL = [", 1)[1].split("];", 1)[0]
        for asset in re.findall(r"'([^']+)'", shell):
            if asset == "/":
                self.assertTrue((ROOT / "templates" / "index.html").is_file())
            else:
                self.assertTrue((ROOT / asset.lstrip("/")).is_file(), asset)
        for asset in (
            "/static/apple-touch-icon.png",
            "/static/icon-maskable-192.png",
            "/static/icon-maskable-512.png",
        ):
            self.assertIn("'" + asset + "'", shell, asset)

    def test_no_stale_user_facing_name(self):
        for path in (
            STATIC / "manifest.webmanifest",
            STATIC / "sw.js",
            ROOT / "templates" / "index.html",
        ):
            text = path.read_text(encoding="utf-8")
            for stale in ("Gateway Monitor", "GatewayMon", "GATEWAYMONITOR"):
                self.assertNotIn(stale, text, (path, stale))


if __name__ == "__main__":
    unittest.main()
