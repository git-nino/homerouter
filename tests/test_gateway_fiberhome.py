import unittest
from types import SimpleNamespace
from unittest.mock import patch

from gateway import RouterClient, parse_voice_status


class ParseVoiceStatusTests(unittest.TestCase):
    def test_observed_directory_number_maps_to_idle_state(self):
        payload = {
            "voice_status": {
                "voice_port_num": 1,
                "ServerType": "1",
                "RegAndCallStatus1": "2",
                "DirectoryNumber1": "213000000000",
                "AuthUserName1": "213000000000",
                "URI1": "",
            }
        }
        result = parse_voice_status(payload)
        self.assertEqual(result["phone_number"], "213000000000")
        self.assertEqual(result["registration_code"], "2")
        self.assertEqual(result["registration_state"], "IDLE")
        self.assertEqual(result["availability"], "available")
        self.assertEqual(result["source"], "get_voice_status")

    def test_server_type_two_prefers_uri(self):
        payload = {
            "voice_status": {
                "voice_port_num": 1,
                "ServerType": "2",
                "URI1": "+213555000111",
                "DirectoryNumber1": "ignored",
            }
        }
        self.assertEqual(
            parse_voice_status(payload)["phone_number"],
            "+213555000111",
        )

    def test_auth_username_is_used_when_directory_number_is_empty(self):
        payload = {
            "voice_status": {
                "voice_port_num": 1,
                "ServerType": "1",
                "AuthUserName1": "213555000222@provider.example",
            }
        }
        self.assertEqual(
            parse_voice_status(payload)["phone_number"],
            "213555000222",
        )

    def test_no_voice_port_is_unsupported_not_empty_available(self):
        result = parse_voice_status({"voice_status": {"voice_port_num": 0}})
        self.assertEqual(result["availability"], "unsupported")
        self.assertEqual(result["phone_number"], "")

    def test_missing_payload_is_unsupported(self):
        self.assertEqual(parse_voice_status(None)["availability"], "unsupported")


class RouterLogoutTests(unittest.TestCase):
    def test_logout_posts_do_logout_and_clears_local_session(self):
        client = RouterClient("192.168.1.1")
        client.logged_in = True
        client.sessionid = "fixture-session"

        with patch.object(client, "get", return_value={"login_user": "1"}), \
             patch.object(client, "_post", return_value=SimpleNamespace(status_code=200)) as post:
            ok, _ = client.logout()

        self.assertTrue(ok)
        post.assert_called_once_with("/cgi-bin/ajax", {
            "ajaxmethod": "do_logout",
            "sessionid": "fixture-session",
            "username": "common",
        })
        self.assertFalse(client.logged_in)
        self.assertIsNone(client.sessionid)

    def test_logout_maps_admin_and_superadmin_roles(self):
        for role, expected in (("2", "admin"), ("4", "superadmin")):
            with self.subTest(role=role):
                client = RouterClient("192.168.1.1")
                client.logged_in = True
                client.sessionid = "fixture-session"
                with patch.object(client, "get", return_value={"login_user": role}), \
                     patch.object(client, "_post", return_value=SimpleNamespace(status_code=200)) as post:
                    client.logout()
                self.assertEqual(post.call_args.args[1]["username"], expected)

    def test_logout_failure_still_clears_local_session(self):
        client = RouterClient("192.168.1.1")
        client.logged_in = True
        client.sessionid = "fixture-session"

        with patch.object(client, "get", return_value={"login_user": "1"}), \
             patch.object(client, "_post", return_value=None):
            ok, _ = client.logout()

        self.assertFalse(ok)
        self.assertFalse(client.logged_in)
        self.assertIsNone(client.sessionid)


if __name__ == "__main__":
    unittest.main()
