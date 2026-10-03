import json, os, unittest
from unittest import mock
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gemini_fallback as gf


class FakeResponse:
    def __init__(self, payload): self._p = json.dumps(payload).encode()
    def read(self): return self._p
    def __enter__(self): return self
    def __exit__(self, *a): return False


def ok(text, **extra):
    c = {"content": {"parts": [{"text": "думаю", "thought": True}, {"text": text}]}, "finishReason": "STOP"}
    c.update(extra)
    return FakeResponse({"candidates": [c], "usageMetadata": {}})


class IsOutage(unittest.TestCase):
    def test_service_failures(self):
        self.assertTrue(gf.is_outage(400, "Your credit balance is too low"))
        self.assertTrue(gf.is_outage(400, "This organization has been disabled."))
        for s in (401, 402, 403, 429, 500, 529):
            self.assertTrue(gf.is_outage(s, "x"))

    def test_request_errors_are_not_outage(self):
        self.assertFalse(gf.is_outage(400, "max_tokens: must be greater than 0"))
        self.assertFalse(gf.is_outage(404, "model not found"))
        self.assertFalse(gf.is_outage(None, "connection"))


@mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k"})
class Generate(unittest.TestCase):
    def test_skips_thoughts_and_sends_expected_body(self):
        with mock.patch("urllib.request.urlopen", return_value=ok("ответ")) as op:
            self.assertEqual(gf.generate("sys", "user", max_tokens=500, json_mode=True), "ответ")
        body = json.loads(op.call_args[0][0].data)
        self.assertEqual(body["system_instruction"]["parts"][0]["text"], "sys")
        self.assertEqual(body["generationConfig"]["maxOutputTokens"], 4500)
        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(body["generationConfig"]["thinkingConfig"]["thinkingLevel"], "low")

    def test_max_tokens_is_error(self):
        with mock.patch("urllib.request.urlopen", return_value=ok("обрезок", finishReason="MAX_TOKENS")):
            with self.assertRaisesRegex(RuntimeError, "обрезан"):
                gf.generate("", "u")

    def test_no_key_is_error_and_disabled(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            self.assertFalse(gf.enabled())
            with self.assertRaises(RuntimeError):
                gf.generate("", "u")

    def test_retries_503_then_succeeds(self):
        import urllib.error, io
        err = urllib.error.HTTPError("u", 503, "x", {}, io.BytesIO(b"busy"))
        with mock.patch("urllib.request.urlopen", side_effect=[err, ok("ок")]), mock.patch("time.sleep"):
            self.assertEqual(gf.generate("", "u"), "ок")

    def test_400_not_retried(self):
        import urllib.error, io
        err = urllib.error.HTTPError("u", 400, "x", {}, io.BytesIO(b"bad"))
        with mock.patch("urllib.request.urlopen", side_effect=err) as op:
            with self.assertRaisesRegex(RuntimeError, "400"):
                gf.generate("", "u")
        self.assertEqual(op.call_count, 1)


if __name__ == "__main__":
    unittest.main()
