"""
KP Follow-Up Bot — Telegram-бот с интерактивными кнопками.

Архитектура:
- Telegram webhook-бот (постоянно работает)
- APScheduler cron: будни 10:00 и 15:00 МСК — проверяет сделки
- По cron отправляет карточки с кнопками в Telegram
- Пользователь нажимает кнопку → бот выполняет действие
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

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

PORT = int(os.environ.get("PORT", "8000"))
RAILWAY_PUBLIC_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")

PIPELINE_ID = int(os.environ.get("PIPELINE_ID", "3887935"))
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

FOLLOWUP_INTERVALS = {1: 3, 2: 4, 3: 7}


# ── AmoCRM API ───────────────────────────────────────────────────────────────

def amocrm_request(method: str, endpoint: str,
                    params: dict = None, body: dict | list = None) -> dict | list | None:
    base = f"https://{AMOCRM_SUBDOMAIN}.amocrm.ru"
    if not endpoint.startswith("/api/"):
        endpoint = f"/api/v4{endpoint}"
    url = f"{base}{endpoint}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method.upper(), headers={
        "Authorization": f"Bearer {AMOCRM_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        log.error(f"AmoCRM {method} {endpoint}: {e.code} {e.read().decode('utf-8', errors='replace')[:300]}")
        return None
    except Exception as e:
        log.error(f"AmoCRM {method} {endpoint}: {e}")
        return None


def get_leads_in_kp_stage() -> list[dict]:
    result = amocrm_request("GET", "/leads", params={
        "filter[statuses][0][pipeline_id]": PIPELINE_ID,
        "filter[statuses][0][status_id]": STATUS_ID,
        "with": "contacts",
        "limit": 50,
    })
    if not result:
        return []
    return result.get("_embedded", {}).get("leads", [])


def get_notes(lead_id: int) -> list[dict]:
    result = amocrm_request("GET", f"/leads/{lead_id}/notes", params={"limit": 50})
    if not result:
        return []
    return result.get("_embedded", {}).get("notes", [])


def get_contact_info(contact_id: int) -> dict:
    """Получить email и имя контакта."""
    result = amocrm_request("GET", f"/contacts/{contact_id}")
    if not result:
        return {"name": "", "email": "", "phone": ""}
    email = ""
    phone = ""
    for field in result.get("custom_fields_values", []) or []:
        if field.get("field_code") == "EMAIL":
            vals = field.get("values", [])
            if vals:
                email = vals[0].get("value", "")
        if field.get("field_code") == "PHONE":
            vals = field.get("values", [])
            if vals:
                phone = vals[0].get("value", "")
    return {
        "name": result.get("name", ""),
        "email": email,
        "phone": phone,
    }


def add_note(lead_id: int, text: str) -> bool:
    result = amocrm_request("POST", f"/leads/{lead_id}/notes", body=[{
        "note_type": "common",
        "params": {"text": text},
    }])
    return result is not None


def create_task_amocrm(lead_id: int, text: str, days_from_now: int = 1) -> bool:
    """Создать задачу в amoCRM."""
    complete_till = int((datetime.now(MSK) + timedelta(days=days_from_now)).timestamp())
    result = amocrm_request("POST", "/tasks", body=[{
        "text": text,
        "complete_till": complete_till,
        "entity_id": lead_id,
        "entity_type": "leads",
        "task_type_id": 1,  # Связаться
    }])
    return result is not None


# ── Mail MCP ─────────────────────────────────────────────────────────────────

def send_email(to: str, subject: str, body: str) -> bool:
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "send_new_email", "arguments": {
            "to": to, "subject": subject, "body": body,
        }},
    }).encode()
    req = urllib.request.Request(MAIL_MCP_URL, data=payload, headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            content = result.get("result", {}).get("content", [])
            if content:
                inner = json.loads(content[0].get("text", "{}"))
                if inner.get("status") == "sent":
                    return True
                if inner.get("error"):
                    log.error(f"Mail error: {inner['error']}")
                    return False
            return not result.get("error")
    except Exception as e:
        log.error(f"Mail send error: {e}")
        return False


# ── Анализ сделки ────────────────────────────────────────────────────────────

def analyze_deal(lead: dict, notes: list[dict]) -> dict:
    now = datetime.now(MSK)
    today_str = now.strftime("%Y-%m-%d")

    followup_notes = []
    for note in notes:
        params = note.get("params", {})
        text = params.get("text", "") if isinstance(params, dict) else ""
        if "Автодожим:" in text:
            followup_notes.append(note)

    existing_count = len(followup_notes)

    if existing_count >= 3:
        return {"needs_followup": False, "reason": "лимит 3 дожима исчерпан",
                "existing_followups": existing_count}

    if followup_notes:
        last_ts = max(n.get("created_at", 0) for n in followup_notes)
        last_date = datetime.fromtimestamp(last_ts, tz=MSK)
        if last_date.strftime("%Y-%m-%d") == today_str:
            return {"needs_followup": False, "reason": "уже отправляли дожим сегодня",
                    "existing_followups": existing_count}
    else:
        last_ts = lead.get("updated_at", lead.get("created_at", 0))
        last_date = datetime.fromtimestamp(last_ts, tz=MSK)

    two_days_ago = now - timedelta(days=2)
    for note in notes:
        if note.get("note_type") in ("incoming_mail_message", "incoming_chat_message"):
            note_date = datetime.fromtimestamp(note.get("created_at", 0), tz=MSK)
            if note_date >= two_days_ago:
                return {"needs_followup": False, "reason": "клиент ответил за 2 дня",
                        "existing_followups": existing_count}

    days_since = (now - last_date).days
    next_num = existing_count + 1
    required = FOLLOWUP_INTERVALS.get(next_num, 999)

    if days_since >= required:
        return {"needs_followup": True, "followup_number": next_num,
                "days_since": days_since, "existing_followups": existing_count}

    return {"needs_followup": False,
            "reason": f"рано: {days_since}/{required} дней до дожима #{next_num}",
            "existing_followups": existing_count}


# ── Telegram handlers ────────────────────────────────────────────────────────

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /check — ручная проверка сделок."""
    await update.message.reply_text("🔍 Проверяю сделки в «КП отправлено»...")
    await check_deals(context)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /status — статус бота."""
    now = datetime.now(MSK).strftime("%Y-%m-%d %H:%M")
    await update.message.reply_text(
        f"🤖 <b>KP Follow-Up Bot</b>\n"
        f"──────────────\n"
        f"⏰ Время: {now} МСК\n"
        f"📊 Cron: будни 10:00 и 15:00\n"
        f"🔧 Статус: работает",
        parse_mode="HTML",
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий inline-кнопок."""
    query = update.callback_query
    await query.answer()

    data = query.data  # формат: action:lead_id:email:followup_num
    parts = data.split(":", 3)
    if len(parts) < 2:
        await query.edit_message_text("❌ Ошибка: неверные данные кнопки")
        return

    action = parts[0]
    lead_id = int(parts[1])
    email = parts[2] if len(parts) > 2 else ""
    followup_num = int(parts[3]) if len(parts) > 3 else 1

    lead_info = amocrm_request("GET", f"/leads/{lead_id}")
    deal_name = lead_info.get("name", "Сделка") if lead_info else "Сделка"

    if action == "send_followup":
        # Отправить дожим-письмо
        template = FOLLOWUP_TEMPLATES.get(followup_num, FOLLOWUP_TEMPLATES[1])
        body = template.format(deal_name=deal_name)
        subject = f"Re: {deal_name}"

        await query.edit_message_text(
            query.message.text + "\n\n⏳ <i>Отправляю письмо...</i>",
            parse_mode="HTML",
        )

        if send_email(to=email, subject=subject, body=body):
            note_text = f"Автодожим: отправлен follow-up #{followup_num} по email на {email}."
            add_note(lead_id, note_text)
            await query.edit_message_text(
                query.message.text_html + f"\n\n✅ <b>Письмо отправлено на {email}</b>",
                parse_mode="HTML",
            )
        else:
            await query.edit_message_text(
                query.message.text_html + "\n\n❌ <b>Ошибка отправки письма</b>",
                parse_mode="HTML",
            )

    elif action == "task_call_1":
        # Задача: перезвонить завтра
        create_task_amocrm(lead_id, f"Перезвонить по КП: {deal_name}", days_from_now=1)
        add_note(lead_id, f"Автодожим: создана задача «перезвонить завтра» по сделке.")
        await query.edit_message_text(
            query.message.text_html + "\n\n📞 <b>Задача создана: перезвонить завтра</b>",
            parse_mode="HTML",
        )

    elif action == "task_call_3":
        create_task_amocrm(lead_id, f"Перезвонить по КП: {deal_name}", days_from_now=3)
        add_note(lead_id, f"Автодожим: создана задача «перезвонить через 3 дня» по сделке.")
        await query.edit_message_text(
            query.message.text_html + "\n\n📞 <b>Задача создана: перезвонить через 3 дня</b>",
            parse_mode="HTML",
        )

    elif action == "task_call_7":
        create_task_amocrm(lead_id, f"Перезвонить по КП: {deal_name}", days_from_now=7)
        add_note(lead_id, f"Автодожим: создана задача «перезвонить через 7 дней» по сделке.")
        await query.edit_message_text(
            query.message.text_html + "\n\n📞 <b>Задача создана: перезвонить через 7 дней</b>",
            parse_mode="HTML",
        )

    elif action == "close_deal":
        # Закрыть сделку (не реализовано)
        amocrm_request("PATCH", f"/leads/{lead_id}", body={
            "status_id": 143,  # Закрыто и не реализовано
        })
        add_note(lead_id, "Автодожим: сделка закрыта (не реализовано) по решению менеджера.")
        await query.edit_message_text(
            query.message.text_html + "\n\n❌ <b>Сделка закрыта (не реализовано)</b>",
            parse_mode="HTML",
        )

    elif action == "skip":
        await query.edit_message_text(
            query.message.text_html + "\n\n⏸ <b>Пропущено</b>",
            parse_mode="HTML",
        )


# ── Cron-проверка ────────────────────────────────────────────────────────────

async def check_deals(bot_or_context):
    """Проверить сделки и отправить карточки в Telegram.
    bot_or_context: telegram.Bot (из cron) или ContextTypes.DEFAULT_TYPE (из команды).
    """
    from telegram import Bot
    if isinstance(bot_or_context, Bot):
        bot = bot_or_context
    elif hasattr(bot_or_context, "bot"):
        bot = bot_or_context.bot
    else:
        bot = bot_or_context

    now_msk = datetime.now(MSK)
    log.info(f"🔍 Проверка сделок: {now_msk.strftime('%Y-%m-%d %H:%M')} МСК")

    leads = get_leads_in_kp_stage()
    log.info(f"Найдено сделок: {len(leads)}")

    candidates = 0
    for lead in leads:
        lead_id = lead["id"]
        deal_name = lead.get("name", "Без названия")
        price = lead.get("price", 0) or 0

        contacts = lead.get("_embedded", {}).get("contacts", [])
        if not contacts:
            continue

        contact_id = contacts[0]["id"]
        contact = get_contact_info(contact_id)
        email = contact["email"]
        phone = contact["phone"]
        contact_name = contact["name"]

        notes = get_notes(lead_id)
        analysis = analyze_deal(lead, notes)

        if not analysis["needs_followup"]:
            continue

        followup_num = analysis["followup_number"]
        days = analysis.get("days_since", 0)
        candidates += 1

        # Формируем карточку
        has_email = bool(email)
        action_hint = "✉️ Отправить дожим" if has_email else "📞 Нужен звонок — нет email"

        text = (
            f"{'✉️ Дожим email' if has_email else '📞 Нужен звонок — нет email'}\n"
            f"──────────────\n"
            f"📁 <b>Сделка:</b> {deal_name} (#{lead_id})\n"
            f"💰 <b>Бюджет:</b> {price:,} ₽\n"
            f"👤 <b>Контакт:</b> {contact_name}"
        )
        if phone:
            text += f", тел. {phone}"
        if email:
            text += f"\n📧 <b>Email:</b> {email}"
        text += (
            f"\n📅 <b>Без ответа:</b> {days} дней"
            f"\n📝 <b>Дожим:</b> #{followup_num} из 3"
        )

        # Кнопки
        buttons = []
        if has_email:
            buttons.append([InlineKeyboardButton(
                f"✉️ Отправить дожим #{followup_num}",
                callback_data=f"send_followup:{lead_id}:{email}:{followup_num}",
            )])

        buttons.append([
            InlineKeyboardButton("📞 Завтра", callback_data=f"task_call_1:{lead_id}:{email}:{followup_num}"),
            InlineKeyboardButton("📞 Через 3 дня", callback_data=f"task_call_3:{lead_id}:{email}:{followup_num}"),
            InlineKeyboardButton("📞 Через 7 дней", callback_data=f"task_call_7:{lead_id}:{email}:{followup_num}"),
        ])
        buttons.append([
            InlineKeyboardButton("❌ Закрыть (не реализовано)", callback_data=f"close_deal:{lead_id}:{email}:{followup_num}"),
            InlineKeyboardButton("⏸ Оставить", callback_data=f"skip:{lead_id}:{email}:{followup_num}"),
        ])

        keyboard = InlineKeyboardMarkup(buttons)

        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

        # Небольшая пауза между сообщениями
        await asyncio.sleep(0.5)

    if candidates == 0:
        log.info("Нет сделок для дожима")
    else:
        log.info(f"Отправлено {candidates} карточек в Telegram")


# ── Запуск ───────────────────────────────────────────────────────────────────

def main():
    log.info("🚀 Запуск KP Follow-Up Bot")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("status", cmd_status))

    # Кнопки
    app.add_handler(CallbackQueryHandler(callback_handler))

    # post_init: запускаем scheduler когда event loop уже работает
    async def post_init(application):
        async def cron_check():
            await check_deals(application.bot)

        scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
        scheduler.add_job(
            cron_check,
            CronTrigger(hour="10,15", minute="0", day_of_week="mon-fri"),
            name="kp_followup_check",
        )
        scheduler.start()
        log.info("⏰ Cron запущен: будни 10:00 и 15:00 МСК")

    app.post_init = post_init

    # Webhook или polling
    if RAILWAY_PUBLIC_DOMAIN:
        webhook_url = f"https://{RAILWAY_PUBLIC_DOMAIN}/webhook"
        log.info(f"🌐 Webhook: {webhook_url}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="/webhook",
            webhook_url=webhook_url,
        )
    else:
        log.info("📡 Polling mode")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
