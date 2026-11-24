"""Deterministic M3 Profile Builder and registration tests.

Every execution path here is offline and fixture-backed. No real router is
contacted, no credentials are used, and synthetic profiles never enter the
shipped connector catalog.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from profile_safety import (  # noqa: E402
    MAX_RESPONSE_BYTES,
    UnsafeProfileRequest,
    build_request_url,
    check_response_size,
    normalize_gateway,
    read_bounded_text,
)
from connector_executor import ConnectorExecutor, EndpointError  # noqa: E402
from tools import connector_builder as builder  # noqa: E402


def run_cli(argv):
    """Run a builder command and return (exit code, stdout, stderr)."""

    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = builder.main(list(argv))
    return code, stdout.getvalue(), stderr.getvalue()


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def make_profile(root, connector_id="synthetic.testmodel", model="TestModel"):
    """Build a synthetic disabled draft through the real CLI commands."""

    code, out, _ = run_cli([
        "create", "--id", connector_id, "--display-name", "Synthetic Test Model",
    ])
    if code != 0:
        raise AssertionError(f"create failed: {out}")
    path = root / f"{connector_id}.json"
    path.write_text(out, encoding="utf-8")

    code, out, _ = run_cli([
        "configure", str(path),
        "--manufacturer", "synthetic",
        "--model", model,
        "--endpoint-id", "root",
        "--path", "/status",
        "--method", "GET",
    ])
    if code != 0:
        raise AssertionError(f"configure failed: {out}")

    code, out, _ = run_cli([
        "map", str(path),
        "--endpoint", "root",
        "--field", "$.Serial",
        "--metric", "device.serial",
    ])
    if code != 0:
        raise AssertionError(f"map failed: {out}")

    # Manual review replaces observed placeholders before registration.
    data = json.loads(path.read_text(encoding="utf-8"))
    data["identification"]["rules"][0]["value"] = "TestModel"
    data["auth"]["flows"][0]["success"]["field"] = "Ready"
    write_json(path, data)
    return path


def make_fixture(root, **overrides):
    data = {
        "schema_version": 1,
        "gateway": "192.0.2.1",
        "responses": {
            "/status": {"Model": "TestModel", "Serial": "SECRET-SERIAL-123", "Firmware": "1.0"},
        },
    }
    data.update(overrides)
    return write_json(root / "fixture.json", data)


class ProfileSafetyTests(unittest.TestCase):
    def test_gateway_helpers_reject_urls_userinfo_ports_and_metadata(self):
        self.assertEqual(normalize_gateway("192.168.1.1"), "192.168.1.1")
        for bad in (
            "http://192.0.2.1",
            "http://192.0.2.1/path",
            "//192.0.2.1",
            "192.0.2.1:8080",
            "user@192.0.2.1",
            "127.0.0.1",
            "127.1",
            "2130706433",
            "0x7f000001",
            "0177.0.0.1",
            "0x7f.1",
            "127.0.0x1",
            "0x7f.0x1",
            "0.0.0.0",
            "169.254.169.254",
            "metadata.google.internal",
            "localhost",
        ):
            with self.subTest(bad=bad), self.assertRaises(UnsafeProfileRequest):
                normalize_gateway(bad)

    def test_endpoint_paths_are_gateway_pinned(self):
        self.assertEqual(
            build_request_url("http", "192.0.2.1", "/cgi-bin/ajax"),
            "http://192.0.2.1/cgi-bin/ajax",
        )
        for bad in ("//evil.example/x", "/../etc/passwd", "/x?y=1", "/x#f", "/x%2f", "\\evil"):
            with self.subTest(bad=bad), self.assertRaises(UnsafeProfileRequest):
                build_request_url("http", "192.0.2.1", bad)

    def test_oversize_response_text_is_rejected(self):
        class Response:
            headers = {"Content-Length": str(MAX_RESPONSE_BYTES + 1)}
            text = ""

        with self.assertRaises(UnsafeProfileRequest):
            check_response_size(Response())

    def test_streamed_response_is_bounded_before_buffering(self):
        class StreamResponse:
            headers = {}
            encoding = "utf-8"

            def iter_content(self, chunk_size):
                yield b"x" * 8192
                yield b"x" * (MAX_RESPONSE_BYTES - 1)

        with self.assertRaises(UnsafeProfileRequest):
            read_bounded_text(StreamResponse())

        class SmallStreamResponse(StreamResponse):
            def iter_content(self, chunk_size):
                yield b'{"ok":true}'

        self.assertEqual(read_bounded_text(SmallStreamResponse()), '{"ok":true}')


class BuilderWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="homerouter-m3-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = make_profile(self.root)
        self.fixture = make_fixture(self.root)

    def test_discovery_helpers_are_wired(self):
        code, out, _ = run_cli(["auth-handlers", "--json"])
        self.assertEqual(code, 0)
        self.assertIn("fiberhome_aes", json.loads(out)["handlers"])

        code, out, _ = run_cli(["metrics", "--json"])
        self.assertEqual(code, 0)
        metric_ids = {row["id"] for row in json.loads(out)["metrics"]}
        self.assertIn("device.serial", metric_ids)
        self.assertIn("speed_test.status", metric_ids)

        code, out, _ = run_cli(["manufacturers", "--json"])
        self.assertEqual(code, 0)
        self.assertIn("fiberhome", [row["id"] for row in json.loads(out)["manufacturers"]])

    def test_inspect_returns_field_paths_but_no_values(self):
        code, out, _ = run_cli([
            "inspect", str(self.profile), "--fixture", str(self.fixture), "--json",
        ])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["mode"], "offline_fixture")
        self.assertEqual(report["device_compatibility"], "not_evaluated")
        fields = {row["path"] for row in report["endpoints"][0]["fields"]}
        self.assertIn("$.Serial", fields)
        self.assertNotIn("SECRET-SERIAL-123", out)
        self.assertNotIn("TestModel", out)

    def test_dry_run_is_offline_redacted_and_never_claims_compatibility(self):
        with patch("requests.Session", side_effect=AssertionError("network is forbidden")):
            code, out, _ = run_cli([
                "test", str(self.profile), "--fixture", str(self.fixture), "--json",
            ])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["mode"], "offline_fixture")
        self.assertEqual(report["device_compatibility"], "not_evaluated")
        self.assertTrue(report["physical_device_check_required"])
        self.assertEqual(
            report["domains"]["device_identity"]["device.serial"]["value"],
            "[REDACTED]",
        )
        self.assertNotIn("SECRET-SERIAL-123", out)

    def test_unsafe_paths_and_methods_are_rejected_without_writing(self):
        before = self.profile.read_text(encoding="utf-8")
        with self.assertRaises(SystemExit):
            run_cli([
                "configure", str(self.profile), "--endpoint-id", "root",
                "--path", "//evil.example/x",
            ])
        with self.assertRaises(SystemExit):
            run_cli([
                "configure", str(self.profile), "--endpoint-id", "root",
                "--path", "/status", "--method", "DELETE",
            ])
        self.assertEqual(self.profile.read_text(encoding="utf-8"), before)

    def test_fixtures_reject_secret_keys_and_unsafe_gateways(self):
        bad_credentials = json.loads(self.fixture.read_text(encoding="utf-8"))
        bad_credentials["responses"]["/status"]["password"] = "not-a-real-password"
        credential_path = write_json(self.root / "bad-credentials.json", bad_credentials)
        with self.assertRaises(SystemExit):
            run_cli(["test", str(self.profile), "--fixture", str(credential_path)])

        bad_gateway = json.loads(self.fixture.read_text(encoding="utf-8"))
        bad_gateway["gateway"] = "http://169.254.169.254/latest/meta-data"
        gateway_path = write_json(self.root / "bad-gateway.json", bad_gateway)
        with self.assertRaises(SystemExit):
            run_cli(["test", str(self.profile), "--fixture", str(gateway_path)])

    def test_redirects_are_rejected_and_never_followed(self):
        redirect_fixture = make_fixture(self.root, status_overrides={"/status": 302})
        code, out, _ = run_cli([
            "test", str(self.profile), "--fixture", str(redirect_fixture), "--json",
        ])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertTrue(any("redirect" in item for item in report["discovery_errors"]))
        self.assertTrue(any("redirect" in item.get("error", "") for item in report["endpoint_errors"]))

    def test_redirect_and_error_responses_are_closed(self):
        profile = json.loads(self.profile.read_text(encoding="utf-8"))
        profile["enabled"] = True

        class CloseTrackingResponse:
            def __init__(self, status_code):
                self.status_code = status_code
                self.text = "{}"
                self.headers = {"Content-Type": "application/json"}
                self.closed = False

            def close(self):
                self.closed = True

        class CloseTrackingSession:
            def __init__(self, response):
                self.headers = {}
                self.response = response

            def request(self, method, url, **kwargs):
                return self.response

        for status_code in (302, 500):
            with self.subTest(status_code=status_code):
                response = CloseTrackingResponse(status_code)
                session = CloseTrackingSession(response)
                executor = ConnectorExecutor(
                    profile, "192.0.2.1", {}, session=session,
                )
                with self.assertRaises(EndpointError):
                    executor.execute_endpoint("root")
                self.assertTrue(response.closed)

    def test_oversize_fixtures_are_rejected(self):
        oversize = make_fixture(self.root, responses={
            "/status": {"Blob": "x" * (MAX_RESPONSE_BYTES + 1)},
        })
        with self.assertRaises(SystemExit):
            run_cli(["test", str(self.profile), "--fixture", str(oversize)])

    def test_metric_mapping_rejects_unknown_metrics(self):
        with self.assertRaises(SystemExit):
            run_cli([
                "map", str(self.profile), "--endpoint", "root",
                "--field", "$.Model", "--metric", "not.a.metric",
            ])

    def test_registration_requires_approval_is_manufacturer_constrained_and_idempotent(self):
        catalog_path = self.root / "catalog.json"
        equipment_path = self.root / "equipment.json"
        write_json(catalog_path, {"schema_version": 1, "connectors": []})
        write_json(equipment_path, {
            "schema_version": 1,
            "manufacturers": [{"id": "synthetic", "name": "Synthetic", "profiles": []}],
        })
        base = [
            "register", str(self.profile),
            "--manufacturer", "synthetic",
            "--model", "TestModel",
            "--catalog", str(catalog_path),
            "--equipment", str(equipment_path),
        ]

        code, _, stderr = run_cli(base)
        self.assertEqual(code, 2)
        self.assertIn("--approve", stderr)
        self.assertEqual(json.loads(catalog_path.read_text())["connectors"], [])

        code, out, _ = run_cli(base + ["--approve", "--json"])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["registered"], "synthetic.testmodel")
        self.assertEqual(report["device_compatibility"], "not_evaluated")

        catalog = json.loads(catalog_path.read_text())
        equipment = json.loads(equipment_path.read_text())
        self.assertEqual(len(catalog["connectors"]), 1)
        self.assertIs(catalog["connectors"][0]["enabled"], False)
        self.assertEqual(equipment["manufacturers"][0]["profiles"], ["synthetic.testmodel"])

        code, out, _ = run_cli(base + ["--approve", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(catalog_path.read_text())["connectors"]), 1)
        self.assertEqual(
            json.loads(equipment_path.read_text())["manufacturers"][0]["profiles"],
            ["synthetic.testmodel"],
        )

        code, out, _ = run_cli([
            "models", "--catalog", str(catalog_path), "--equipment", str(equipment_path), "--json",
        ])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["models"][0]["registered"])

        duplicate_profile = make_profile(
            self.root, connector_id="synthetic.testmodel2", model="TestModel",
        )
        with self.assertRaises(SystemExit):
            run_cli([
                "register", str(duplicate_profile),
                "--manufacturer", "synthetic", "--model", "TestModel",
                "--catalog", str(catalog_path), "--equipment", str(equipment_path),
                "--approve",
            ])

        with self.assertRaises(SystemExit):
            run_cli(base[:-4] + [
                "--manufacturer", "other", "--model", "TestModel",
                "--catalog", str(catalog_path), "--equipment", str(equipment_path),
                "--approve",
            ])

    def test_registration_rejects_unresolved_placeholders(self):
        unresolved = json.loads(self.profile.read_text(encoding="utf-8"))
        unresolved["identification"]["rules"][0]["value"] = "__OBSERVE_MODEL__"
        unresolved_path = write_json(self.root / "unresolved.json", unresolved)
        with self.assertRaises(SystemExit) as caught:
            run_cli([
                "register", str(unresolved_path),
                "--manufacturer", "synthetic", "--model", "TestModel",
                "--catalog", str(self.root / "catalog.json"),
                "--equipment", str(self.root / "equipment.json"),
                "--approve",
            ])
        self.assertIn("placeholder", str(caught.exception))

    def test_dangling_equipment_references_block_registration(self):
        catalog_path = self.root / "catalog.json"
        equipment_path = self.root / "equipment.json"
        write_json(catalog_path, {"schema_version": 1, "connectors": []})
        write_json(equipment_path, {
            "schema_version": 1,
            "manufacturers": [{
                "id": "synthetic", "name": "Synthetic", "profiles": ["synthetic.missing"],
            }],
        })
        code, out, _ = run_cli([
            "register", str(self.profile),
            "--manufacturer", "synthetic", "--model", "TestModel",
            "--catalog", str(catalog_path), "--equipment", str(equipment_path),
            "--approve",
        ])
        self.assertEqual(code, 1)
        self.assertIn("validation failed", out)


class ApiBoundaryTests(unittest.TestCase):
    def test_connect_rejects_arbitrary_gateway_urls(self):
        with patch("gateway.detect_gateway", return_value={
            "gateway": "192.168.1.1", "model": "", "detected": False,
        }):
            import app as app_module  # noqa: E402

        client = app_module.app.test_client()
        for target in (
            "http://192.168.1.1",
            "http://169.254.169.254/latest/meta-data",
            "127.1",
            "2130706433",
            "0x7f000001",
            "0177.0.0.1",
            "0x7f.1",
            "127.0.0x1",
            "0x7f.0x1",
        ):
            with self.subTest(target=target):
                response = client.post("/api/connect", json={
                    "username": "tester",
                    "password": "unused-test-password",
                    "gateway": target,
                    "manufacturer_id": "fiberhome",
                })
                self.assertEqual(response.status_code, 400)
                self.assertIn("Invalid gateway", response.get_json()["message"])


if __name__ == "__main__":
    unittest.main()
