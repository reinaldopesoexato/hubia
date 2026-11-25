import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "portal"))
from agent_runtime import ConnectorError, https_url, query_connector, search_knowledge


class RuntimeTest(unittest.TestCase):
    def test_knowledge_matches_accented_and_returns_original_text(self):
        body = "Expedição atende até 17h.\n\nFinanceiro atende até 18h."
        self.assertEqual(search_knowledge(body, "expedicao"), "Expedição atende até 17h.")
        self.assertIn("Nenhum trecho", search_knowledge(body, "estoque"))

    def test_urls(self):
        self.assertEqual(https_url("https://example.org/abrir"), "https://example.org/abrir")
        for url in ("javascript:alert(1)", "http://a", "https:///path", "https://test@a",
                    "https://a#token", "https://a:65536", "https://a\\@b", "https://a/ x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                https_url(url)

    def submit(self, config, result, history=()):
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(result).encode())
        with patch("agent_runtime.request.build_opener", return_value=opener):
            answer = query_connector(config, {"principal_id":"conta"}, "saldo?", "run", history)
        req = opener.open.call_args.args[0]
        return answer, json.loads(req.data), req.headers

    def test_query_contract(self):
        answer, body, headers = self.submit({"url":"https://example.org/q","token":"teste"},{"answer":"saldo"})
        self.assertEqual(answer,"saldo")
        self.assertEqual(body,{"principal_id":"conta","query":"saldo?","request_id":"run"})
        self.assertEqual(headers["Authorization"],"Bearer teste")

    def test_model_contract(self):
        answer, body, _ = self.submit({"type":"chat_completions","model":"modelo","instructions":"português",
            "url":"https://example.org/chat"},{"choices":[{"message":{"content":"ok"}}]},[("antes","resposta")])
        self.assertEqual(answer,"ok")
        self.assertEqual([m["role"] for m in body["messages"]],["system","user","assistant","user"])
        self.assertNotIn("principal_id",body)
        self.assertFalse(body["store"])

    def test_invalid_or_excessive_response(self):
        for response in ({"erro":"inválido"},{"answer":" "},{"answer":"a"*16001}):
            with self.subTest(response_type=next(iter(response))), self.assertRaises(ConnectorError):
                self.submit({"url":"https://example.org/q"},response)


if __name__ == "__main__":
    unittest.main()
