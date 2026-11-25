import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "portal"))
from notification_channels import send_whatsapp, whatsapp_recipient


class LeadfinderTest(unittest.TestCase):
    def test_direct_recipient(self):
        self.assertEqual(whatsapp_recipient("+5511999990000"), ("5511999990000", ["5511999990000"]))
        for group in ("123-group","123@g.us"):
            address, aliases = whatsapp_recipient(group)
            self.assertEqual(address,group)
            self.assertEqual(set(aliases),{"123-group","123@g.us"})
        for invalid in (None,"","Contato","1,2","https://example.org","123-group\n456"):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                whatsapp_recipient(invalid)

    @patch("notification_channels.build_opener")
    def test_request_contract(self, build):
        response = MagicMock()
        response.__enter__.return_value.status = 202
        build.return_value.open.return_value = response
        state, result = send_whatsapp({"integration":"qa","key":"teste"}, "123-group", "Olá")
        request = build.return_value.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "teste")
        self.assertEqual(json.loads(request.data), {"mensagem":"Olá","destinatarios":["123-group"]})
        self.assertTrue(request.full_url.endswith("/enviarMensagem/qa/TEXTO"))
        self.assertEqual(state, "done")
        self.assertIn("sem confirmação",result)

    @patch("notification_channels.build_opener")
    def test_timeout_does_not_resend(self, build):
        build.return_value.open.side_effect = TimeoutError()
        state, message = send_whatsapp({"integration":"qa","key":"teste"}, "123-group", "Olá")
        self.assertEqual(state, "failed")
        self.assertIn("incerto", message)
        build.return_value.open.assert_called_once()
