import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps/portal"))
from bridge.avihub_api import AccessDenied, select_account


class AccountContextTests(unittest.TestCase):
    accounts = [
        {"id": "conta-1", "nome": "Granja Norte", "email": "norte@example.org"},
        {"id": "conta-2", "nome": "Granja Sul", "email": "sul@example.org"},
    ]

    def test_one_ready_session_is_implicit(self):
        self.assertEqual(select_account(self.accounts[:1], None), self.accounts[0])

    def test_context_accepts_exact_alias_email_or_id(self):
        for selection in ("granja sul", " SUL@example.org ", "conta-2"):
            with self.subTest(selection=selection):
                self.assertEqual(select_account(self.accounts, selection), self.accounts[1])

    def test_ambiguous_context_requests_a_choice(self):
        prompt = select_account(self.accounts, None)
        self.assertEqual(prompt["estado"], "escolher_conta")
        self.assertEqual(prompt["contas"], self.accounts)

    def test_duplicate_names_and_multiple_sessions_with_same_email(self):
        duplicates = [self.accounts[0], dict(self.accounts[0], id="conta-3")]
        for value in ("Granja Norte", "norte@example.org"):
            self.assertEqual(select_account(duplicates, value)["contas"], duplicates)
        self.assertEqual(select_account(duplicates, "conta-3"), duplicates[1])

    def test_no_partial_match_or_fallback_after_disconnect(self):
        for value in ("norte", "sul@example.org", "conta-2"):
            with self.assertRaises(AccessDenied):
                select_account(self.accounts[:1], value)
        with self.assertRaises(AccessDenied):
            select_account([], None)

    def test_existing_login_without_recorded_email_remains_selectable(self):
        old = {"id": "conta-17", "nome": "Conta vinculada", "email": ""}
        self.assertEqual(select_account([old, *self.accounts], "conta-17"), old)
