"""Telegram-бот «Марк»: справочник для сотрудников гостиницы «Тополя» (ИИ-помощник)."""
import asyncio
import json
import logging
import os
import re
import time
import httpx
from collections import defaultdict, deque

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ChatType
from telegram.ext import (Application, ApplicationBuilder, CommandHandler, ContextTypes,
                          CallbackQueryHandler, MessageHandler, filters)

import config
import drive_reader
import llm
import notion_api
from kb import KB

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("mark")

TEMP_ERR = "Сейчас не могу проверить информацию, попробуйте через минуту."

START_TEXT = (
    "Здравствуйте, {name}! Я Марк — ИИ-помощник сотрудников гостиницы «Тополя». Я искусственный "
    "интеллект, не человек. Отвечаю на вопросы по правилам, тарифам, инструкциям и чек-листам — "
    "только по документам отеля. Если ответа в документах нет, так и скажу.\n\n"
    "Важно: ваши вопросы и мои ответы записываются в журнал, который видит владелец.\n\n"
    "Просто напишите вопрос. Список тем — /help.")

HELP_TEXT = (
    "Можно спрашивать обычными словами. Темы:\n"
    "- правила проживания, курение, места общего пользования, общение в чатах\n"
    "- тарифы, меню и питание для групп\n"
    "- парковка, Wi-Fi\n"
    "- заселение и оформление гостей, документы гостей, водительское удостоверение\n"
    "- конфликтные ситуации, уведомления коммунальных служб\n"
    "- чек-листы администратора и горничной, правила для персонала, график смен\n"
    "- прейскурант на порчу имущества, аптечка\n\n"
    "В группе зовите меня так: @{bot} ваш вопрос.")

DENY_TEXT = ("Я ИИ-помощник сотрудников гостиницы «Тополя», доступ только для сотрудников. "
             "Чтобы вас добавили, передайте администратору ваш Telegram ID: {id}{uname}.")

GREET_RE = re.compile(r"^\s*(привет|здравствуй(те)?|добрый (день|вечер)|доброе утро|хай|hello|hi)[\s!.,)]*$", re.I)
THANKS_RE = re.compile(r"^\s*(спасибо|благодарю|ок|окей|понятно|ясно|пока|до свидания)[\s!.,)]*$", re.I)


def extract_question(chat_type, text, bot_username):
    """Вопрос из сообщения. В группах — только если бота позвали по имени; иначе None."""
    text = (text or "").strip()
    if chat_type == ChatType.PRIVATE:
        return text or None
    mention = "@" + bot_username
    if mention.lower() not in text.lower():
        return None
    return re.sub(re.escape(mention), "", text, flags=re.I).strip()


class Memory:
    """Память разговора: последние 10 сообщений на сотрудника, сессия 1 час."""

    def __init__(self):
        self.d = {}

    def get(self, uid):
        rec = self.d.get(uid)
        if not rec or time.time() - rec["t"] > config.SESSION_SECONDS:
            self.d[uid] = {"t": time.time(), "m": deque(maxlen=config.HISTORY_MESSAGES)}
            return []
        return list(rec["m"])

    def add(self, uid, question, answer):
        rec = self.d.setdefault(uid, {"t": time.time(), "m": deque(maxlen=config.HISTORY_MESSAGES)})
        rec["m"].append({"role": "user", "content": question})
        rec["m"].append({"role": "assistant", "content": answer})
        rec["t"] = time.time()


def get_notifier(app):
    n = app.bot_data.get("notifier")
    if n is None:
        n = app.bot_data["notifier"] = Notifier(app)
    return n


class Notifier:
    """Тревога владельцу в Telegram (не чаще раза в 30 минут по одному ключу)."""

    def __init__(self, app):
        self.app, self.last = app, {}

    async def __call__(self, key, text):
        if not config.OWNER_TELEGRAM_ID or time.time() - self.last.get(key, 0) < 1800:
            return
        self.last[key] = time.time()
        try:
            await self.app.bot.send_message(config.OWNER_TELEGRAM_ID, "⚠️ Марк: " + text[:800])
        except Exception:
            log.exception("не удалось отправить тревогу владельцу")


memory = Memory()
user_locks = defaultdict(asyncio.Lock)
bg_tasks = set()


def spawn(coro):
    t = asyncio.create_task(coro)
    bg_tasks.add(t)
    t.add_done_callback(bg_tasks.discard)


async def say(update: Update, text):
    for i in range(0, len(text), 4000):
        await update.effective_message.reply_text(text[i:i + 4000])


async def check_access(update: Update):
    """Активный сотрудник из Notion (без кэша) или None — тогда пользователю уже всё объяснили."""
    user = update.effective_user
    try:
        staff = await notion_api.staff_lookup(user.id)
    except Exception:
        log.exception("Проверка доступа не удалась")
        await say(update, TEMP_ERR)
        return None
    if not staff:
        uname = f" (@{user.username})" if user.username else ""
        await say(update, DENY_TEXT.format(id=user.id, uname=uname))
        return None
    return staff


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    staff = await check_access(update)
    if staff:
        await say(update, START_TEXT.format(name=staff["name"] or update.effective_user.first_name or "коллега"))


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await check_access(update):
        await say(update, HELP_TEXT.format(bot=config.BOT_USERNAME))


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        await say(update, "Голосовые сообщения я пока не понимаю. Напишите вопрос текстом.")


async def log_event(staff, user, question, ans):
    name = staff["name"] or user.full_name
    try:
        await notion_api.log_question(question, name, user.id, ans.text, ", ".join(ans.sources), ans.status)
        if ans.status == "Не найдено":
            await notion_api.log_gap(question, name)
    except Exception as e:
        log.warning("Запись в журнал Notion не удалась: %s", e)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    question = extract_question(chat.type, msg.text, config.BOT_USERNAME)
    if question is None:
        return
    if not question:
        await say(update, "Напишите вопрос после упоминания.")
        return
    staff = await check_access(update)
    if not staff:
        return
    if GREET_RE.match(question):
        await say(update, "Здравствуйте! Я Марк, ИИ-помощник отеля. Задайте вопрос по правилам, тарифам или инструкциям.")
        return
    if THANKS_RE.match(question):
        await say(update, "Пожалуйста! Если будут вопросы, пишите.")
        return
    kb: KB = ctx.application.bot_data["kb"]
    async with user_locks[user.id]:
        await chat.send_action(ChatAction.TYPING)
        history = memory.get(user.id)
        try:
            ans = await kb.answer(question, history)
        except Exception as e:
            log.exception("Ответ не получен")
            await get_notifier(ctx.application)("answer", f"не удалось ответить на вопрос ({e})")
            await say(update, TEMP_ERR)
            return
        memory.add(user.id, question, ans.text)
        reply = ans.text + (("\n\nИсточники: " + ", ".join(ans.sources)) if ans.sources else "")
        await say(update, reply)
        spawn(log_event(staff, user, question, ans))


WELCOME = ("Здравствуйте{name}! Вас добавили, теперь можете задавать мне вопросы по документам и правилам "
           "отеля. Я ИИ-помощник и отвечаю только по документам. Нужна помощь — напишите /help")


async def welcome_loop(app):
    """Раз в минуту: приветствие новым сотрудникам из таблицы «Марк — Сотрудники»."""
    last_try = {}
    while True:
        try:
            for st in await notion_api.pending_welcomes():
                if time.time() - last_try.get(st["tg_id"], 0) < 600:
                    continue
                last_try[st["tg_id"]] = time.time()
                short = re.sub(r"\s*\(.*?\)", "", st["name"]).strip()
                try:
                    await app.bot.send_message(st["tg_id"], WELCOME.format(name=", " + short if short else ""))
                except Exception as e:
                    log.warning("Приветствие не доставлено %s: %s", st["tg_id"], e)
                    await get_notifier(app)(f"welcome-{st['tg_id']}",
                        f"не удалось отправить приветствие сотруднику {st['name'] or st['tg_id']}: пусть откроет бота "
                        "@" + config.BOT_USERNAME + " и нажмёт «Старт» (повтор через 10 минут)")
                    continue
                try:
                    await notion_api.mark_welcomed(st["page_id"])
                except Exception as e:
                    log.warning("Не удалось поставить отметку о приветствии: %s", e)
        except Exception as e:
            log.warning("Проверка новых сотрудников не удалась: %s", e)
        await asyncio.sleep(60)


async def ask_connect(app, c):
    """Спрашивает владельца (кнопками), подключать ли новый подписанный документ к боту."""
    if not config.OWNER_TELEGRAM_ID:
        return
    key = c["page_id"]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Подключить к боту", callback_data="add:" + key),
                                InlineKeyboardButton("Не нужно", callback_data="skip:" + key)]])
    try:
        await app.bot.send_message(
            config.OWNER_TELEGRAM_ID,
            f"Новый подписанный документ: «{c['title']}». Подключить к боту, чтобы сотрудники могли "
            "спрашивать по нему?", reply_markup=kb)
    except Exception:
        log.exception("не удалось отправить запрос о подключении документа")


async def on_connect_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or q.from_user.id != config.OWNER_TELEGRAM_ID:
        if q:
            await q.answer("Это решает владелец.", show_alert=True)
        return
    action, _, key = (q.data or "").partition(":")
    try:
        c = await notion_api.doc_card(key)
    except Exception as e:
        await q.answer("Не удалось прочитать карточку в Notion, попробуйте позже.", show_alert=True)
        log.warning("карточка %s: %s", key, e)
        return
    await q.answer()
    if action == "skip":
        await q.edit_message_text(f"«{c['title']}» не подключаю. Если передумаете — отметьте «Для бота» в Notion.")
        return
    fid, kind = drive_reader.parse_url(c.get("url"))
    if not fid:
        await q.edit_message_text(f"«{c['title']}»: в карточке нет ссылки на файл. Добавьте ссылку и отметьте «Для бота» в Notion.")
        return
    if kind == "doc":
        try:
            await asyncio.to_thread(drive_reader.read_document, fid)
        except Exception as e:
            log.warning("нет доступа к новому документу %s: %s", c["title"], e)
            await q.edit_message_text(f"«{c['title']}»: у бота нет доступа к файлу. Откройте его для сервисного аккаунта mark-bot (читатель) и нажмите кнопку ещё раз.", reply_markup=q.message.reply_markup)
            return
    try:
        await notion_api.enable_for_bot(c["page_id"], "владельца")
    except Exception as e:
        log.warning("не удалось подключить документ: %s", e)
        await q.edit_message_text(f"«{c['title']}»: не удалось поставить галочку в Notion ({e}). Отметьте «Для бота» вручную.")
        return
    await q.edit_message_text(f"Готово: «{c['title']}» подключён. Бот увидит его в течение 15 минут.")


SEEN_FILE = os.path.join(config.BASE_DIR, "seen_docs.json")


async def new_docs_loop(app):
    """Раз в 15 минут: новая подписанная карточка без галочки «Для бота» → тревога Михаилу.
    Первый запуск без файла только запоминает текущие карточки (о них решение уже принято)."""
    try:
        seen = set(json.load(open(SEEN_FILE)))
        first = False
    except Exception:
        seen, first = set(), True
    while True:
        try:
            cards = await notion_api.signed_not_in_bot()
            new = [c for c in cards if c["page_id"] not in seen]
            for c in new:
                seen.add(c["page_id"])
                if not first:
                    await ask_connect(app, c)
            if new:
                json.dump(sorted(seen), open(SEEN_FILE, "w"))
            first = False
        except Exception as e:
            log.warning("Проверка новых документов не удалась: %s", e)
        await asyncio.sleep(900)


async def heartbeat_loop(app: Application):
    """Раз в 5 минут «пинг» во внешний мониторинг (healthchecks.io). Если пинги прекратились —
    бот или сервер лежат, и сервис сам пишет владельцу. /fail — бот жив, но документы не обновляются >30 мин."""
    url = config.HEALTHCHECK_URL
    if not url:
        return
    while True:
        try:
            fresh = time.time() - app.bot_data.get("last_ok", 0) < 1800
            async with httpx.AsyncClient(timeout=10) as c:
                await c.get(url if fresh else url.rstrip("/") + "/fail")
        except Exception as e:
            log.warning("Пинг мониторинга не прошёл: %s", e)
        await asyncio.sleep(300)


async def post_init(app: Application):
    async def warm():
        """Прогрев при старте и затем обновление ВСЕХ документов каждые ~10 минут:
        поиск по фрагментам работает по кэшу, он не должен устаревать."""
        while True:
            try:
                kb: KB = app.bot_data["kb"]
                docs = await kb.catalog()
                ok = 0
                for d in docs:
                    try:
                        await kb.text(d, force=True)
                        ok += 1
                    except Exception as e:
                        log.warning("Не обновлён «%s»: %s", d.title, e)
                log.info("Кэш обновлён: %d из %d документов", ok, len(docs))
                if ok:
                    app.bot_data["last_ok"] = time.time()
                    await asyncio.to_thread(kb.save_snapshot)
            except Exception as e:
                log.warning("Обновление кэша не удалось: %s", e)
            await asyncio.sleep(600)
    app.bot_data["notifier"] = Notifier(app)
    app.bot_data["kb"] = KB(llm.get_provider(), app.bot_data["notifier"])
    spawn(warm())
    spawn(heartbeat_loop(app))
    spawn(welcome_loop(app))
    spawn(new_docs_loop(app))


def main():
    miss = config.missing()
    if miss:
        raise SystemExit("В .env не хватает: " + ", ".join(miss))
    app = (ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN)
           .concurrent_updates(True).post_init(post_init).build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(on_connect_button, pattern=r"^(add|skip):"))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Марк запущен: выбор=%s, ответ=%s", config.SELECT_MODEL, config.ANSWER_MODEL)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
