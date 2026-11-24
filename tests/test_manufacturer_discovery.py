"""Focused M2 manufacturer-constrained discovery tests.

These tests use only synthetic profiles and mocked unauthenticated probes. They
verify that the optional equipment selector narrows discovery without changing
the frozen public API shapes or legacy omitted-selector behavior.
"""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from connector_manager import (  # noqa: E402
    DEFAULT_CREDENTIALS,
    DEFAULT_SCHEMA,
    ConnectorManager,
)
from tests.fixtures import manager_for_catalog, synthetic_connector  # noqa: E402

# app.py calls detect_gateway() at import time; keep tests offline.
with patch("gateway.detect_gateway", return_value={
    "gateway": "192.168.1.1", "model": "", "detected": False,
}):
    import app as app_module  # noqa: E402


DETECTION = {"gateway": "192.168.1.1", "model": "", "detected": True}
DISCOVERY = {
    "gateway": "192.168.1.1",
    "gateway_detected": True,
    "detected": True,
    "detected_device": "HG6145F1",
    "selected_profile": "fiberhome.hg6145f1",
}

DISCOVERY_KEYS = {
    "gateway", "detected", "gateway_detected", "detected_device",
    "selected_profile", "identification_confidence", "evidence",
    "credential_suggestion", "profiles", "discovery_errors",
    "physical_device_check_required",
}
PROFILE_ROW_KEYS = {
    "id", "display_name", "status", "identification_confidence",
    "capabilities", "device_compatibility",
}
LOGIN_DEFAULTS_KEYS = {
    "gateway", "gateway_detected", "supported", "username", "password",
    "message",
}


class RecordingExecutor:
    """Minimal authentication executor for route-level connect tests."""

    def __init__(self, profile, host, credentials, manager=None, session=None):
        self.profile = profile
        self.host = host
        self.credentials = credentials

    def connect(self):
        return {}


class ManufacturerDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.original_state = dict(app_module.state)
        self.client = app_module.app.test_client()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.equipment_path = Path(self.temp.name) / "equipment.json"
        self._write_equipment([
            {"id": "aurora", "name": "Aurora", "profiles": ["aurora.x1"]},
            {"id": "empty", "name": "Empty", "profiles": []},
        ])
        patcher = patch.object(app_module, "EQUIPMENT_CATALOG", self.equipment_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        app_module.state.clear()
        app_module.state.update(self.original_state)

    # ---------- helpers ----------

    def _write_equipment(self, manufacturers):
        self.equipment_path.write_text(
            json.dumps({"schema_version": 1, "manufacturers": manufacturers}, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _profile(profile_id="aurora.x1", manufacturer_id="aurora", enabled=True,
                 vendor="Aurora X1", weight=80):
        profile = copy.deepcopy(synthetic_connector())
        profile["id"] = profile_id
        profile["display_name"] = f"{profile_id} (synthetic)"
        profile["enabled"] = enabled
        profile["model"] = "X1"
        if manufacturer_id is None:
            profile.pop("manufacturer_id", None)
        else:
            profile["manufacturer_id"] = manufacturer_id
        rule = profile["identification"]["rules"][0]
        rule["value"] = vendor
        rule["weight"] = weight
        return profile

    def _discover(self, manager, manufacturer_id=None, force=True,
                  vendor_by_profile=None):
        probed = []
        vendor_by_profile = dict(vendor_by_profile or {})

        def fake_discover(executor):
            probed.append(executor.profile_id)
            vendor = vendor_by_profile.get(executor.profile_id, "Aurora X1")
            return {
                "observations": {"challenge": {"Vendor": vendor}},
                "errors": [],
            }

        with patch.object(app_module, "detect_gateway", return_value=DETECTION), \
             patch.object(app_module.ConnectorExecutor, "discover", new=fake_discover), \
             patch.object(
                 app_module.ConnectorExecutor, "connect",
                 side_effect=AssertionError("Discovery must not authenticate."),
             ), \
             patch.dict(app_module.state, {
                 "manager": manager,
                 "discovery": None,
                 "discovery_manufacturer_id": "",
                 "gateway": DETECTION,
             }):
            result = app_module.run_discovery(
                force=force, manufacturer_id=manufacturer_id
            )
        return result, probed

    def _real_discovery(self, manufacturer_id=None, force=False,
                        observations=None, observations_by_profile=None):
        """Run real discovery with mocked probes and no cache resets."""

        probed = []
        default_observations = dict(observations or {})
        by_profile = {
            key: dict(value)
            for key, value in (observations_by_profile or {}).items()
        }

        def fake_discover(executor):
            probed.append(executor.profile_id)
            payload = by_profile.get(executor.profile_id, default_observations)
            return {"observations": dict(payload), "errors": []}

        with patch.object(app_module, "detect_gateway", return_value=DETECTION), \
             patch.object(app_module.ConnectorExecutor, "discover", new=fake_discover), \
             patch.object(
                 app_module.ConnectorExecutor, "connect",
                 side_effect=AssertionError("Discovery must not authenticate."),
             ):
            result = app_module.run_discovery(
                force=force, manufacturer_id=manufacturer_id
            )
        return result, probed

    def _post_connect(self, payload):
        app_module.state["manager"] = ConnectorManager()
        app_module.state["discovery"] = None
        with patch.object(
            app_module, "run_discovery", return_value=DISCOVERY
        ) as discovery, patch.object(
            app_module, "ConnectorExecutor", RecordingExecutor
        ):
            response = self.client.post("/api/connect", json=payload)
        return response, discovery

    def _assert_discovery_shape(self, payload):
        self.assertEqual(set(payload), DISCOVERY_KEYS)
        self.assertEqual(payload["physical_device_check_required"], True)
        self.assertEqual(payload["discovery_errors"], [])
        for row in payload["profiles"]:
            self.assertEqual(set(row), PROFILE_ROW_KEYS)
            self.assertEqual(row["device_compatibility"], "not_evaluated")
        self.assertIn("requires_user_confirmation", payload["credential_suggestion"])
        self.assertNotIn("password", payload["credential_suggestion"])

    # ---------- focused M2 cases ----------

    def test_01_omitted_manufacturer_probes_every_enabled_profile(self):
        profiles = [
            self._profile("aurora.x1", weight=80),
            self._profile("aurora.x2", weight=90),
        ]
        with manager_for_catalog({"schema_version": 1, "connectors": profiles}) as manager:
            result, probed = self._discover(manager)

        self.assertEqual(probed, ["aurora.x1", "aurora.x2"])
        self.assertEqual(result["selected_profile"], "aurora.x2")
        self.assertEqual(result["identification_confidence"], "exact")
        self.assertTrue(result["evidence"])
        self._assert_discovery_shape(result)

    def test_02_known_manufacturer_probes_only_associated_enabled_profile(self):
        profiles = [
            self._profile("aurora.x1", weight=80),
            self._profile("aurora.x2", weight=90),
        ]
        with manager_for_catalog({"schema_version": 1, "connectors": profiles}) as manager:
            result, probed = self._discover(manager, manufacturer_id="aurora")

        self.assertEqual(probed, ["aurora.x1"])
        self.assertEqual(result["selected_profile"], "aurora.x1")
        self.assertEqual(result["identification_confidence"], "exact")
        self.assertEqual(
            [row["id"] for row in result["profiles"]], ["aurora.x1"]
        )
        self._assert_discovery_shape(result)

    def test_03_unknown_profile_reference_does_not_fall_back_to_all_profiles(self):
        self._write_equipment([{
            "id": "aurora", "name": "Aurora", "profiles": ["missing.profile"],
        }])
        profiles = [self._profile("aurora.x1")]
        with manager_for_catalog({"schema_version": 1, "connectors": profiles}) as manager:
            result, probed = self._discover(manager, manufacturer_id="aurora")

        self.assertEqual(probed, [])
        self.assertIsNone(result["selected_profile"])
        self.assertIn("unsupported manufacturer", result["evidence"])
        self.assertEqual(result["identification_confidence"], "unknown")
        self._assert_discovery_shape(result)

    def test_04_empty_manufacturer_association_fails_closed(self):
        profiles = [self._profile("aurora.x1")]
        with manager_for_catalog({"schema_version": 1, "connectors": profiles}) as manager:
            result, probed = self._discover(manager, manufacturer_id="empty")

        self.assertEqual(probed, [])
        self.assertIsNone(result["selected_profile"])
        self.assertIn("unsupported manufacturer", result["evidence"])
        self._assert_discovery_shape(result)

    def test_05_disabled_associated_profile_is_not_probed(self):
        profiles = [self._profile("aurora.x1", enabled=False)]
        with manager_for_catalog({"schema_version": 1, "connectors": profiles}) as manager:
            result, probed = self._discover(manager, manufacturer_id="aurora")

        self.assertEqual(probed, [])
        self.assertIsNone(result["selected_profile"])
        self.assertIn("unsupported manufacturer", result["evidence"])

    def test_06_profile_metadata_mismatch_is_not_a_fallback_candidate(self):
        profiles = [self._profile("aurora.x1", manufacturer_id="other-vendor")]
        with manager_for_catalog({"schema_version": 1, "connectors": profiles}) as manager:
            result, probed = self._discover(manager, manufacturer_id="aurora")

        self.assertEqual(probed, [])
        self.assertIsNone(result["selected_profile"])
        self.assertIn("unsupported manufacturer", result["evidence"])

    def test_07_unknown_manufacturer_is_rejected_before_discovery(self):
        payload = {
            "gateway": "192.168.1.1",
            "username": "tester",
            "password": "unit-test-password",
            "manufacturer_id": "not-in-catalog",
        }
        response, discovery = self._post_connect(payload)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])
        self.assertEqual(response.get_json()["message"], "Unknown manufacturer.")
        discovery.assert_not_called()
        self.assertFalse(app_module.state.get("connected", False))

    def test_08_invalid_equipment_catalog_does_not_fall_back_to_all_profiles(self):
        payload = {
            "gateway": "192.168.1.1",
            "username": "tester",
            "password": "unit-test-password",
            "manufacturer_id": "aurora",
        }
        missing = Path(self.temp.name) / "absent-equipment.json"
        with patch.object(app_module, "EQUIPMENT_CATALOG", missing), \
             patch.object(app_module, "run_discovery") as discovery:
            response = self.client.post("/api/connect", json=payload)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["message"], "Manufacturer catalog is not configured."
        )
        discovery.assert_not_called()

    def test_09_connect_forwards_selector_and_ignores_client_profile_id(self):
        payload = {
            "gateway": "192.168.1.1",
            "username": "tester",
            "password": "unit-test-password",
            "manufacturer_id": "aurora",
            "profile_id": "attacker-supplied.profile",
        }
        response, discovery = self._post_connect(payload)

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["manufacturer_id"], "aurora")
        self.assertEqual(app_module.state["manufacturer_id"], "aurora")
        self.assertEqual(
            app_module.state["credentials"]["profile_id"], "fiberhome.hg6145f1"
        )
        discovery.assert_called_once_with(
            force=True, host="192.168.1.1", manufacturer_id="aurora"
        )

    def test_10_legacy_alias_and_omitted_selector_are_preserved(self):
        base = {
            "gateway": "192.168.1.1",
            "username": "tester",
            "password": "unit-test-password",
        }
        alias_payload = dict(base, manufacturer="aurora")
        alias_response, alias_discovery = self._post_connect(alias_payload)
        self.assertEqual(alias_response.status_code, 200)
        self.assertEqual(alias_response.get_json()["manufacturer_id"], "aurora")
        self.assertEqual(
            alias_discovery.call_args.kwargs["manufacturer_id"], "aurora"
        )

        self._post_connect(base)
        legacy_response, legacy_discovery = self._post_connect(base)
        self.assertEqual(legacy_response.status_code, 200)
        self.assertEqual(legacy_response.get_json()["manufacturer_id"], "")
        self.assertEqual(legacy_discovery.call_args.kwargs["manufacturer_id"], "")

    def test_11_manufacturer_change_invalidates_discovery_cache(self):
        profiles = [
            self._profile("aurora.x1", weight=80),
            self._profile("aurora.x2", weight=90),
        ]
        probed = []
        vendor_by_profile = {}

        def fake_discover(executor):
            probed.append(executor.profile_id)
            return {
                "observations": {
                    "challenge": {
                        "Vendor": vendor_by_profile.get(
                            executor.profile_id, "Aurora X1"
                        )
                    }
                },
                "errors": [],
            }

        with manager_for_catalog({"schema_version": 1, "connectors": profiles}) as manager:
            with patch.object(app_module, "detect_gateway", return_value=DETECTION), \
                 patch.object(app_module.ConnectorExecutor, "discover", new=fake_discover), \
                 patch.object(
                     app_module.ConnectorExecutor, "connect",
                     side_effect=AssertionError("Discovery must not authenticate."),
                 ), patch.dict(app_module.state, {
                     "manager": manager,
                     "discovery": None,
                     "discovery_manufacturer_id": "",
                     "gateway": DETECTION,
                 }):
                legacy = app_module.run_discovery(force=True)
                self.assertEqual(probed, ["aurora.x1", "aurora.x2"])
                self.assertEqual(legacy["selected_profile"], "aurora.x2")

                filtered = app_module.run_discovery(
                    force=False, manufacturer_id="aurora"
                )
                self.assertEqual(
                    probed, ["aurora.x1", "aurora.x2", "aurora.x1"]
                )
                self.assertEqual(filtered["selected_profile"], "aurora.x1")
                self.assertIsNot(app_module.state["discovery"], legacy)

    def test_12_filtered_discovery_routes_keep_frozen_shape_and_hide_metadata(self):
        profiles = [self._profile("aurora.x1")]
        with manager_for_catalog({"schema_version": 1, "connectors": profiles}) as manager:
            with patch.dict(app_module.state, {
                "manager": manager, "discovery": None, "gateway": DETECTION,
            }), patch.object(
                app_module, "detect_gateway", return_value=DETECTION
            ), patch.object(
                app_module.ConnectorExecutor, "discover", return_value={
                    "observations": {"challenge": {"Vendor": "Aurora X1"}},
                    "errors": [],
                }
            ), patch.object(
                app_module.ConnectorExecutor, "connect",
                side_effect=AssertionError("Discovery must not authenticate."),
            ):
                discovery = self.client.get(
                    "/api/discovery?manufacturer_id=aurora"
                ).get_json()
                login_defaults = self.client.get(
                    "/api/login-defaults?manufacturer_id=aurora"
                ).get_json()

        self._assert_discovery_shape(discovery)
        self.assertEqual(set(login_defaults), LOGIN_DEFAULTS_KEYS)
        for hidden in (
            "profile", "profile_id", "selected_profile", "evidence",
            "manufacturer", "confidence", "discovery_errors", "profiles",
            "model", "firmware",
        ):
            self.assertNotIn(hidden, login_defaults)
        for hidden in ("profile_id", "manufacturer_id", "model", "firmware"):
            self.assertNotIn(hidden, json.dumps(discovery))

    def test_13_equipment_reference_validation_reports_bad_links(self):
        profile = self._profile("aurora.x1", manufacturer_id="aurora")
        with tempfile.TemporaryDirectory() as temp:
            catalog_path = Path(temp) / "connectors.json"
            equipment_path = Path(temp) / "equipment.json"
            catalog_path.write_text(
                json.dumps({"schema_version": 1, "connectors": [profile]}),
                encoding="utf-8",
            )
            equipment_path.write_text(json.dumps({
                "schema_version": 1,
                "manufacturers": [
                    {
                        "id": "aurora",
                        "name": "Aurora",
                        "profiles": ["aurora.x1", "aurora.x1", "missing.profile"],
                    },
                    {
                        "id": "other",
                        "name": "Other",
                        "profiles": ["aurora.x1"],
                    },
                ],
            }), encoding="utf-8")
            manager = ConnectorManager(
                catalog_path, DEFAULT_CREDENTIALS, DEFAULT_SCHEMA, equipment_path
            )
            report = manager.validate()
            codes = {issue.code for issue in report.issues}

        self.assertIn("duplicate_profile_reference", codes)
        self.assertIn("unknown_profile_reference", codes)
        self.assertIn("profile_manufacturer_mismatch", codes)
        self.assertFalse(report.structural_valid)

    def test_14_shipped_metadata_is_optional_declarative_and_linked(self):
        manager = ConnectorManager()
        report = manager.validate()
        profile = manager.get("fiberhome.hg6145f1")
        equipment = json.loads(
            (ROOT / "config" / "equipment.json").read_text(encoding="utf-8")
        )
        fiberhome = next(
            item for item in equipment["manufacturers"]
            if item["id"] == "fiberhome"
        )

        self.assertTrue(report.structural_valid)
        self.assertEqual(report.errors, [])
        self.assertEqual(profile["manufacturer_id"], "fiberhome")
        self.assertEqual(profile["model"], "HG6145F1")
        self.assertNotIn("firmware", profile)
        self.assertEqual(fiberhome["profiles"], ["fiberhome.hg6145f1"])
        for item in equipment["manufacturers"]:
            if item["id"] != "fiberhome":
                self.assertEqual(item["profiles"], [])

    def test_15_cached_success_is_not_reused_after_catalog_becomes_invalid(self):
        """A cached FiberHome result must not survive catalog corruption."""

        self._write_equipment([{
            "id": "fiberhome",
            "name": "FiberHome",
            "profiles": ["fiberhome.hg6145f1"],
        }])
        observations = {"device_name": {"ModelName": "HG6145F1"}}
        manager = ConnectorManager()
        with patch.dict(app_module.state, {
            "manager": manager,
            "discovery": None,
            "discovery_manufacturer_id": "",
            "discovery_catalog_fingerprint": None,
            "gateway": DETECTION,
        }):
            cached, first_probed = self._real_discovery(
                manufacturer_id="fiberhome", force=True, observations=observations
            )
            self.assertEqual(cached["selected_profile"], "fiberhome.hg6145f1")
            self.assertEqual(first_probed, ["fiberhome.hg6145f1"])
            cached_fingerprint = app_module.state["discovery_catalog_fingerprint"]
            self.assertTrue(cached_fingerprint.startswith("sha256:"))

            self.equipment_path.write_text("{not-json", encoding="utf-8")
            fresh, second_probed = self._real_discovery(
                manufacturer_id="fiberhome", force=False, observations=observations
            )
            fresh_fingerprint = app_module.state["discovery_catalog_fingerprint"]

        self.assertIsNot(fresh, cached)
        self.assertIsNone(fresh["selected_profile"])
        self.assertIn("unsupported manufacturer", fresh["evidence"])
        self.assertEqual(second_probed, [])
        self.assertTrue(fresh_fingerprint.startswith("invalid:"))
        self.assertNotEqual(fresh_fingerprint, cached_fingerprint)

    def test_16_changed_association_set_invalidates_cache_and_controls_candidates(self):
        """A changed equipment link must replace, not reuse, cached discovery."""

        profiles = [
            self._profile("aurora.x1", vendor="Aurora X1", weight=80),
            self._profile("aurora.x2", vendor="Aurora X2", weight=90),
        ]
        observations_by_profile = {
            "aurora.x1": {"challenge": {"Vendor": "Aurora X1"}},
            "aurora.x2": {"challenge": {"Vendor": "Aurora X2"}},
        }
        self._write_equipment([{
            "id": "aurora", "name": "Aurora", "profiles": ["aurora.x1"],
        }])
        with manager_for_catalog({
            "schema_version": 1, "connectors": profiles,
        }) as manager, patch.dict(app_module.state, {
            "manager": manager,
            "discovery": None,
            "discovery_manufacturer_id": "",
            "discovery_catalog_fingerprint": None,
            "gateway": DETECTION,
        }):
            first, first_probed = self._real_discovery(
                manufacturer_id="aurora", force=True,
                observations_by_profile=observations_by_profile,
            )
            self.assertEqual(first_probed, ["aurora.x1"])
            self.assertEqual(first["selected_profile"], "aurora.x1")
            first_fingerprint = app_module.state["discovery_catalog_fingerprint"]

            self._write_equipment([{
                "id": "aurora", "name": "Aurora", "profiles": ["aurora.x2"],
            }])
            second, second_probed = self._real_discovery(
                manufacturer_id="aurora", force=False,
                observations_by_profile=observations_by_profile,
            )
            second_fingerprint = app_module.state["discovery_catalog_fingerprint"]
            cached_state = app_module.state["discovery"]

        self.assertIsNot(second, first)
        self.assertIs(cached_state, second)
        self.assertNotEqual(second_fingerprint, first_fingerprint)
        self.assertEqual(second_probed, ["aurora.x2"])
        self.assertEqual(second["selected_profile"], "aurora.x2")
        self.assertEqual(
            [row["id"] for row in second["profiles"]], ["aurora.x2"]
        )

    def test_17_equipment_profile_order_is_authoritative(self):
        """Candidate probe order follows the equipment profiles array."""

        profiles = [
            self._profile("aurora.x1", vendor="Aurora X1", weight=80),
            self._profile("aurora.x2", vendor="Aurora X2", weight=90),
        ]
        observations_by_profile = {
            "aurora.x1": {"challenge": {"Vendor": "Aurora X1"}},
            "aurora.x2": {"challenge": {"Vendor": "Aurora X2"}},
        }
        self._write_equipment([{
            "id": "aurora",
            "name": "Aurora",
            "profiles": ["aurora.x2", "aurora.x1"],
        }])
        with manager_for_catalog({
            "schema_version": 1, "connectors": profiles,
        }) as manager, patch.dict(app_module.state, {
            "manager": manager,
            "discovery": None,
            "discovery_manufacturer_id": "",
            "discovery_catalog_fingerprint": None,
            "gateway": DETECTION,
        }):
            result, probed = self._real_discovery(
                manufacturer_id="aurora", force=True,
                observations_by_profile=observations_by_profile,
            )

        self.assertEqual(probed, ["aurora.x2", "aurora.x1"])
        self.assertEqual(
            [row["id"] for row in result["profiles"]], ["aurora.x2", "aurora.x1"]
        )
        self.assertEqual(result["selected_profile"], "aurora.x2")

    def test_18_omitted_manufacturer_still_probes_all_profiles_after_catalog_change(self):
        """Legacy discovery remains all-profile and is still cache-revalidated."""

        profiles = [
            self._profile("aurora.x1", vendor="Aurora X1", weight=80),
            self._profile("aurora.x2", vendor="Aurora X2", weight=90),
        ]
        observations_by_profile = {
            "aurora.x1": {"challenge": {"Vendor": "Aurora X1"}},
            "aurora.x2": {"challenge": {"Vendor": "Aurora X2"}},
        }
        self._write_equipment([{
            "id": "aurora", "name": "Aurora", "profiles": ["aurora.x1"],
        }])
        with manager_for_catalog({
            "schema_version": 1, "connectors": profiles,
        }) as manager, patch.dict(app_module.state, {
            "manager": manager,
            "discovery": None,
            "discovery_manufacturer_id": "",
            "discovery_catalog_fingerprint": None,
            "gateway": DETECTION,
        }):
            first, first_probed = self._real_discovery(
                force=True, observations_by_profile=observations_by_profile
            )
            first_fingerprint = app_module.state["discovery_catalog_fingerprint"]

            self.equipment_path.write_text("{not-json", encoding="utf-8")
            second, second_probed = self._real_discovery(
                force=False, observations_by_profile=observations_by_profile
            )
            second_fingerprint = app_module.state["discovery_catalog_fingerprint"]

        self.assertEqual(first_probed, ["aurora.x1", "aurora.x2"])
        self.assertEqual(second_probed, ["aurora.x1", "aurora.x2"])
        self.assertEqual(first["selected_profile"], "aurora.x2")
        self.assertEqual(second["selected_profile"], "aurora.x2")
        self.assertNotEqual(second_fingerprint, first_fingerprint)
        self.assertTrue(second_fingerprint.startswith("invalid:"))


if __name__ == "__main__":
    unittest.main()
