import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from connector_executor import ConnectorExecutor
from connector_manager import DEFAULT_CREDENTIALS, DEFAULT_SCHEMA, ConnectorManager
from models.universal import METRICS
from tests.fixtures import RecordingSession, manager_for_catalog, synthetic_connector
from tests.test_connector_manager import _minimal_connector
from tools.connector_builder import main as builder_main
from tools.har_analyzer import analyze_har, filter_entries, parse_har, sanitize, write_outputs


FIXTURE = Path(__file__).parent / "fixtures" / "har" / "synthetic_ont.har"
GATEWAY = "192.168.1.1"
SECRETS = [
    "SuperSecret123",
    "token123456789",
    "csrf987654321",
    "203.0.113.7",
    "+15551234567",
    "aa:bb:cc:dd:ee:ff",
    "living-room-nas",
    "SYNTHSERIAL001",
    "sess-abcdef",
    "Synthetic-Laptop",
]


class URLRecordingSession(RecordingSession):
    def __init__(self, payloads):
        super().__init__(payloads)
        self.urls = []

    def request(self, method, url, **kwargs):
        self.urls.append(url)
        return super().request(method, url, **kwargs)


class HarParsingTests(unittest.TestCase):
    def setUp(self):
        self.analysis = analyze_har(FIXTURE, GATEWAY)

    def test_parse_and_host_filter(self):
        entries = parse_har(FIXTURE)
        self.assertEqual(len(entries), 5)
        selected = filter_entries(entries, GATEWAY)
        self.assertEqual(len(selected), 4)
        self.assertNotIn(
            "telemetry.example.net",
            json.dumps([item["request"]["url"] for item in selected]),
        )
        self.assertEqual(self.analysis["total_entries"], 5)
        self.assertEqual(self.analysis["filtered_entries"], 4)

    def test_json_analysis_and_call_sequence(self):
        details = {item["id"]: item for item in self.analysis["details"]}
        self.assertEqual(
            [item["id"] for item in self.analysis["details"]],
            [
                "get_cgi_bin_ajax",
                "post_cgi_bin_ajax",
                "get_cgi_bin_ajax_02",
                "get_web_style_css",
            ],
        )
        self.assertEqual(details["post_cgi_bin_ajax"]["method"], "POST")
        self.assertEqual(details["post_cgi_bin_ajax"]["body"]["ajaxmethod"], "do_login")
        self.assertEqual(details["post_cgi_bin_ajax"]["status"], 200)
        self.assertEqual(
            details["get_cgi_bin_ajax"]["response"]["ModelName"],
            "SyntheticModel X1",
        )
        status = details["get_cgi_bin_ajax_02"]["response"]
        self.assertEqual(status["WAN"]["Status"], "up")
        self.assertEqual(status["Optical"]["RxPower"], -21.5)
        self.assertEqual(status["Devices"][0]["HostName"], "<redacted-hostname>")
        self.assertEqual(self.analysis["sequence"][0]["path"], "/cgi-bin/ajax")

    def test_redaction_preserves_structure(self):
        payload = {
            "Password": "hunter2",
            "SessionId": "abc123",
            "Nested": {"PublicIP": "198.51.100.9", "Count": 2},
        }
        result = sanitize(payload)
        self.assertEqual(result["Password"], "<redacted-password>")
        self.assertEqual(result["SessionId"], "<redacted-session>")
        self.assertEqual(result["Nested"]["PublicIP"], "<redacted-public-ip>")
        self.assertEqual(result["Nested"]["Count"], 2)

    def test_outputs_never_contain_raw_har_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = write_outputs(self.analysis, Path(directory))
            written = "\n".join(
                path.read_text(encoding="utf-8")
                for path in Path(directory).rglob("*")
                if path.is_file()
            )
            for secret in SECRETS:
                self.assertNotIn(secret, written)
            self.assertTrue(Path(paths["draft"]).is_file())
            evidence = Path(paths["evidence"])
            self.assertTrue((evidence / "capture-summary.json").is_file())
            self.assertTrue((evidence / "redaction-report.json").is_file())
            self.assertEqual(len(list((evidence / "endpoints").glob("*.json"))), 4)


class AuthClassificationTests(unittest.TestCase):
    def _classification(self, *, headers=None, body=None, path="/status"):
        request = {
            "method": "POST" if body is not None else "GET",
            "url": f"http://{GATEWAY}{path}",
            "headers": [
                {"name": name, "value": value}
                for name, value in (headers or {}).items()
            ],
        }
        if body is not None:
            request["postData"] = {
                "mimeType": "application/json",
                "text": json.dumps(body),
            }
        har = {
            "log": {
                "version": "1.2",
                "entries": [{
                    "request": request,
                    "response": {
                        "status": 200,
                        "content": {"mimeType": "application/json", "text": "{}"},
                    },
                }],
            }
        }
        return analyze_har(har, GATEWAY)

    def test_standard_handlers(self):
        cases = [
            (self._classification(), "none", "none"),
            (
                self._classification(headers={"Authorization": "Basic dXNlcjpwYXNz"}),
                "http_basic", "http_basic",
            ),
            (
                self._classification(headers={"Authorization": "Digest username=user"}),
                "http_digest", "http_digest",
            ),
            (
                self._classification(headers={"Authorization": "Bearer abcdef123456"}),
                "token", "token",
            ),
            (
                self._classification(headers={"Set-Cookie": "session=abc123"}),
                "cookie_session", "session",
            ),
            (
                self._classification(path="/login", body={"username": "admin"}),
                "html_form", "form",
            ),
        ]
        for analysis, expected, handler in cases:
            with self.subTest(expected=expected):
                self.assertEqual(analysis["auth_observations"]["classification"], expected)
                self.assertEqual(analysis["auth_observations"]["proposed_handler"], handler)
                self.assertFalse(analysis["draft"]["requires_manual_auth_handler"])

    def test_proprietary_challenge_requires_manual_handler(self):
        analysis = self._classification(
            path="/login",
            body={"username": "admin", "nonce": "xyz", "loginpd": "encrypted"},
        )
        observations = analysis["auth_observations"]
        self.assertEqual(observations["classification"], "proprietary_challenge_response")
        self.assertEqual(observations["proposed_handler"], "manual_handler_required")
        self.assertTrue(analysis["draft"]["requires_manual_auth_handler"])
        self.assertTrue(
            any("auth_handlers.py" in item for item in analysis["draft"]["manual_review"])
        )


class DraftProfileTests(unittest.TestCase):
    def setUp(self):
        self.analysis = analyze_har(FIXTURE, GATEWAY)
        self.draft = self.analysis["draft"]

    def test_draft_shape_and_evidence(self):
        self.assertEqual(self.draft["status"], "draft")
        self.assertIs(self.draft["enabled"], False)
        self.assertIs(self.draft["evidence"]["verified"], False)
        self.assertIn("HAR analysis", self.draft["evidence"]["source"])
        self.assertEqual(self.draft["identification"]["confidence"], "unknown")
        self.assertTrue(self.draft["manual_review"])
        self.assertTrue(self.draft["limitations"])
        self.assertTrue(self.draft["auth_observations"]["sequence"])
        self.assertTrue(self.draft["metric_mapping_candidates"])
        for candidate in self.draft["metric_mapping_candidates"]:
            self.assertIn(candidate["metric"], METRICS)
            self.assertTrue(candidate["manual_review"])
            self.assertIn(candidate["path"], candidate["evidence"])
        self.assertIn("connected_devices", self.draft["capabilities"])

    def test_draft_passes_project_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = write_outputs(self.analysis, Path(directory))
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = builder_main(["validate-draft", paths["draft"], "--json"])
            self.assertEqual(code, 0, stdout.getvalue())
            report = json.loads(stdout.getvalue())
            self.assertTrue(report["valid"])
            self.assertEqual(report["errors"], [])
            self.assertEqual(report["device_compatibility"], "not_evaluated")

    def test_schema_rejects_unimplemented_extraction(self):
        connector = _minimal_connector()
        connector["extraction"] = [{
            "id": "bad-xpath",
            "endpoint": "root",
            "method": "xpath",
            "expression": "//div",
            "metric": "device.model",
        }]
        with manager_for_catalog({"schema_version": 1, "connectors": [connector]}) as manager:
            codes = [issue.code for issue in manager.validate().errors]
        self.assertIn("unknown_extraction_method", codes)

    def test_schema_rejects_unsupported_transport_options(self):
        connector = _minimal_connector()
        connector["scheme"] = "ftp"
        connector["endpoints"][0]["body_format"] = "xml"
        with manager_for_catalog({"schema_version": 1, "connectors": [connector]}) as manager:
            codes = [issue.code for issue in manager.validate().errors]
        self.assertIn("unsupported_scheme", codes)
        self.assertIn("unsupported_body_format", codes)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.analysis = analyze_har(FIXTURE, GATEWAY)
        self.draft = self.analysis["draft"]

    def _write(self, directory, catalog):
        draft_path = Path(directory) / "draft.json"
        catalog_path = Path(directory) / "connectors.json"
        draft_path.write_text(json.dumps(self.draft, indent=2), encoding="utf-8")
        catalog_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
        return draft_path, catalog_path

    def _shipped_catalog(self):
        return json.loads(
            (Path(__file__).parent.parent / "config" / "connectors.json").read_text(
                encoding="utf-8"
            )
        )

    def test_import_requires_approval_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = self._shipped_catalog()
            original = copy.deepcopy(catalog)
            draft_path, catalog_path = self._write(directory, catalog)

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = builder_main([
                    "import-draft", str(draft_path), "--catalog", str(catalog_path),
                ])
            self.assertEqual(code, 2)
            self.assertIn("--approve", stderr.getvalue())
            self.assertEqual(json.loads(catalog_path.read_text()), original)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = builder_main([
                    "import-draft", str(draft_path), "--catalog", str(catalog_path),
                    "--approve",
                ])
            self.assertEqual(code, 0, stdout.getvalue())
            written = json.loads(catalog_path.read_text())
            self.assertEqual(written["connectors"][:-1], original["connectors"])
            imported = written["connectors"][-1]
            self.assertEqual(imported["id"], self.draft["id"])
            self.assertIs(imported["enabled"], False)
            self.assertEqual(imported["status"], "draft")

            with redirect_stderr(stderr):
                code = builder_main([
                    "import-draft", str(draft_path), "--catalog", str(catalog_path),
                    "--approve",
                ])
            self.assertEqual(code, 1)
            self.assertEqual(
                json.loads(catalog_path.read_text())["connectors"],
                written["connectors"],
            )

    def test_secret_leak_is_rejected(self):
        connector = _minimal_connector("leaky.profile")
        connector["evidence"] = {
            "source": "Observed Authorization: Bearer abcdef123456 at 203.0.113.7",
            "verified": False,
        }
        with manager_for_catalog({"schema_version": 1, "connectors": [connector]}) as manager:
            issues = manager.validate().issues
        codes = {issue.code for issue in issues if issue.severity == "error"}
        self.assertIn("authorization_value_in_catalog", codes)
        self.assertIn("public_ip_in_catalog", codes)


class RuntimeCompatibilityTests(unittest.TestCase):
    def test_json_post_uses_https_and_tls_verification(self):
        profile = synthetic_connector()
        profile["scheme"] = "https"
        profile["endpoints"] = [{
            "id": "json_post",
            "method": "POST",
            "path": "/json",
            "body_format": "json",
            "body": {"hello": "world"},
            "format": "json",
            "timeout_seconds": 5,
            "auth": False,
        }]
        with manager_for_catalog({"schema_version": 1, "connectors": [profile]}) as manager:
            session = URLRecordingSession({"/json": {"ok": True}})
            executor = ConnectorExecutor(
                manager.get("aurora.x1"),
                "192.168.5.1",
                {"username": "tester", "password": "unit-test-password"},
                manager=manager,
                session=session,
            )
            result = executor.execute_endpoint("json_post")
        self.assertEqual(result.payload, {"ok": True})
        self.assertEqual(session.urls, ["https://192.168.5.1/json"])
        self.assertEqual(session.calls[0]["json"], {"hello": "world"})
        self.assertEqual(session.calls[0]["data"], {})
        self.assertIs(session.calls[0]["verify"], True)

    def test_default_credentials_match_app_contract(self):
        data = json.loads(DEFAULT_CREDENTIALS.read_text(encoding="utf-8"))
        required = {
            "key", "default_username", "default_password",
            "requires_user_confirmation", "provenance",
        }
        for suggestion in data["credential_suggestions"]:
            with self.subTest(key=suggestion.get("key")):
                self.assertTrue(required.issubset(suggestion))
                self.assertIs(suggestion["requires_user_confirmation"], True)

    def test_fiberhome_profile_regression(self):
        catalog = json.loads(
            (Path(__file__).parent.parent / "config" / "connectors.json").read_text(
                encoding="utf-8"
            )
        )
        with manager_for_catalog(catalog) as manager:
            report = manager.validate()
            profile = manager.get("fiberhome.hg6145f1")
        self.assertEqual(report.errors, [])
        self.assertIsNotNone(profile)
        self.assertEqual(profile["status"], "observed")
        self.assertIs(profile["enabled"], True)
        self.assertEqual(profile["auth"]["flows"][0]["type"], "fiberhome_aes")


if __name__ == "__main__":
    unittest.main()
