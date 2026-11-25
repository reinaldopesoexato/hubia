import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps/portal"))
from bridge import mail_protocol as mail


class MailProtocolTests(unittest.TestCase):
    def test_inbox_is_readonly_and_verifies_certificate(self):
        with patch.object(mail.imaplib, "IMAP4_SSL") as tls:
            imap = tls.return_value.__enter__.return_value
            imap.capabilities = (b"AUTH=PLAIN",)
            imap.select.return_value = ("OK", [b"4"])
            with mail.inbox("qa@example.org", "fixture") as (client, count):
                self.assertEqual(count, 4)
                self.assertIs(client, imap)
            self.assertTrue(tls.call_args.kwargs["ssl_context"].check_hostname)
            imap.select.assert_called_once_with("INBOX", readonly=True)

    def test_smtp_uses_own_address_tls_and_one_recipient(self):
        with patch.object(mail.smtplib, "SMTP_SSL") as tls:
            smtp = tls.return_value.__enter__.return_value
            params = ("owner@example.org", "fixture", "dest@example.org", "Aviso", "Olá")
            self.assertEqual(mail.submit(*params), "aceito_pelo_smtp")
            self.assertEqual(tls.call_args.args, (mail.SMTP_HOST, 465))
            self.assertTrue(tls.call_args.kwargs["context"].check_hostname)
            smtp.login.assert_called_once_with(*params[:2])
            message = smtp.send_message.call_args.args[0]
            self.assertEqual((str(message["From"]), str(message["To"]), str(message["Subject"])),
                             (params[0], params[2], params[3]))
            self.assertEqual(message.get_content().strip(), params[4])
            self.assertEqual(smtp.send_message.call_args.kwargs, {"from_addr":params[0], "to_addrs":[params[2]]})
            smtp.send_message.side_effect = mail.smtplib.SMTPDataError(550, b"rejected")
            self.assertEqual(mail.submit(*params), "recusado")
            smtp.send_message.side_effect = TimeoutError()
            self.assertEqual(mail.submit(*params), "incerto")

    def test_signed_email_has_one_signature_in_each_alternative(self):
        with patch.object(mail.smtplib, "SMTP_SSL") as tls:
            smtp = tls.return_value.__enter__.return_value
            signature = '<b>Reinaldo</b><br><a href="https://example.org/">Site</a>'
            self.assertEqual(mail.submit("a@example.org", "pw", "b@example.org", "Teste",
                                         "Linha 1\n<texto>", signature), "aceito_pelo_smtp")
            msg = smtp.send_message.call_args.args[0]
            plain, html = (part.get_content() for part in msg.iter_parts())
            self.assertEqual(plain.strip(), "Linha 1\n<texto>\n\nReinaldo\nSite (https://example.org/)")
            self.assertIn("Linha 1<br>\n&lt;texto&gt;", html)
            self.assertEqual(html.count(signature), 1)

    def test_signature_markup_roundtrip_and_validation(self):
        for raw in ('<p><b>Equipe</b><br/>P&amp;D</p>', '<div>Nome</div><ul><li>Suporte</li></ul>'):
            html, plain = mail.signature_parts(raw)
            self.assertEqual(mail.signature_parts(html), (html, plain))
        self.assertEqual(mail.signature_parts("<div><br></div>"), ("", ""))
        for raw in ('<script>123</script>', '<b onclick="run()">X</b>',
                    '<a href="javascript:alert(1)">X</a>', "x" * 6001):
            with self.assertRaises(ValueError):
                mail.signature_parts(raw)

    def test_initial_page_and_incoming_mail_race(self):
        client = MagicMock()
        client.fetch.return_value = ("OK", [b"5 (UID 90)", b"6 (UID 101)"])
        client.uid.return_value = ("OK", [(b"5 ...", b"Subject: Test\r\n\r\n")])
        items, marker = mail.page(client, 6, 100, None)
        self.assertEqual([uid for uid, _ in items], [90])
        self.assertEqual(marker, 100)
        self.assertIn("BODY.PEEK", client.uid.call_args.args[-1])

    def test_incremental_pagination(self):
        client = MagicMock()
        def dispatch(command, *args):
            if command == "search":
                self.assertEqual(args[-1], "2:1001")
                return "OK", [b"2 3 4 5 6 7 8 9 10 11 12"]
            return "OK", [(b"header", b"Subject: QA\r\n\r\n")]
        client.uid.side_effect = dispatch
        items, marker = mail.page(client, 100, 2000, 1)
        self.assertEqual((len(items), marker), (10, 11))
        client.reset_mock()
        self.assertEqual(mail.page(client, 0, 2000, 2000), ([], 2000))
        client.uid.assert_not_called()

    def test_no_cursor_advance_on_fetch_failure(self):
        client = MagicMock()
        client.uid.side_effect = [("OK",[b"11"]), ("NO",[b"unavailable"])]
        with self.assertRaises(ValueError):
            mail.page(client, 42, 11, 10)

    def test_message_excludes_attachments_and_hidden_html(self):
        body = (b"Subject: Test\r\nContent-Type: multipart/mixed; boundary=X\r\n\r\n"
                b"--X\r\nContent-Type: text/html\r\n\r\n<p>Hello</p><script>hidden</script>\r\n"
                b"--X\r\nContent-Disposition: attachment\r\nContent-Type: text/plain\r\n\r\nsecret\r\n--X--")
        client = MagicMock()
        client.uid.return_value = ("OK", [(b"1 FETCH", body)])
        message = mail.read(client, 33)
        self.assertEqual(message["texto"].strip(), "Hello")
        self.assertFalse(message["truncado"])
        self.assertIn("BODY.PEEK[]", client.uid.call_args.args[-1])
