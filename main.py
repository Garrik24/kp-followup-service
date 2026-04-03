"""
KP Auto Follow-Up Service
Автоматический дожим по отправленным коммерческим предложениям.

Стратегия:
- Дожим 1: через 3 дня после отправки КП
- Дожим 2: через 4 дня после дожима 1
- Дожим 3: через 7 дней после дожима 2

Работает на Railway по cron: будни 10:00 и 15:00 МСК.
"""

import json
import logging
import os
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Конфигурация ─────────────────────────────────────────────────────────────

AMOCRM_SUBDOMAIN = os.environ.get("AMOCRM_SUBDOMAIN", "stavgeo26")
AMOCRM_ACCESS_TOKEN = os.environ.get("AMOCRM_ACCESS_TOKEN", "")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MAIL_MCP_URL = os.environ.get(
    "MAIL_MCP_URL",
    "https://mail-mcp-server-production.up.railway.app/mcp",
)

# Воронка «Юридические Лица»
PIPELINE_ID = int(os.environ.get("PIPELINE_ID", "3887935"))
# Стадия «КП отправлено»
STATUS_ID = int(os.environ.get("STATUS_ID", "37270534"))

MSK = timezone(timedelta(hours=3))

# ── Шаблоны дожимов ──────────────────────────────────────────────────────────

FOLLOWUP_TEMPLATES = {
    1: (
        "Добрый день!<br><br>"
        "Направляли вам коммерческое предложение по теме «{deal_name}». "
        "Хотели уточнить, успели ли ознакомиться?"
    ),
    2: (
        "Добрый день!<br><br>"
        "Хотели бы уточнить, когда будет проводиться процедура закупки."
    ),
    3: (
        "Добрый день!<br><br>"
        "Так и не получили от вас ответа на наше коммерческое предложение. "
        "Если у вас есть вопросы, мы готовы на них оперативно ответить."
    ),
}

# Интервалы (дни) перед каждым дожимом
FOLLOWUP_INTERVALS = {
    1: 3,   # 3 дня после отправки КП
    2: 4,   # 4 дня после дожима 1
    3: 7,   # 7 дней после дожима 2
}


# ── AmoCRM API ───────────────────────────────────────────────────────────────

def amocrm_request(method: str, endpoint: str,
                    params: dict = None, body: dict | list = None) -> dict | list | None:
    """Универсальный запрос к amoCRM API v4."""
    base = f"https://{AMOCRM_SUBDOMAIN}.amocrm.ru"
    if not endpoint.startswith("/api/"):
        endpoint = f"/api/v4{endpoint}"
    url = f"{base}{endpoint}"

    if params:
        qs = urllib.parse.urlencode(params, doseq=True)
        url = f"{url}?{qs}"

    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        method=method.upper(),
        headers={
            "Authorization": f"Bearer {AMOCRM_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_err = e.read().decode("utf-8", errors="replace")
        log.error(f"AmoCRM {method} {endpoint}: {e.code} {body_err[:300]}")
        return None
    except Exception as e:
        log.error(f"AmoCRM {method} {endpoint}: {e}")
        return None


def get_leads_in_kp_stage() -> list[dict]:
    """Получить сделки в стадии «КП отправлено»."""
    result = amocrm_request("GET", "/leads", params={
        "filter[statuses][0][pipeline_id]": PIPELINE_ID,
        "filter[statuses][0][status_id]": STATUS_ID,
        "with": "contacts",
        "limit": 50,
    })
    if not result:
        return []
    return result.get("_embedded", {}).get("leads", [])


def get_notes(lead_id: int, limit: int = 50) -> list[dict]:
    """Получить заметки сделки."""
    result = amocrm_request("GET", f"/leads/{lead_id}/notes", params={
        "limit": limit,
    })
    if not result:
        return []
    return result.get("_embedded", {}).get("notes", [])


def get_contact_email(contact_id: int) -> str:
    """Получить email контакта."""
    result = amocrm_request("GET", f"/contacts/{contact_id}")
    if not result:
        return ""
    for field in result.get("custom_fields_values", []):
        if field.get("field_code") == "EMAIL":
            values = field.get("values", [])
            if values:
                return values[0].get("value", "")
    return ""


def add_note(lead_id: int, text: str) -> bool:
    """Добавить заметку к сделке."""
    result = amocrm_request("POST", f"/leads/{lead_id}/notes", body=[{
        "note_type": "common",
        "params": {"text": text},
    }])
    return result is not None


# ── Mail MCP ─────────────────────────────────────────────────────────────────

def send_email(to: str, subject: str, body: str) -> bool:
    """Отправить письмо через mail MCP сервер."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "send_new_email",
            "arguments": {
                "to": to,
                "subject": subject,
                "body": body,
            },
        },
    }).encode("utf-8")

    req = urllib.request.Request(
        MAIL_MCP_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("error"):
                log.error(f"Mail MCP error: {result['error']}")
                return False
            # Проверяем результат внутри
            content = result.get("result", {}).get("content", [])
            if content:
                inner = json.loads(content[0].get("text", "{}"))
                if inner.get("status") == "sent":
                    log.info(f"Email отправлен: {to}")
                    return True
                if inner.get("error"):
                    log.error(f"Mail error: {inner['error']}")
                    return False
            return True
    except Exception as e:
        log.error(f"Ошибка отправки email: {e}")
        return False


# ── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    """Отправить уведомление в Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram не настроен")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
    })

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False


# ── Основная логика ──────────────────────────────────────────────────────────

def analyze_deal(lead: dict, notes: list[dict]) -> dict:
    """Анализирует сделку и определяет нужен ли дожим.

    Возвращает:
      {
        "needs_followup": bool,
        "followup_number": int,
        "reason": str,
        "days_since_last": int,
        "existing_followups": int,
      }
    """
    now = datetime.now(MSK)
    today_str = now.strftime("%Y-%m-%d")

    # Считаем существующие дожимы
    followup_notes = []
    for note in notes:
        params = note.get("params", {})
        text = params.get("text", "") if isinstance(params, dict) else ""
        if "Автодожим:" in text:
            followup_notes.append(note)

    existing_count = len(followup_notes)

    # Лимит исчерпан
    if existing_count >= 3:
        return {
            "needs_followup": False,
            "reason": "лимит 3 дожима исчерпан",
            "existing_followups": existing_count,
        }

    # Дата последнего дожима (или updated_at сделки)
    if followup_notes:
        last_followup_ts = max(n.get("created_at", 0) for n in followup_notes)
        last_date = datetime.fromtimestamp(last_followup_ts, tz=MSK)

        # Проверка: не отправляли ли уже сегодня
        if last_date.strftime("%Y-%m-%d") == today_str:
            return {
                "needs_followup": False,
                "reason": "уже отправляли дожим сегодня",
                "existing_followups": existing_count,
            }
    else:
        # Дата обновления сделки (примерно = дата отправки КП)
        last_followup_ts = lead.get("updated_at", lead.get("created_at", 0))
        last_date = datetime.fromtimestamp(last_followup_ts, tz=MSK)

    # Проверяем входящие от клиента за последние 2 дня
    two_days_ago = now - timedelta(days=2)
    for note in notes:
        note_type = note.get("note_type", "")
        if note_type in ("incoming_mail_message", "incoming_chat_message"):
            note_ts = note.get("created_at", 0)
            note_date = datetime.fromtimestamp(note_ts, tz=MSK)
            if note_date >= two_days_ago:
                return {
                    "needs_followup": False,
                    "reason": "клиент ответил за последние 2 дня",
                    "existing_followups": existing_count,
                }

    # Сколько дней прошло
    days_since = (now - last_date).days
    next_followup = existing_count + 1
    required_days = FOLLOWUP_INTERVALS.get(next_followup, 999)

    if days_since >= required_days:
        return {
            "needs_followup": True,
            "followup_number": next_followup,
            "days_since_last": days_since,
            "existing_followups": existing_count,
            "reason": f"прошло {days_since} дней (нужно {required_days})",
        }

    return {
        "needs_followup": False,
        "reason": f"рано: прошло {days_since}/{required_days} дней до дожима #{next_followup}",
        "existing_followups": existing_count,
    }


def process_deal(lead: dict) -> dict:
    """Обработать одну сделку. Возвращает результат."""
    lead_id = lead["id"]
    deal_name = lead.get("name", "Без названия")
    price = lead.get("price", 0)

    log.info(f"── Сделка #{lead_id}: {deal_name} (бюджет: {price}₽)")

    # Получаем контакт
    contacts = lead.get("_embedded", {}).get("contacts", [])
    if not contacts:
        return {"lead_id": lead_id, "status": "skip", "reason": "нет контакта"}

    contact_id = contacts[0]["id"]
    email = get_contact_email(contact_id)
    if not email:
        return {"lead_id": lead_id, "status": "skip", "reason": "нет email"}

    # Получаем заметки
    notes = get_notes(lead_id)

    # Анализируем
    analysis = analyze_deal(lead, notes)
    if not analysis["needs_followup"]:
        log.info(f"   Пропуск: {analysis['reason']}")
        return {
            "lead_id": lead_id,
            "status": "skip",
            "reason": analysis["reason"],
        }

    followup_num = analysis["followup_number"]
    template = FOLLOWUP_TEMPLATES[followup_num]
    body = template.format(deal_name=deal_name)
    subject = f"Re: {deal_name}"

    log.info(f"   → Дожим #{followup_num} на {email}")

    # 1. Отправляем email
    if not send_email(to=email, subject=subject, body=body):
        log.error(f"   ✗ Email не отправлен")
        return {
            "lead_id": lead_id,
            "status": "error",
            "reason": "ошибка отправки email",
        }

    # 2. Записываем заметку в amoCRM
    note_text = (
        f"Автодожим: отправлен follow-up #{followup_num} "
        f"по email на {email}."
    )
    add_note(lead_id, note_text)

    # 3. Отправляем Telegram-уведомление
    tg_text = (
        f"📬 <b>АВТОДОЖИМ #{followup_num}</b>\n"
        f"──────────────\n"
        f"📋 <b>Сделка:</b> {deal_name}\n"
        f"📧 <b>Кому:</b> {email}\n"
        f"💰 <b>Бюджет:</b> {price:,}₽\n"
        f"📝 <b>Дожим:</b> #{followup_num} из 3\n"
        f"🕐 {datetime.now(MSK).strftime('%H:%M')} МСК"
    )
    send_telegram(tg_text)

    log.info(f"   ✓ Дожим #{followup_num} отправлен")
    return {
        "lead_id": lead_id,
        "deal_name": deal_name,
        "email": email,
        "status": "sent",
        "followup_number": followup_num,
    }


def run_followup():
    """Основной цикл: проверить все сделки и отправить дожимы."""
    now_msk = datetime.now(MSK)
    log.info("=" * 60)
    log.info(f"🚀 Запуск автодожима: {now_msk.strftime('%Y-%m-%d %H:%M')} МСК")
    log.info("=" * 60)

    if not AMOCRM_ACCESS_TOKEN:
        log.error("AMOCRM_ACCESS_TOKEN не настроен!")
        return

    # Получаем сделки
    leads = get_leads_in_kp_stage()
    log.info(f"Найдено сделок в «КП отправлено»: {len(leads)}")

    if not leads:
        log.info("Нет сделок для дожима")
        return

    # Обрабатываем
    results = {"sent": [], "skip": [], "error": []}
    for lead in leads:
        result = process_deal(lead)
        results[result["status"]].append(result)
        # Пауза между запросами чтобы не превысить rate limit
        time.sleep(1)

    # Итоговый отчёт
    log.info("")
    log.info("═" * 60)
    log.info(f"📊 ИТОГО:")
    log.info(f"   Проверено сделок: {len(leads)}")
    log.info(f"   Дожимов отправлено: {len(results['sent'])}")
    log.info(f"   Пропущено: {len(results['skip'])}")
    log.info(f"   Ошибок: {len(results['error'])}")

    for r in results["sent"]:
        log.info(f"   ✓ #{r['lead_id']} {r['deal_name']} → {r['email']} (дожим #{r['followup_number']})")

    log.info("═" * 60)

    # Telegram-отчёт если были дожимы
    if results["sent"]:
        summary = (
            f"📊 <b>Автодожим завершён</b>\n"
            f"──────────────\n"
            f"✅ Отправлено: {len(results['sent'])}\n"
            f"⏭ Пропущено: {len(results['skip'])}\n"
            f"❌ Ошибок: {len(results['error'])}\n"
            f"🕐 {now_msk.strftime('%H:%M')} МСК"
        )
        send_telegram(summary)


# ── Запуск ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_followup()
