import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps/portal"))
from bridge.avihub_client import Panel, SessionExpired, MFA


HOME = b'<nav class="navbar-body"><form action="/users/sign_out"></form></nav>'


class PanelTests(unittest.TestCase):
    def panel(self, *responses):
        panel = Panel("https://panel.example")
        panel.request = Mock(side_effect=responses)
        return panel

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
