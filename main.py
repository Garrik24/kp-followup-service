"""
KP Follow-Up Bot — Telegram-бот с интерактивными кнопками и AI-анализом.

Три блока проверки (cron будни 10:00 и 15:00 МСК):
1. Новые ответы клиентов → Claude AI анализирует → карточка в Telegram
2. Задачи на сегодня (день решения) → сводка-напоминание в Telegram
3. Сделки без ответа → стандартный дожим → карточка с кнопками
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Конфигурация ─────────────────────────────────────────────────────────────

AMOCRM_SUBDOMAIN = os.environ.get("AMOCRM_SUBDOMAIN", "stavgeo26")
AMOCRM_ACCESS_TOKEN = os.environ.get("AMOCRM_ACCESS_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
MAIL_MCP_URL = os.environ.get("MAIL_MCP_URL", "https://mail-mcp-server-production.up.railway.app/mcp")
PORT = int(os.environ.get("PORT", "8000"))
RAILWAY_PUBLIC_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
PIPELINE_ID = int(os.environ.get("PIPELINE_ID", "3887935"))
STATUS_ID = int(os.environ.get("STATUS_ID", "37270534"))
MSK = timezone(timedelta(hours=3))

FOLLOWUP_TEMPLATES = {
    1: "Добрый день!<br><br>Направляли вам коммерческое предложение по теме «{deal_name}». Хотели уточнить, успели ли ознакомиться?",
    2: "Добрый день!<br><br>Хотели бы уточнить, когда будет проводиться процедура закупки.",
    3: "Добрый день!<br><br>Так и не получили от вас ответа на наше коммерческое предложение. Если у вас есть вопросы, мы готовы на них оперативно ответить.",
}
FOLLOWUP_INTERVALS = {1: 3, 2: 4, 3: 7}

# Маркер в заметке amoCRM — чтобы не анализировать ответ дважды
ANALYZED_MARKER = "[AI-анализ]"


# ── AmoCRM API ───────────────────────────────────────────────────────────────

def amocrm_request(method: str, endpoint: str,
                    params: dict = None, body=None):
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
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        log.error(f"AmoCRM {method} {endpoint}: {e.code}")
        return None
    except Exception as e:
        log.error(f"AmoCRM {method} {endpoint}: {e}")
        return None


def get_leads_in_kp_stage() -> list[dict]:
    result = amocrm_request("GET", "/leads", params={
        "filter[statuses][0][pipeline_id]": PIPELINE_ID,
        "filter[statuses][0][status_id]": STATUS_ID,
        "with": "contacts", "limit": 50,
    })
    return result.get("_embedded", {}).get("leads", []) if result else []


def get_notes(lead_id: int, limit: int = 50) -> list[dict]:
    result = amocrm_request("GET", f"/leads/{lead_id}/notes", params={"limit": limit})
    return result.get("_embedded", {}).get("notes", []) if result else []


def get_contact_info(contact_id: int) -> dict:
    result = amocrm_request("GET", f"/contacts/{contact_id}")
    if not result:
        return {"name": "", "email": "", "phone": ""}
    email, phone = "", ""
    for field in result.get("custom_fields_values", []) or []:
        code = field.get("field_code", "")
        vals = field.get("values", [])
        if code == "EMAIL" and vals:
            email = vals[0].get("value", "")
        if code == "PHONE" and vals:
            phone = vals[0].get("value", "")
    return {"name": result.get("name", ""), "email": email, "phone": phone}


def add_note(lead_id: int, text: str) -> bool:
    result = amocrm_request("POST", f"/leads/{lead_id}/notes", body=[{
        "note_type": "common", "params": {"text": text},
    }])
    return result is not None


def create_task_amocrm(lead_id: int, text: str, due_date: datetime = None, days_from_now: int = 1) -> bool:
    if due_date:
        ts = int(due_date.timestamp())
    else:
        ts = int((datetime.now(MSK) + timedelta(days=days_from_now)).timestamp())
    result = amocrm_request("POST", "/tasks", body=[{
        "text": text, "complete_till": ts,
        "entity_id": lead_id, "entity_type": "leads", "task_type_id": 1,
    }])
    return result is not None


def get_tasks_due_today() -> list[dict]:
    """Получить задачи с дедлайном сегодня."""
    now = datetime.now(MSK)
    start = now.replace(hour=0, minute=0, second=0)
    end = now.replace(hour=23, minute=59, second=59)
    result = amocrm_request("GET", "/tasks", params={
        "filter[updated_at][from]": "",
        "filter[is_completed]": 0,
        "limit": 100,
    })
    if not result:
        return []
    tasks = result.get("_embedded", {}).get("tasks", [])
    # Фильтруем по дедлайну сегодня
    today_tasks = []
    for t in tasks:
        due = t.get("complete_till", 0)
        if due:
            due_dt = datetime.fromtimestamp(due, tz=MSK)
            if start <= due_dt <= end:
                today_tasks.append(t)
    return today_tasks


# ── Claude AI анализ ─────────────────────────────────────────────────────────

def analyze_response_with_claude(deal_name: str, client_name: str,
                                  response_text: str) -> dict:
    """Анализирует ответ клиента через Claude API.

    Возвращает:
      {
        "summary": "Краткое содержание ответа",
        "sentiment": "positive|neutral|negative",
        "has_date": true/false,
        "decision_date": "2026-04-09" или null,
        "days_to_wait": 3,
        "action": "wait|follow_up|close"
      }
    """
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY не настроен — пропуск AI-анализа")
        return {
            "summary": response_text[:200],
            "sentiment": "neutral",
            "has_date": False,
            "decision_date": None,
            "days_to_wait": 3,
            "action": "wait",
        }

    today = datetime.now(MSK).strftime("%Y-%m-%d")
    prompt = f"""Ты помощник менеджера по продажам. Проанализируй ответ клиента на коммерческое предложение.

Сделка: {deal_name}
Клиент: {client_name}
Сегодня: {today}

Ответ клиента:
---
{response_text[:2000]}
---

Верни JSON (без markdown):
{{
  "summary": "краткое содержание ответа на русском, 1-2 предложения",
  "sentiment": "positive" или "neutral" или "negative",
  "has_date": true если клиент назвал конкретный срок/дату,
  "decision_date": "YYYY-MM-DD" дата когда ожидается решение (или null),
  "days_to_wait": число дней до следующего контакта (целое число),
  "action": "wait" (ждать решения) или "follow_up" (нужен дожим) или "close" (клиент отказал)
}}"""

    payload = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 500,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            text = result.get("content", [{}])[0].get("text", "")
            # Извлекаем JSON из ответа
            match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
            if match:
                return json.loads(match.group())
            return {"summary": text[:200], "sentiment": "neutral",
                    "has_date": False, "decision_date": None,
                    "days_to_wait": 3, "action": "wait"}
    except Exception as e:
        log.error(f"Claude API error: {e}")
        return {"summary": response_text[:200], "sentiment": "neutral",
                "has_date": False, "decision_date": None,
                "days_to_wait": 3, "action": "wait"}


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
                return inner.get("status") == "sent"
            return not result.get("error")
    except Exception as e:
        log.error(f"Mail send error: {e}")
        return False


# ── Telegram callback ────────────────────────────────────────────────────────

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Проверяю сделки...")
    await run_all_checks(context.bot)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(MSK).strftime("%Y-%m-%d %H:%M")
    await update.message.reply_text(
        f"🤖 <b>KP Follow-Up Bot</b>\n⏰ {now} МСК\n📊 Cron: будни 10:00 и 15:00\n🔧 Работает",
        parse_mode="HTML")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":", 4)
    action, lead_id = parts[0], int(parts[1])
    email = parts[2] if len(parts) > 2 else ""
    followup_num = int(parts[3]) if len(parts) > 3 else 1

    lead_info = amocrm_request("GET", f"/leads/{lead_id}")
    deal_name = lead_info.get("name", "Сделка") if lead_info else "Сделка"
    orig_text = query.message.text_html or query.message.text or ""

    if action == "send_followup":
        template = FOLLOWUP_TEMPLATES.get(followup_num, FOLLOWUP_TEMPLATES[1])
        body = template.format(deal_name=deal_name)
        if send_email(to=email, subject=f"Re: {deal_name}", body=body):
            add_note(lead_id, f"Автодожим: отправлен follow-up #{followup_num} по email на {email}.")
            await query.edit_message_text(orig_text + f"\n\n✅ <b>Письмо отправлено на {email}</b>", parse_mode="HTML")
        else:
            await query.edit_message_text(orig_text + "\n\n❌ <b>Ошибка отправки</b>", parse_mode="HTML")

    elif action == "send_decision_request":
        body = (f"Добрый день!<br><br>Хотели уточнить, удалось ли принять решение "
                f"по нашему коммерческому предложению по теме «{deal_name}»?")
        if send_email(to=email, subject=f"Re: {deal_name}", body=body):
            add_note(lead_id, f"Автодожим: запрос решения отправлен на {email}.")
            await query.edit_message_text(orig_text + f"\n\n✅ <b>Запрос решения отправлен на {email}</b>", parse_mode="HTML")
        else:
            await query.edit_message_text(orig_text + "\n\n❌ <b>Ошибка отправки</b>", parse_mode="HTML")

    elif action.startswith("task_call_"):
        days = int(action.split("_")[-1])
        create_task_amocrm(lead_id, f"Перезвонить по КП: {deal_name}", days_from_now=days)
        add_note(lead_id, f"Автодожим: задача «перезвонить через {days} дн.» создана.")
        await query.edit_message_text(orig_text + f"\n\n📞 <b>Задача: перезвонить через {days} дн.</b>", parse_mode="HTML")

    elif action == "remind":
        days = int(parts[3]) if len(parts) > 3 else 3
        create_task_amocrm(lead_id, f"Напомнить по КП: {deal_name}", days_from_now=days)
        add_note(lead_id, f"Автодожим: напоминание через {days} дн. создано.")
        await query.edit_message_text(orig_text + f"\n\n📅 <b>Напоминание через {days} дн.</b>", parse_mode="HTML")

    elif action == "close_deal":
        amocrm_request("PATCH", f"/leads/{lead_id}", body={"status_id": 143})
        add_note(lead_id, "Автодожим: сделка закрыта (не реализовано).")
        await query.edit_message_text(orig_text + "\n\n❌ <b>Сделка закрыта</b>", parse_mode="HTML")

    elif action == "skip":
        await query.edit_message_text(orig_text + "\n\n⏸ <b>Пропущено</b>", parse_mode="HTML")


# ── Блок 1: Новые ответы клиентов ────────────────────────────────────────────

async def check_new_responses(bot):
    """Ищем новые входящие, которые ещё не анализировали."""
    log.info("📨 Блок 1: Проверка новых ответов клиентов")
    leads = get_leads_in_kp_stage()
    count = 0

    for lead in leads:
        lead_id = lead["id"]
        deal_name = lead.get("name", "Без названия")
        price = lead.get("price", 0) or 0
        contacts = lead.get("_embedded", {}).get("contacts", [])
        if not contacts:
            continue

        contact = get_contact_info(contacts[0]["id"])
        notes = get_notes(lead_id)

        # Ищем входящие, которые ещё не анализировали
        already_analyzed_ids = set()
        for n in notes:
            params = n.get("params", {})
            text = params.get("text", "") if isinstance(params, dict) else ""
            if ANALYZED_MARKER in text:
                # Извлекаем ID проанализированной заметки из маркера
                match = re.search(r'note_id:(\d+)', text)
                if match:
                    already_analyzed_ids.add(match.group(1))

        for note in notes:
            note_type = note.get("note_type", "")
            if note_type not in ("incoming_mail_message", "incoming_chat_message"):
                continue

            note_id = str(note.get("id", ""))
            if note_id in already_analyzed_ids:
                continue

            # Новый неанализированный ответ!
            note_params = note.get("params", {})
            if isinstance(note_params, dict):
                response_text = note_params.get("text", "") or note_params.get("body", "")
            else:
                response_text = str(note_params)

            if not response_text or len(response_text) < 5:
                continue

            note_date = datetime.fromtimestamp(note.get("created_at", 0), tz=MSK)

            # Анализируем через Claude
            analysis = analyze_response_with_claude(deal_name, contact["name"], response_text)

            # Создаём задачу если есть дата решения
            decision_date_str = ""
            if analysis.get("decision_date"):
                try:
                    decision_dt = datetime.strptime(analysis["decision_date"], "%Y-%m-%d").replace(tzinfo=MSK)
                    create_task_amocrm(lead_id, f"День решения по КП: {deal_name}", due_date=decision_dt)
                    decision_date_str = analysis["decision_date"]
                except ValueError:
                    pass

            if not decision_date_str and analysis.get("days_to_wait"):
                days = analysis["days_to_wait"]
                create_task_amocrm(lead_id, f"Проверить решение по КП: {deal_name}", days_from_now=days)
                decision_dt = datetime.now(MSK) + timedelta(days=days)
                decision_date_str = decision_dt.strftime("%Y-%m-%d")

            # Маркируем как проанализированное
            sentiment_emoji = {"positive": "🟢", "neutral": "🟡", "negative": "🔴"}.get(analysis.get("sentiment", ""), "🟡")
            add_note(lead_id,
                f"{ANALYZED_MARKER} note_id:{note_id}\n"
                f"{sentiment_emoji} {analysis.get('summary', '')}\n"
                f"Дата решения: {decision_date_str or 'не указана'}\n"
                f"Действие: {analysis.get('action', 'wait')}"
            )

            # Карточка в Telegram
            text = (
                f"💬 <b>Ответ от клиента</b>\n"
                f"──────────────\n"
                f"📁 <b>Сделка:</b> {deal_name} (#{lead_id})\n"
                f"💰 <b>Бюджет:</b> {price:,} ₽\n"
                f"👤 <b>Контакт:</b> {contact['name']}"
            )
            if contact["phone"]:
                text += f", тел. {contact['phone']}"
            text += (
                f"\n📧 <b>Email:</b> {contact['email'] or '—'}\n"
                f"📅 <b>Дата ответа:</b> {note_date.strftime('%d.%m.%Y %H:%M')}\n"
                f"──────────────\n"
                f"{sentiment_emoji} <b>Суть:</b> {analysis.get('summary', '—')}\n"
            )
            if decision_date_str:
                text += f"📅 <b>Ожидаемое решение:</b> {decision_date_str}\n"
                text += "✅ Задача в amoCRM создана\n"

            days_wait = analysis.get("days_to_wait", 3)
            buttons = []
            if decision_date_str:
                buttons.append([InlineKeyboardButton(
                    f"📅 Напомнить {decision_date_str}",
                    callback_data=f"remind:{lead_id}:{contact['email']}:{days_wait}")])
            buttons.append([
                InlineKeyboardButton("📅 +3 дня", callback_data=f"remind:{lead_id}:{contact['email']}:3"),
                InlineKeyboardButton("📅 +7 дней", callback_data=f"remind:{lead_id}:{contact['email']}:7"),
            ])
            buttons.append([
                InlineKeyboardButton("⏸ Оставить", callback_data=f"skip:{lead_id}"),
            ])

            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text,
                                   parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
            count += 1
            await asyncio.sleep(0.5)

    log.info(f"   Новых ответов обработано: {count}")


# ── Блок 2: Задачи на сегодня ────────────────────────────────────────────────

async def check_tasks_due_today(bot):
    """Проверяем задачи с дедлайном сегодня по сделкам в КП."""
    log.info("🔔 Блок 2: Задачи на сегодня (день решения)")
    tasks = get_tasks_due_today()
    kp_leads = {l["id"]: l for l in get_leads_in_kp_stage()}
    count = 0

    for task in tasks:
        entity_id = task.get("entity_id")
        if entity_id not in kp_leads:
            continue

        lead = kp_leads[entity_id]
        lead_id = lead["id"]
        deal_name = lead.get("name", "Без названия")
        price = lead.get("price", 0) or 0

        contacts = lead.get("_embedded", {}).get("contacts", [])
        contact = get_contact_info(contacts[0]["id"]) if contacts else {"name": "", "email": "", "phone": ""}

        # Найдём последний AI-анализ из заметок
        notes = get_notes(lead_id)
        last_analysis = ""
        last_response_date = ""
        for n in reversed(notes):
            params = n.get("params", {})
            text = params.get("text", "") if isinstance(params, dict) else ""
            if ANALYZED_MARKER in text:
                last_analysis = text.replace(ANALYZED_MARKER, "").strip()
                break

        # Считаем дожимы
        followup_count = sum(1 for n in notes
                              if isinstance(n.get("params"), dict)
                              and "Автодожим:" in n["params"].get("text", ""))

        text = (
            f"🔔 <b>Сегодня день решения!</b>\n"
            f"──────────────\n"
            f"📁 <b>Сделка:</b> {deal_name} (#{lead_id})\n"
            f"💰 <b>Бюджет:</b> {price:,} ₽\n"
            f"👤 <b>Контакт:</b> {contact['name']}"
        )
        if contact["phone"]:
            text += f"\n📞 <b>Тел:</b> {contact['phone']}"
        if contact["email"]:
            text += f"\n📧 <b>Email:</b> {contact['email']}"
        text += f"\n📝 <b>Задача:</b> {task.get('text', '')}\n"
        if last_analysis:
            text += f"──────────────\n📋 <b>Последний анализ:</b>\n{last_analysis}\n"
        text += f"📊 <b>Дожимов было:</b> {followup_count} из 3"

        buttons = []
        if contact["email"]:
            buttons.append([InlineKeyboardButton(
                "✉️ Запросить решение",
                callback_data=f"send_decision_request:{lead_id}:{contact['email']}")])
        buttons.append([
            InlineKeyboardButton("📞 Перезвонить", callback_data=f"task_call_1:{lead_id}:{contact['email']}:1"),
        ])
        buttons.append([
            InlineKeyboardButton("📅 +3 дня", callback_data=f"remind:{lead_id}:{contact['email']}:3"),
            InlineKeyboardButton("📅 +7 дней", callback_data=f"remind:{lead_id}:{contact['email']}:7"),
        ])
        buttons.append([
            InlineKeyboardButton("❌ Закрыть", callback_data=f"close_deal:{lead_id}"),
            InlineKeyboardButton("⏸ Оставить", callback_data=f"skip:{lead_id}"),
        ])

        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text,
                               parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
        count += 1
        await asyncio.sleep(0.5)

    log.info(f"   Задач на сегодня: {count}")


# ── Блок 3: Стандартный дожим ────────────────────────────────────────────────

async def check_followups(bot):
    """Сделки без ответа → дожим по расписанию."""
    log.info("✉️ Блок 3: Стандартный дожим")
    leads = get_leads_in_kp_stage()
    count = 0
    now = datetime.now(MSK)

    for lead in leads:
        lead_id = lead["id"]
        deal_name = lead.get("name", "Без названия")
        price = lead.get("price", 0) or 0
        contacts = lead.get("_embedded", {}).get("contacts", [])
        if not contacts:
            continue

        contact = get_contact_info(contacts[0]["id"])
        if not contact["email"]:
            continue

        notes = get_notes(lead_id)

        # Считаем дожимы и проверяем ответы
        followup_notes = [n for n in notes
                          if isinstance(n.get("params"), dict)
                          and "Автодожим:" in n["params"].get("text", "")]
        existing_count = len(followup_notes)
        if existing_count >= 3:
            continue

        # Если был ответ за последние 2 дня — пропуск
        two_days_ago = now - timedelta(days=2)
        has_recent_reply = any(
            n.get("note_type") in ("incoming_mail_message", "incoming_chat_message")
            and datetime.fromtimestamp(n.get("created_at", 0), tz=MSK) >= two_days_ago
            for n in notes
        )
        if has_recent_reply:
            continue

        # Дата последнего действия
        if followup_notes:
            last_ts = max(n.get("created_at", 0) for n in followup_notes)
        else:
            last_ts = lead.get("updated_at", lead.get("created_at", 0))
        last_date = datetime.fromtimestamp(last_ts, tz=MSK)

        # Не отправляли ли уже сегодня
        if last_date.strftime("%Y-%m-%d") == now.strftime("%Y-%m-%d"):
            continue

        days_since = (now - last_date).days
        next_num = existing_count + 1
        required = FOLLOWUP_INTERVALS.get(next_num, 999)
        if days_since < required:
            continue

        # Карточка в Telegram
        text = (
            f"✉️ <b>Дожим #{next_num}</b>\n"
            f"──────────────\n"
            f"📁 <b>Сделка:</b> {deal_name} (#{lead_id})\n"
            f"💰 <b>Бюджет:</b> {price:,} ₽\n"
            f"👤 <b>Контакт:</b> {contact['name']}"
        )
        if contact["phone"]:
            text += f", тел. {contact['phone']}"
        text += (
            f"\n📧 <b>Email:</b> {contact['email']}\n"
            f"📅 <b>Без ответа:</b> {days_since} дней\n"
            f"📝 <b>Дожим:</b> #{next_num} из 3"
        )

        buttons = [
            [InlineKeyboardButton(f"✉️ Отправить дожим #{next_num}",
                callback_data=f"send_followup:{lead_id}:{contact['email']}:{next_num}")],
            [InlineKeyboardButton("📞 Завтра", callback_data=f"task_call_1:{lead_id}:{contact['email']}"),
             InlineKeyboardButton("📞 3 дня", callback_data=f"task_call_3:{lead_id}:{contact['email']}"),
             InlineKeyboardButton("📞 7 дней", callback_data=f"task_call_7:{lead_id}:{contact['email']}")],
            [InlineKeyboardButton("❌ Закрыть", callback_data=f"close_deal:{lead_id}"),
             InlineKeyboardButton("⏸ Оставить", callback_data=f"skip:{lead_id}")],
        ]

        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text,
                               parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
        count += 1
        await asyncio.sleep(0.5)

    log.info(f"   Дожимов предложено: {count}")


# ── Запуск всех проверок ─────────────────────────────────────────────────────

async def run_all_checks(bot):
    now_msk = datetime.now(MSK)
    log.info(f"{'='*50}")
    log.info(f"🚀 Проверка: {now_msk.strftime('%Y-%m-%d %H:%M')} МСК")
    log.info(f"{'='*50}")
    await check_new_responses(bot)
    await check_tasks_due_today(bot)
    await check_followups(bot)
    log.info(f"{'='*50}")
    log.info("✅ Все проверки завершены")


# ── Запуск ───────────────────────────────────────────────────────────────────

def main():
    log.info("🚀 Запуск KP Follow-Up Bot")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Запускаем post_init для cron
    async def post_init(application: Application):
        scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
        scheduler.add_job(
            lambda: asyncio.ensure_future(run_all_checks(application.bot)),
            CronTrigger(hour="10,15", minute="0", day_of_week="mon-fri"),
            name="kp_check",
        )
        scheduler.start()
        log.info("⏰ Cron: будни 10:00, 15:00 МСК")

    app.post_init = post_init

    if RAILWAY_PUBLIC_DOMAIN:
        webhook_url = f"https://{RAILWAY_PUBLIC_DOMAIN}/webhook"
        log.info(f"🌐 Webhook: {webhook_url}")
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path="/webhook",
                        webhook_url=webhook_url, drop_pending_updates=True)
    else:
        log.info("📡 Polling mode")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
