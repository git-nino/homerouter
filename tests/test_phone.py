import unittest

from models.phone import (
    build_call_action,
    build_tel_uri,
    normalize_phone_number,
)


class NormalizePhoneNumberTests(unittest.TestCase):
    def test_international_number_is_kept_in_e164_shape(self):
        self.assertEqual(normalize_phone_number("+1 (555) 123-4567"), "+15551234567")

    def test_local_number_is_digits_only(self):
        self.assertEqual(normalize_phone_number("0555 123 456"), "0555123456")

    def test_invalid_inputs_return_none(self):
        for value in (None, "", "123", "abcdef", "+1-800-FLOWERS", "++33123456789", "12+34567890"):
            with self.subTest(value=value):
                self.assertIsNone(normalize_phone_number(value))

    def test_too_many_digits_is_rejected(self):
        self.assertIsNone(normalize_phone_number("1234567890123456"))


class DialUriTests(unittest.TestCase):
    def test_tel_uri_is_built_from_valid_number(self):
        self.assertEqual(build_tel_uri("+33 1 23 45 67 89"), "tel:+33123456789")
        self.assertEqual(build_tel_uri("0555123456"), "tel:0555123456")

    def test_tel_uri_contains_only_safe_characters(self):
        uri = build_tel_uri("+1 (555) 123-4567")
        self.assertIsNotNone(uri)
        self.assertTrue(uri.startswith("tel:+"))
        self.assertTrue(uri[5:].isdigit())

    def test_invalid_number_never_produces_uri(self):
        self.assertIsNone(build_tel_uri("not-a-number"))


class CallActionTests(unittest.TestCase):
    def test_valid_number_enables_action(self):
        action = build_call_action("+1 (555) 123-4567")
        self.assertTrue(action.enabled)
        self.assertEqual(action.dial_uri, "tel:+15551234567")
        self.assertIsNone(action.reason)

    def test_missing_number_is_disabled_with_reason(self):
        action = build_call_action(None, unavailable_reason="not loaded")
        self.assertFalse(action.enabled)
        self.assertIsNone(action.dial_uri)
        self.assertEqual(action.reason, "not loaded")

    def test_invalid_number_is_disabled(self):
        action = build_call_action("abc")
        self.assertFalse(action.enabled)
        self.assertIsNone(action.dial_uri)
        self.assertIn("validation", action.reason.lower())


if __name__ == "__main__":
    unittest.main()
