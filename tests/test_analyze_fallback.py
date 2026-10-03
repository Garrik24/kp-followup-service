import io, json, os, sys, unittest, urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
for name in ("telegram", "telegram.ext", "apscheduler", "apscheduler.schedulers", "apscheduler.schedulers.asyncio",
             "apscheduler.triggers", "apscheduler.triggers.cron"):
    sys.modules.setdefault(name, mock.MagicMock())

import gemini_fallback  # noqa: E402
import main  # noqa: E402

GOOD = '{"summary":"Решение до пятницы","sentiment":"neutral","has_date":true,"decision_date":"2026-10-09","days_to_wait":6,"action":"wait"}'


class Resp:
    def __init__(self, payload): self._p = json.dumps(payload).encode()
    def read(self): return self._p
    def __enter__(self): return self
    def __exit__(self, *a): return False


def http_error(code, body):
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(body.encode()))


def analyze():
    return main.analyze_response_with_claude("Сделка", "Иван", "Подумаем до пятницы")


class AnalyzeFallback(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(main, "ANTHROPIC_API_KEY", "k"); p.start(); self.addCleanup(p.stop)

    def test_claude_ok_skips_thinking_block(self):
        payload = {"content": [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": GOOD}]}
        with mock.patch("urllib.request.urlopen", return_value=Resp(payload)), \
             mock.patch.object(gemini_fallback, "generate") as g:
            self.assertEqual(analyze()["decision_date"], "2026-10-09")
        g.assert_not_called()

    def test_org_blocked_uses_gemini(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_error(400, "This organization has been disabled.")), \
             mock.patch.object(gemini_fallback, "enabled", return_value=True), \
             mock.patch.object(gemini_fallback, "generate", return_value=GOOD) as g:
            self.assertEqual(analyze()["action"], "wait")
        self.assertTrue(g.call_args.kwargs["json_mode"])

    def test_network_error_uses_gemini(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")), \
             mock.patch.object(gemini_fallback, "enabled", return_value=True), \
             mock.patch.object(gemini_fallback, "generate", return_value=GOOD):
            self.assertEqual(analyze()["days_to_wait"], 6)

    def test_request_error_does_not_use_gemini(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_error(400, "max_tokens: bad")), \
             mock.patch.object(gemini_fallback, "enabled", return_value=True), \
             mock.patch.object(gemini_fallback, "generate") as g:
            self.assertEqual(analyze()["action"], "wait")  # значения по умолчанию, как раньше
        g.assert_not_called()

    def test_no_claude_key_uses_gemini(self):
        with mock.patch.object(main, "ANTHROPIC_API_KEY", ""), \
             mock.patch.object(gemini_fallback, "enabled", return_value=True), \
             mock.patch.object(gemini_fallback, "generate", return_value=GOOD):
            self.assertEqual(analyze()["decision_date"], "2026-10-09")

    def test_both_down_returns_default(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_error(429, "rate")), \
             mock.patch.object(gemini_fallback, "enabled", return_value=True), \
             mock.patch.object(gemini_fallback, "generate", side_effect=RuntimeError("boom")):
            r = analyze()
        self.assertEqual((r["action"], r["days_to_wait"]), ("wait", 3))


if __name__ == "__main__":
    unittest.main()
