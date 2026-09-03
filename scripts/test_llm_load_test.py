import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import llm_load_test as tool


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/v1/models":
            self.send_error(404)
            return
        self.send_json({"data": [{"id": "model-b"}, {"id": "model-a"}]})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        prompt = payload["messages"][0]["content"]
        if prompt == "RATE":
            body = b'{"error":"limited"}'
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "1")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if payload.get("stream"):
            chunks = [
                {"choices": [{"delta": {"content": "O"}}]},
                {"choices": [{"delta": {"content": "K"}}]},
                {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}},
            ]
            data = "".join("data: " + json.dumps(item) + "\n\n" for item in chunks) + "data: [DONE]\n\n"
            body = data.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("X-RateLimit-Remaining-Requests", "9")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_json(
            {
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
            headers={"X-RateLimit-Remaining-Requests": "9"},
        )

    def send_json(self, payload, headers=None):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class LoadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_model_discovery(self):
        self.assertEqual(tool.discover_models(self.base_url, "secret", 3), ["model-a", "model-b"])

    def test_non_stream_and_stream(self):
        plain = tool.perform_request(1, self.base_url, "secret", "test", "OK", 8, False, 3)
        streamed = tool.perform_request(2, self.base_url, "secret", "test", "OK", 8, True, 3)
        self.assertTrue(plain["ok"])
        self.assertTrue(streamed["ok"])
        self.assertEqual(plain["response_chars"], 2)
        self.assertEqual(streamed["response_chars"], 2)
        self.assertEqual(streamed["usage"]["total_tokens"], 7)
        self.assertEqual(streamed["rate_limit_headers"]["x-ratelimit-remaining-requests"], "9")

    def test_rate_limit_classification(self):
        result = tool.perform_request(1, self.base_url, "secret", "test", "RATE", 8, False, 3)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 429)
        self.assertEqual(result["error_category"], "rate_limit")
        self.assertEqual(result["rate_limit_headers"]["retry-after"], "1")

    def test_summary_and_cost(self):
        results = [
            {"ok": True, "first_response_ms": 10, "total_ms": 20, "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 100_000, "total_tokens": 1_100_000}},
            {"ok": False, "first_response_ms": None, "total_ms": 30, "usage": None, "error_category": "rate_limit"},
        ]
        summary = tool.summarize(results, 2, (2.0, 10.0))
        self.assertEqual(summary["success_rate"], 0.5)
        self.assertEqual(summary["errors"], {"rate_limit": 1})
        self.assertEqual(summary["known_cost_estimate"], 3.0)

    def test_end_to_end_report_contains_no_response_text(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder, "report.json")
            html_output = Path(folder, "report.html")
            os.environ["TEST_API_KEY"] = "secret-value"
            try:
                code = tool.main([
                    "--base-url", self.base_url,
                    "--model", "test",
                    "--api-key-env", "TEST_API_KEY",
                    "--requests", "3",
                    "--warmup", "0",
                    "--concurrency", "2",
                    "--rpm", "600",
                    "--max-tokens", "8",
                    "--output", str(output),
                    "--html", str(html_output),
                    "--confirm-live-run",
                ])
            finally:
                os.environ.pop("TEST_API_KEY", None)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(code, 0)
            self.assertEqual(report["summary"]["succeeded"], 3)
            self.assertNotIn("secret-value", output.read_text(encoding="utf-8"))
            self.assertNotIn('"content": "OK"', output.read_text(encoding="utf-8"))
            self.assertIn("LLM API Load Test", html_output.read_text(encoding="utf-8"))

    def test_validation_limits(self):
        with self.assertRaises(ValueError):
            tool.validate_base_url("http://example.com/v1")
        with self.assertRaises(ValueError):
            tool.validate_base_url("https://user@example.com/v1?key=secret")
        args = tool.parse_args(["--base-url", "https://example.com/v1", "--model", "m", "--requests", "501"])
        with self.assertRaises(ValueError):
            tool.validate_args(args)
        payload = tool.request_payload("m", "OK", 8, "max_completion_tokens", False)
        self.assertEqual(payload["max_completion_tokens"], 8)
        self.assertNotIn("max_tokens", payload)


if __name__ == "__main__":
    unittest.main()
