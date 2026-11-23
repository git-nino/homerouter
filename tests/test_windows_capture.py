import json
import re
import unittest
from pathlib import Path

from connector_manager import ConnectorManager, DEFAULT_SCHEMA

WORKSPACE = Path(__file__).resolve().parents[1]
WINDOWS_DIR = WORKSPACE / "tools" / "windows"
PS1_PATH = WINDOWS_DIR / "Capture-ONT.ps1"
BAT_PATH = WINDOWS_DIR / "Capture-ONT.bat"
README_PATH = WINDOWS_DIR / "README.md"
DRAFT_FIXTURE = Path(__file__).parent / "fixtures" / "har" / "windows_draft_shape.json"
CHECKLIST_FIXTURE = Path(__file__).parent / "fixtures" / "har" / "windows_checklist_13.json"


class WindowsCaptureToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ps1 = PS1_PATH.read_text(encoding="utf-8-sig")
        cls.bat = BAT_PATH.read_text(encoding="utf-8-sig")
        cls.readme = README_PATH.read_text(encoding="utf-8-sig")
        cls.draft = json.loads(DRAFT_FIXTURE.read_text(encoding="utf-8"))
        cls.checklist = json.loads(CHECKLIST_FIXTURE.read_text(encoding="utf-8"))

    def test_required_files_present(self):
        for path in (PS1_PATH, BAT_PATH, README_PATH):
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file())
                self.assertGreater(path.stat().st_size, 0)

    def test_powershell_native_no_external_runtimes(self):
        self.assertIn("#requires -Version 5.1", self.ps1)
        self.assertIn("[CmdletBinding()]", self.ps1)
        self.assertIn("Get-NetRoute", self.ps1)
        self.assertIn("Get-NetIPConfiguration", self.ps1)
        self.assertIn("ConvertFrom-Json", self.ps1)
        command_pattern = re.compile(
            r"^\s*(?:&|cmd(?:\s+/c)?\s+)?(?:python3?|pwsh|wsl|docker|node|npm)(?:\.exe)?\b",
            re.IGNORECASE,
        )
        invocations = [
            line.strip()
            for line in self.ps1.splitlines()
            if command_pattern.search(line)
        ]
        self.assertEqual([], invocations)
        self.assertNotIn("Start-Process python", self.ps1.lower())
        self.assertIn("powershell", self.bat.lower())
        self.assertIn("-ExecutionPolicy Bypass", self.bat)

    def test_cli_and_host_selection(self):
        for flag in ("-HarPath", "-Gateway", "-Hostname", "-OutputDirectory"):
            with self.subTest(flag=flag):
                self.assertIn(flag, self.ps1)
        self.assertIn("$targetHost", self.ps1)
        self.assertIn("Target for HAR filtering", self.ps1)
        self.assertIn("$ignoredCount", self.ps1)

    def test_har_and_redaction_coverage(self):
        for marker in ("log", "entries", "request", "response", "queryString", "postData"):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.ps1)
        self.assertIn(
            "Invoke-RedactText -Text ([string] $rep.url_info.query[$name]) -FieldName",
            self.ps1,
        )
        for marker in (
            "Authorization",
            "Cookie",
            "Set-Cookie",
            "password",
            "token",
            "csrf",
            "nonce",
            "phone",
            "public",
            "serial",
            "mac",
            "hostname",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker.lower(), self.ps1.lower())

    def test_readme_documents_workflow_and_limits(self):
        for marker in (
            "Edge",
            "Chrome",
            "Preserve log",
            "Network",
            "-Gateway",
            "status",
            "WAN",
            "optical",
            "VoIP",
            "devices",
            "validate-draft",
            "import-draft",
            "--approve",
            "not",
            "manual",
            "FirmwareFamily",
            "manufacturer.model",
            "HTML",
            "XML",
            "private",
            "fail closed",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.readme)

    def test_draft_fixture_matches_contract(self):
        manager = ConnectorManager(schema_path=DEFAULT_SCHEMA)
        manager.catalog = {"schema_version": 1, "connectors": [self.draft]}
        report = manager.validate()
        errors = [i for i in report.issues if i.severity == "error"]
        self.assertEqual([], [i.message for i in errors])
        self.assertEqual("draft", self.draft["status"])
        self.assertIs(self.draft["enabled"], False)
        self.assertIs(self.draft["evidence"]["verified"], False)
        self.assertTrue(self.draft["source_har"])
        self.assertRegex(self.draft["id"], r"^(observed\.[a-z0-9.-]+|[a-z0-9]+(?:\.[a-z0-9-]+)+)$")
        self.assertNotRegex(self.draft["id"], r"\d{1,3}(?:\.\d{1,3}){3}")
        for rule in self.draft.get("extraction", []):
            self.assertEqual(rule.get("method"), "json_path")
            self.assertNotIn("transforms", rule)
            self.assertNotIn("value_type", rule)
        self.assertEqual(self.draft.get("validation"), [])

    def test_windows_13_point_checklist_was_executed(self):
        self.assertTrue(self.checklist["executed_on_windows"])
        self.assertEqual("Desktop", self.checklist["powershell_edition"])
        self.assertTrue(self.checklist["powershell_version"].startswith("5.1."))
        self.assertTrue(self.checklist["windows_version"].startswith("Microsoft Windows NT 10.0."))
        checks = self.checklist["checklist"]
        self.assertEqual(13, len(checks))
        for name, result in checks.items():
            with self.subTest(check=name):
                if name in ("01_windows_version", "07_draft_status"):
                    self.assertTrue(result)
                else:
                    self.assertIs(result, True)


if __name__ == "__main__":
    unittest.main()
