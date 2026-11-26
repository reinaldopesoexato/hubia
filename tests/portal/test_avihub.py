import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, call

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps/portal"))
from bridge.avihub_client import Panel, SessionExpired, MFA


HOME = b'<nav class="navbar-body"><form action="/users/sign_out"></form></nav>'


class PanelTests(unittest.TestCase):
    def panel(self, *responses):
        panel = Panel("https://panel.example")
        panel.request = Mock(side_effect=responses)
        return panel

    def test_password_reset_uses_native_form_and_csrf(self):
        form = b'<form method="post" action="/users/password"><input name="authenticity_token" value="test-csrf"></form>'
        panel = self.panel((200, None, form), (303, "/users/sign_in", b""))
        panel.request_password_reset("member@example.org")
        self.assertEqual(panel.request.call_args_list, [
            call("/users/password/new"),
            call("/users/password", {"authenticity_token": "test-csrf", "user[email]": "member@example.org"},
                 referer="/users/password/new"),
        ])

    def test_reset_requires_valid_upstream_response(self):
        for status in (429, 503):
            panel = self.panel((status, None, b""))
            panel.form = Mock(return_value="csrf")
            with self.assertRaises(ValueError):
                panel.request_password_reset("member@example.org")
        panel = self.panel((200, None, b"<html></html>"))
        with self.assertRaises(ValueError):
            panel.request_password_reset("member@example.org")
        self.assertEqual(panel.request.call_count, 1)

    def test_expired_panel_session(self):
        panel = self.panel((302, "/users/sign_in", b""))
        with self.assertRaises(SessionExpired):
            panel.home()

    def test_mfa_cannot_be_treated_as_success(self):
        panel = self.panel((303, MFA, b""), (303, "/", b""), (200, "/", HOME))
        panel.form = Mock(return_value="csrf")
        self.assertFalse(panel.authenticate(email="member@example.org", password="fixture"))
        self.assertTrue(panel.authenticate(code="123456"))
        panel = self.panel((303, "/", b""), (200, "/", b"<html>login</html>"))
        panel.form = Mock(return_value="csrf")
        with self.assertRaises(ValueError):
            panel.authenticate(email="x", password="y")
