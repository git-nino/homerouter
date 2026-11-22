import json
import unittest

from models.enums import Availability, CompatibilityState, Domain
from models.universal import METRICS, ConnectedDevice, UniversalModel


class MetricDefinitionTests(unittest.TestCase):
    def test_all_seven_domains_have_stable_metric_ids(self):
        expected = {
            Domain.DEVICE_IDENTITY,
            Domain.WAN,
            Domain.OPTICAL,
            Domain.VOIP,
            Domain.SYSTEM,
            Domain.CONNECTED_DEVICES,
            Domain.SPEED_TEST,
        }
        self.assertEqual(set(Domain), expected)
        self.assertTrue(METRICS)
        for metric_id, spec in METRICS.items():
            self.assertEqual(spec.id, metric_id)
            self.assertIn(spec.domain, expected)

    def test_metric_ids_are_unique(self):
        self.assertEqual(len(METRICS), len(set(METRICS)))


class UniversalModelTests(unittest.TestCase):
    def setUp(self):
        self.model = UniversalModel(gateway="192.168.1.1", profile_id="fiberhome.hg6145f1")

    def test_missing_metric_is_explicitly_not_loaded(self):
        metric = self.model.get_metric(Domain.OPTICAL, "optical.rx_power_dbm")
        self.assertEqual(metric.availability, Availability.NOT_LOADED)
        self.assertIsNone(metric.value)
        self.assertEqual(metric.unit, "dBm")

    def test_available_zero_is_allowed_and_source_is_preserved(self):
        metric = self.model.set_metric(
            Domain.SYSTEM,
            "system.cpu_percent",
            value=0,
            source_value="0",
            source="get_base_info.cpu_usage",
        )
        self.assertEqual(metric.value, 0)
        self.assertEqual(metric.source_value, "0")
        self.assertEqual(metric.availability, Availability.AVAILABLE)
        self.assertIsNotNone(metric.observed_at)

    def test_available_without_value_is_rejected(self):
        with self.assertRaises(ValueError):
            self.model.set_metric(
                Domain.WAN,
                "wan.public_ip",
                availability=Availability.AVAILABLE,
            )

    def test_invalid_value_never_becomes_a_normalized_value(self):
        metric = self.model.set_metric(
            Domain.OPTICAL,
            "optical.rx_power_dbm",
            availability=Availability.INVALID,
            source_value="not-a-number",
            error="expected numeric dBm",
        )
        self.assertIsNone(metric.value)
        self.assertEqual(metric.source_value, "not-a-number")
        self.assertEqual(metric.availability, Availability.INVALID)
        self.assertEqual(metric.error, "expected numeric dBm")

    def test_stale_metric_keeps_its_last_known_value(self):
        metric = self.model.set_metric(
            Domain.WAN,
            "wan.public_ip",
            value="192.0.2.10",
            availability=Availability.STALE,
            source_value="192.0.2.10",
        )
        self.assertEqual(metric.value, "192.0.2.10")
        self.assertEqual(metric.availability, Availability.STALE)

    def test_wrong_domain_for_metric_is_rejected(self):
        with self.assertRaises(KeyError):
            self.model.set_metric(Domain.VOIP, "wan.public_ip", value="192.0.2.10")

    def test_device_count_is_not_fabricated_when_not_loaded(self):
        self.model.set_devices(None, loaded=False, error="table not loaded")
        metric = self.model.get_metric(Domain.CONNECTED_DEVICES, "devices.count")
        self.assertEqual(metric.availability, Availability.ERROR)
        self.assertIsNone(metric.value)
        self.assertEqual(self.model.devices, [])

    def test_device_count_for_loaded_empty_list_is_zero(self):
        self.model.set_devices([], loaded=True, source="get_lan_status")
        metric = self.model.get_metric(Domain.CONNECTED_DEVICES, "devices.count")
        self.assertEqual(metric.availability, Availability.AVAILABLE)
        self.assertEqual(metric.value, 0)
        self.assertTrue(self.model.devices_loaded)

    def test_device_count_is_derived_from_loaded_devices(self):
        devices = [
            ConnectedDevice(
                id="lan.1",
                hostname="desktop",
                ip_address="192.168.1.9",
                mac_address="00:11:22:33:44:55",
                raw={"HostName": "desktop"},
            )
        ]
        self.model.set_devices(devices, loaded=True)
        metric = self.model.get_metric(Domain.CONNECTED_DEVICES, "devices.count")
        self.assertEqual(metric.value, 1)
        self.assertEqual(metric.source_value, 1)

    def test_default_compatibility_is_identification_incomplete(self):
        self.assertEqual(
            self.model.compatibility.state,
            CompatibilityState.IDENTIFICATION_INCOMPLETE,
        )

    def test_to_dict_is_json_serializable_and_contains_all_domains(self):
        self.model.set_metric(
            Domain.VOIP,
            "voip.phone_number",
            value="+213555123456",
            availability=Availability.AVAILABLE,
            source_value="+213555123456",
        )
        payload = json.dumps(self.model.to_dict())
        self.assertIn('"device_identity"', payload)
        self.assertIn('"voip"', payload)
        self.assertIn('"call_action"', payload)


class VoipCallActionTests(unittest.TestCase):
    def test_call_action_disabled_when_phone_not_loaded(self):
        model = UniversalModel()
        action = model.primary_call_action()
        self.assertFalse(action.enabled)
        self.assertIsNone(action.dial_uri)
        self.assertIn("not been loaded", action.reason)

    def test_call_action_enabled_from_valid_phone_number(self):
        model = UniversalModel()
        line = model.set_primary_voip_line(
            phone_number="+213 555 12 34 56",
            phone_availability=Availability.AVAILABLE,
            registration_state="registered",
            registration_availability=Availability.AVAILABLE,
            line_status="idle",
            line_status_availability=Availability.AVAILABLE,
        )
        self.assertTrue(line.call_action.enabled)
        self.assertEqual(line.call_action.dial_uri, "tel:+213555123456")
        self.assertEqual(model.primary_call_action(), line.call_action)

    def test_call_action_disabled_for_invalid_phone_number(self):
        model = UniversalModel()
        line = model.set_primary_voip_line(
            phone_number="abc",
            phone_availability=Availability.INVALID,
        )
        self.assertFalse(line.call_action.enabled)
        self.assertIsNone(line.call_action.dial_uri)

    def test_call_action_disabled_for_stale_phone_number(self):
        model = UniversalModel()
        line = model.set_primary_voip_line(
            phone_number="+213555123456",
            phone_availability=Availability.STALE,
        )
        self.assertFalse(line.call_action.enabled)
        self.assertIn("stale", line.call_action.reason.lower())


if __name__ == "__main__":
    unittest.main()
