import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from connector_manager import (
    DEFAULT_CATALOG,
    DEFAULT_CREDENTIALS,
    DEFAULT_SCHEMA,
    ConnectorManager,
)
from tools.connector_builder import main as builder_main


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path, data):
    Path(path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _minimal_connector(connector_id="vendor.model"):
    return {
        "id": connector_id,
        "display_name": "Vendor Model",
        "status": "observed",
        "evidence": {
            "source": "Synthetic test fixture.",
            "verified": True,
        },
        "identification": {
            "confidence": "exact",
            "rules": [
                {
                    "id": "model",
                    "phase": "pre_auth",
                    "source": "root",
                    "field": "ModelName",
                    "operator": "equals",
                    "value": "MODEL",
                    "confidence": "exact",
                    "evidence": "Synthetic test fixture.",
                }
            ],
        },
        "auth": {
            "flows": [
                {
                    "id": "none",
                    "type": "none",
                    "order": 1,
                    "success": {"field": "ok", "allowed": [True]},
                }
            ]
        },
        "endpoints": [
            {
                "id": "root",
                "method": "GET",
                "path": "/status",
                "format": "json",
                "timeout_seconds": 5,
            }
        ],
        "extraction": [
            {
                "id": "model-metric",
                "metric": "device.model",
                "endpoint": "root",
                "method": "json_path",
                "expression": "$.ModelName",
            }
        ],
        "validation": [],
        "dependencies": {},
        "capabilities": ["device_identity"],
    }


def _manager_for(catalog_data, credentials_data=None):
    temp = tempfile.TemporaryDirectory()
    root = Path(temp.name)
    catalog_path = root / "connectors.json"
    credentials_path = root / "credentials.json"
    _write(catalog_path, catalog_data)
    _write(credentials_path, credentials_data or _load(DEFAULT_CREDENTIALS))
    manager = ConnectorManager(catalog_path, credentials_path, DEFAULT_SCHEMA)
    return temp, manager


class ConnectorCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager = ConnectorManager()
        cls.catalog = _load(DEFAULT_CATALOG)
        cls.credentials = _load(DEFAULT_CREDENTIALS)

    def test_shipped_catalog_is_structurally_valid(self):
        report = self.manager.validate()
        self.assertTrue(report.structural_valid, [item.to_dict() for item in report.issues])
        self.assertEqual(report.connector_count, 1)
        self.assertEqual(report.errors, [])

    def test_compatibility_is_never_inferred_from_json(self):
        report = self.manager.validate()
        compatibility = report.to_dict()["device_compatibility"]
        self.assertEqual(compatibility["status"], "not_evaluated")
        self.assertTrue(compatibility["physical_device_check_required"])

    def test_identification_never_claims_exact_on_partial_evidence(self):
        result = self.manager.identify(
            {"device_name": {"ModelName": "HG6145F1"}}, phase="pre_auth"
        )
        self.assertEqual(result["confidence"], "partial")
        self.assertEqual(result["profile_ids"], ["fiberhome.hg6145f1"])
        self.assertTrue(result["needs_user_confirmation"])

    def test_ambiguous_exact_matches_are_downgraded(self):
        first = _minimal_connector("vendor.one")
        second = _minimal_connector("vendor.two")
        temp, manager = _manager_for({"schema_version": 1, "connectors": [first, second]})
        with temp:
            result = manager.identify({"root": {"ModelName": "MODEL"}})
            self.assertEqual(result["confidence"], "partial")
            self.assertTrue(result["ambiguous"])

    def test_catalog_and_credentials_store_no_secret_values(self):
        self.assertEqual(self.manager._scan_secrets(self.catalog, "$"), [])
        self.assertEqual(self.manager._scan_secrets(self.credentials, "$"), [])

    def test_voip_number_rules_require_an_observed_voice_port(self):
        connector = self.manager.get("fiberhome.hg6145f1")
        number_rules = [
            rule for rule in connector["extraction"]
            if rule["metric"] == "voip.phone_number"
        ]
        self.assertEqual(len(number_rules), 3)
        for rule in number_rules:
            conditions = rule.get("when_all", [])
            self.assertTrue(conditions)
            self.assertTrue(any(
                condition["field"] == "voice_status.voice_port_num"
                and condition["operator"] == "not_equals"
                and str(condition["value"]) == "0"
                for condition in conditions
            ))

    def test_duplicate_connector_ids_are_rejected(self):
        first = _minimal_connector("vendor.one")
        second = _minimal_connector("vendor.one")
        temp, manager = _manager_for({"schema_version": 1, "connectors": [first, second]})
        with temp:
            codes = [item.code for item in manager.validate().errors]
            self.assertIn("duplicate_connector_id", codes)

    def test_unknown_extraction_method_is_rejected(self):
        connector = _minimal_connector()
        connector["extraction"][0]["method"] = "magic_lookup"
        temp, manager = _manager_for({"schema_version": 1, "connectors": [connector]})
        with temp:
            codes = [item.code for item in manager.validate().errors]
            self.assertIn("unknown_extraction_method", codes)

    def test_unknown_metric_is_rejected(self):
        connector = _minimal_connector()
        connector["extraction"][0]["metric"] = "made.up.metric"
        temp, manager = _manager_for({"schema_version": 1, "connectors": [connector]})
        with temp:
            codes = [item.code for item in manager.validate().errors]
            self.assertIn("unknown_metric", codes)

    def test_conflicting_exact_identification_rules_are_rejected(self):
        first = _minimal_connector("vendor.one")
        second = _minimal_connector("vendor.two")
        temp, manager = _manager_for({"schema_version": 1, "connectors": [first, second]})
        with temp:
            codes = [item.code for item in manager.validate().errors]
            self.assertIn("conflicting_identification_rules", codes)

    def test_partial_evidence_cannot_back_exact_claim(self):
        connector = _minimal_connector()
        connector["identification"]["rules"][0]["confidence"] = "partial"
        temp, manager = _manager_for({"schema_version": 1, "connectors": [connector]})
        with temp:
            codes = [item.code for item in manager.validate().errors]
            self.assertIn("overclaimed_identification_confidence", codes)

    def test_secret_literal_in_catalog_is_rejected(self):
        connector = _minimal_connector()
        connector["auth"]["flows"][0]["fields"] = {"password": "DO_NOT_STORE"}
        temp, manager = _manager_for({"schema_version": 1, "connectors": [connector]})
        with temp:
            codes = [item.code for item in manager.validate().errors]
            self.assertIn("secret_value_in_catalog", codes)

    def test_credentials_require_explicit_confirmation(self):
        credentials = copy.deepcopy(self.credentials)
        credentials["credential_suggestions"][0].pop("requires_user_confirmation")
        temp, manager = _manager_for(
            {"schema_version": 1, "connectors": [_minimal_connector()]}, credentials
        )
        with temp:
            codes = [item.code for item in manager.validate().errors]
            self.assertIn("confirmation_required", codes)

    def test_dependency_cycle_is_rejected(self):
        connector = _minimal_connector()
        connector["endpoints"].append({
            "id": "second",
            "method": "GET",
            "path": "/second",
            "format": "json",
            "timeout_seconds": 5,
        })
        connector["dependencies"] = {"root": ["second"], "second": ["root"]}
        temp, manager = _manager_for({"schema_version": 1, "connectors": [connector]})
        with temp:
            codes = [item.code for item in manager.validate().errors]
            self.assertIn("dependency_cycle", codes)


class ConnectorBuilderCliTests(unittest.TestCase):
    def test_create_prints_secret_free_draft(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = builder_main([
                "create", "--id", "vendor.device", "--display-name", "Vendor Device",
            ])
        self.assertEqual(code, 0)
        draft = json.loads(output.getvalue())
        self.assertEqual(draft["status"], "draft")
        self.assertFalse(draft["enabled"])
        self.assertIn("__OBSERVE_MODEL__", draft["identification"]["rules"][0]["value"])
        self.assertEqual(
            ConnectorManager()._scan_secrets(draft, "$"), []
        )

    def test_create_can_append_and_rejects_duplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            catalog_path = Path(temp) / "connectors.json"
            with redirect_stdout(io.StringIO()):
                code = builder_main([
                    "create", "--id", "vendor.device", "--display-name", "Vendor Device",
                    "--output", str(catalog_path),
                ])
            self.assertEqual(code, 0)
            data = _load(catalog_path)
            self.assertEqual(len(data["connectors"]), 1)
            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    builder_main([
                        "create", "--id", "vendor.device", "--display-name", "Duplicate",
                        "--output", str(catalog_path),
                    ])

    def test_validate_cli_separates_compatibility(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = builder_main(["validate", "--json"])
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report["structural_valid"])
        self.assertEqual(
            report["device_compatibility"]["status"], "not_evaluated"
        )

    def test_list_cli_shows_shipped_profile(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = builder_main(["list"])
        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn("fiberhome.hg6145f1", text)
        self.assertIn("Device compatibility: NOT EVALUATED", text)


if __name__ == "__main__":
    unittest.main()
