"""Проверка ответов бота владельцем: кнопка «🔍 Проверить ответы» в меню, карточки по одной,
«✅ Верно» / «❌ Неверно» + причина кнопкой + комментарий. Модель не вызывается, токены не тратятся."""
import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import config
import notion_api

log = logging.getLogger("review")
TZ = ZoneInfo("Europe/Moscow")
REASONS = notion_api.REVIEW_REASONS


def _owner_q(q):
    return q.from_user.id == config.OWNER_TELEGRAM_ID


def _when(s):
    try:
        return datetime.fromisoformat(s).astimezone(TZ).strftime("%d.%m %H:%M")
    except Exception:
        return s[:10]


def card_text(it, left):
    ans = it["answer"] if len(it["answer"]) < 2500 else it["answer"][:2500] + "…"
    return (f"🔍 Проверка ответа (осталось: {left})\n\n"
            f"Вопрос: {it['question']}\n"
            f"Спросил(а): {it['who'] or '—'}, {_when(it['when'])}\n\n"
            f"Ответ Марка:\n{ans}\n\n"
            f"Документ: {it['source'] or 'не указан'}")


def card_markup(pid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Верно", callback_data=f"rv:ok:{pid}"),
         InlineKeyboardButton("❌ Неверно", callback_data=f"rv:bad:{pid}")],
        [InlineKeyboardButton("⏭ Позже", callback_data=f"rv:skip:{pid}"),
         InlineKeyboardButton("Закончить", callback_data="rv:stop:-")]])


async def next_card(app, edit=None):
    """Показать следующую карточку. edit — сообщение, которое заменить."""
    skip = app.bot_data.setdefault("rv_skip", set())
    try:
        items = [i for i in await notion_api.review_pending() if i["id"] not in skip]
    except Exception as e:
        log.warning("Очередь проверки не получена: %s", e)
        items = None
    if items is None:
        text, kbd = "Не удалось открыть журнал. Попробуйте ещё раз через минуту.", None
    elif not items:
        text, kbd = "Все ответы проверены ✅", None
    else:
        text, kbd = card_text(items[0], len(items)), card_markup(items[0]["id"])
    if edit is not None:
        try:
            await edit.edit_message_text(text, reply_markup=kbd)
            return
        except Exception:
            pass
    await app.bot.send_message(config.OWNER_TELEGRAM_ID, text, reply_markup=kbd)


async def start(app):
    app.bot_data["rv_skip"] = set()
    await next_card(app)


async def on_review_button(update, ctx):
    q = update.callback_query
    if not _owner_q(q):
        await q.answer("Это делает владелец.", show_alert=True)
        return
    _, act, pid = q.data.split(":", 2)
    app = ctx.application
    if act == "stop":
        await q.answer()
        app.bot_data.pop("rv_flow", None)
        await q.edit_message_text("Проверка остановлена. Продолжить можно в «📋 Меню».")
        return
    if act == "skip":
        app.bot_data.setdefault("rv_skip", set()).add(pid)
        await q.answer()
        await next_card(app, edit=q)
        return
    if act == "ok":
        try:
            await notion_api.review_set(pid, "Верно")
        except Exception as e:
            log.warning("Оценка не записана: %s", e)
            await q.answer("Не удалось записать. Нажмите ещё раз.", show_alert=True)
            return
        await q.answer("Записано")
        await next_card(app, edit=q)
        return
    if act == "bad":
        await q.answer()
        rows = [[InlineKeyboardButton(r, callback_data=f"rv:r{i}:{pid}")] for i, r in enumerate(REASONS)]
        rows.append([InlineKeyboardButton("Другая причина", callback_data=f"rv:r9:{pid}")])
        await q.edit_message_text("Что не так с ответом?\n(Это определит, кому передать исправление.)",
                                  reply_markup=InlineKeyboardMarkup(rows))
        return
    if act.startswith("r") and act[1:].isdigit():
        i = int(act[1:])
        reason = REASONS[i] if i < len(REASONS) else None
        try:
            await notion_api.review_set(pid, "Неверно", reason=reason)
        except Exception as e:
            log.warning("Оценка не записана: %s", e)
            await q.answer("Не удалось записать. Нажмите ещё раз.", show_alert=True)
            return
        await q.answer("Записано")
        app.bot_data["rv_flow"] = {"pid": pid, "ts": time.time()}
        await q.edit_message_text(
            "Напишите одним сообщением, в чём ошибка и как должно быть. Или нажмите «Без комментария».",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Без комментария", callback_data=f"rv:nc:{pid}")]]))
        return
    if act == "nc":
        await q.answer()
        app.bot_data.pop("rv_flow", None)
        await next_card(app, edit=q)


async def comment_text(update, ctx):
    """Текст владельца как комментарий к неверному ответу. True — обработано."""
    flow = ctx.application.bot_data.get("rv_flow")
    u = update.effective_user
    if not flow or not u or u.id != config.OWNER_TELEGRAM_ID:
        return False
    if time.time() - flow["ts"] > 3600:
        ctx.application.bot_data.pop("rv_flow", None)
        return False
    ctx.application.bot_data.pop("rv_flow", None)
    try:
        await notion_api.review_set(flow["pid"], "Неверно", comment=(update.effective_message.text or "").strip()[:1900])
        await update.effective_message.reply_text("Записал.")
    except Exception as e:
        log.warning("Комментарий не записан: %s", e)
        await update.effective_message.reply_text("Не удалось записать комментарий. Добавьте его в журнале Notion.")
    await next_card(ctx.application)
    return True


async def nudge_pass(app):
    """Раз в неделю (вс после 18:00 МСК): сколько ответов ждёт проверки."""
    import learning
    now = datetime.now(TZ)
    if now.weekday() != 6 or now.hour < 18:
        return
    st = learning.load_state()
    key = now.strftime("%G-W%V")
    if st.get("rv_nudged") == key:
        return
    n = await notion_api.review_count()
    st["rv_nudged"] = key
    learning.save_state(st)
    if n:
        await app.bot.send_message(
            config.OWNER_TELEGRAM_ID, f"🔍 Ответов ждёт вашей проверки: {n}.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Проверить ответы", callback_data="mn:rev")]]))


async def nudge_loop(app):
    await asyncio.sleep(120)
    while True:
        try:
            if config.JOURNAL_DB_ID:
                await nudge_pass(app)
        except Exception as e:
            log.warning("Напоминание о проверке не отправлено: %s", e)
        await asyncio.sleep(600)
