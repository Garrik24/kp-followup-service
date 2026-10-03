"""Запасной LLM: Gemini API (generateContent), если Claude недоступен как сервис.

Только стандартная библиотека. Включается переменной GEMINI_API_KEY; без неё
`enabled()` ложно, и сервисы работают как раньше.

Запрос по документации Google (v1beta/models/{model}:generateContent):
system_instruction + contents + generationConfig. Проверено на живом API:
- размышления Gemini 3 считаются в тот же maxOutputTokens, что и текст, поэтому
  к лимиту добавляется запас, а глубина размышлений ограничена (thinkingLevel low);
- уровень minimal модель gemini-3.7-flash не принимает (400) — не используем.

Переменные окружения:
  GEMINI_API_KEY        ключ (без него запасной канал выключен)
  GEMINI_TEXT_MODEL     модель, по умолчанию gemini-3.7-flash
  GEMINI_FALLBACK_ENABLED=false  выключить при наличии ключа
"""

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request

log = logging.getLogger("gemini_fallback")

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-3.7-flash"
_RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
# Запас токенов под размышления: они делят лимит с текстом ответа.
_THINKING_HEADROOM = 4000

_OUTAGE_400 = re.compile(
    r"credit balance|organization has been disabled|organization_on_hold", re.I
)


def enabled() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY")) and (
        os.environ.get("GEMINI_FALLBACK_ENABLED", "true").strip().lower() != "false"
    )


def is_outage(status, message: str = "") -> bool:
    """Сбой самого сервиса Anthropic, а не этого запроса.

    Пустой баланс и блокировка организации приходят как 400, ключ — 401/403,
    лимиты — 429, перегрузка и сбои сервера — 5xx/529. Обычные ошибки запроса
    (кривые параметры) запасной канал не включают: другая модель их не исправит.
    `status=None` (обрыв связи) вызывающий код считает сбоем сам.
    """
    if status is None:
        return False
    if status in (401, 402, 403, 429) or status >= 500:
        return True
    return status == 400 and bool(_OUTAGE_400.search(message or ""))


def generate(
    system: str,
    user: str,
    max_tokens: int = 4096,
    json_mode: bool = False,
    timeout: int = 90,
) -> str:
    """Возвращает текст ответа Gemini. Бросает RuntimeError при любой неудаче."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY не задан")
    model = os.environ.get("GEMINI_TEXT_MODEL", DEFAULT_MODEL)

    generation_config = {
        "maxOutputTokens": max_tokens + _THINKING_HEADROOM,
        "thinkingConfig": {"thinkingLevel": "low"},
    }
    if json_mode:
        generation_config["responseMimeType"] = "application/json"
    body = {"contents": [{"role": "user", "parts": [{"text": user}]}], "generationConfig": generation_config}
    if system:
        body["system_instruction"] = {"parts": [{"text": system}]}

    request = urllib.request.Request(
        f"{BASE_URL}/{model}:generateContent",
        data=json.dumps(body).encode(),
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
    )

    last_error: Exception = RuntimeError("Gemini: нет попыток")
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode())
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            last_error = RuntimeError(f"Gemini ответил {exc.code}: {detail}")
            retryable = exc.code in _RETRY_STATUSES
        except Exception as exc:  # сеть, таймаут
            last_error = RuntimeError(f"Gemini: нет ответа ({exc})")
            retryable = True
        if not retryable or attempt == _MAX_ATTEMPTS:
            raise last_error
        delay = 2 * 2 ** (attempt - 1)
        log.warning("%s — повтор через %s с", last_error, delay)
        time.sleep(delay)

    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason")
        raise RuntimeError(f"Gemini не вернул ответ{f' (blockReason: {reason})' if reason else ''}")
    candidate = candidates[0]
    if candidate.get("finishReason") == "MAX_TOKENS":
        raise RuntimeError("ответ Gemini обрезан по лимиту токенов")
    # Блоки размышления помечены thought: true — в ответ они не идут.
    text = "".join(
        part.get("text", "")
        for part in (candidate.get("content") or {}).get("parts", [])
        if part.get("text") and not part.get("thought")
    ).strip()
    if not text:
        raise RuntimeError(f"Gemini вернул ответ без текста (finishReason: {candidate.get('finishReason')})")

    usage = data.get("usageMetadata") or {}
    log.info(
        "Gemini %s: вход %s, выход %s, размышления %s токенов",
        model, usage.get("promptTokenCount"), usage.get("candidatesTokenCount"), usage.get("thoughtsTokenCount", 0),
    )
    return text
