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


def _norm(q):
    return " ".join("".join(ch if ch.isalnum() else " " for ch in (q or "").lower().replace("ё", "е")).split())


def build_queue(items):
    """Ответы по одному; «Не найдено» с одинаковым вопросом — одной карточкой: [{..., ids, n}]."""
    out, groups = [], {}
    for it in items:
        if it.get("status") == "Не найдено":
            k = _norm(it["question"])
            if k in groups:
                groups[k]["ids"].append(it["id"])
                groups[k]["n"] += 1
                continue
            it = dict(it, ids=[it["id"]], n=1)
            groups[k] = it
        else:
            it = dict(it, ids=[it["id"]], n=1)
        out.append(it)
    return out


def card_text(it, left):
    if it.get("status") == "Не найдено":
        times = f" (спросили {it['n']} раз)" if it["n"] > 1 else ""
        return (f"📭 Марк не нашёл ответ в документах{times}\n(осталось: {left})\n\n"
                f"Вопрос: {it['question']}\n"
                f"Спросил(а): {it['who'] or '—'}, {_when(it['when'])}")
    ans = it["answer"] if len(it["answer"]) < 2500 else it["answer"][:2500] + "…"
    return (f"🔍 Проверка ответа (осталось: {left})\n\n"
            f"Вопрос: {it['question']}\n"
            f"Спросил(а): {it['who'] or '—'}, {_when(it['when'])}\n\n"
            f"Ответ Марка:\n{ans}\n\n"
            f"Документ: {it['source'] or 'не указан'}")


def card_markup(it):
    pid = it["id"]
    tail = [InlineKeyboardButton("⏭ Позже", callback_data=f"rv:skip:{pid}"),
            InlineKeyboardButton("Закончить", callback_data="rv:stop:-")]
    if it.get("status") == "Не найдено":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🛠 Проработать", callback_data=f"rv:gap:{pid}"),
             InlineKeyboardButton("🗑 Не по теме", callback_data=f"rv:off:{pid}")], tail])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Верно", callback_data=f"rv:ok:{pid}"),
         InlineKeyboardButton("❌ Неверно", callback_data=f"rv:bad:{pid}")], tail])


def _ids(app, pid):
    return app.bot_data.get("rv_groups", {}).get(pid) or [pid]


async def next_card(app, edit=None):
    """Показать следующую карточку. edit — сообщение, которое заменить."""
    skip = app.bot_data.setdefault("rv_skip", set())
    try:
        queue = [i for i in build_queue(await notion_api.review_pending()) if i["id"] not in skip]
    except Exception as e:
        log.warning("Очередь проверки не получена: %s", e)
        queue = None
    if queue is None:
        text, kbd = "Не удалось открыть журнал. Попробуйте ещё раз через минуту.", None
    elif not queue:
        text, kbd = "Все ответы проверены ✅", None
    else:
        app.bot_data["rv_groups"] = {i["id"]: i["ids"] for i in queue}
        app.bot_data["rv_q"] = {i["id"]: i["question"] for i in queue}
        text, kbd = card_text(queue[0], len(queue)), card_markup(queue[0])
    sent = False
    if edit is not None:
        try:
            await edit.edit_message_text(text, reply_markup=kbd)
            sent = True
        except Exception:
            pass
    if not sent:
        await app.bot.send_message(config.OWNER_TELEGRAM_ID, text, reply_markup=kbd)
    if kbd is None:
        await _restore_keyboard(app)


async def _restore_keyboard(app):
    """Когда очередь закончилась, заново показываем кнопки «Меню» и «Помощь» внизу чата."""
    import learning
    try:
        await app.bot.send_message(config.OWNER_TELEGRAM_ID, "Кнопка «📋 Меню» внизу экрана.",
                                   reply_markup=learning.owner_keyboard())
    except Exception as e:
        log.warning("Кнопки меню не показаны: %s", e)


async def start(app):
    app.bot_data["rv_skip"] = set()
    await next_card(app)


async def _write(q, fn):
    try:
        await fn()
        return True
    except Exception as e:
        log.warning("Оценка не записана: %s", e)
        await q.answer("Не удалось записать. Нажмите ещё раз.", show_alert=True)
        return False


async def _ask_text(app, q, pid, kind, prompt):
    app.bot_data["rv_flow"] = {"pid": pid, "kind": kind, "ts": time.time()}
    await q.edit_message_text(prompt, reply_markup=InlineKeyboardMarkup(
        [[InlineKeyboardButton("Без комментария", callback_data=f"rv:nc:{pid}")]]))


async def on_review_button(update, ctx):
    q = update.callback_query
    if not _owner_q(q):
        await q.answer("Это делает владелец.", show_alert=True)
        return
    _, act, pid = q.data.split(":", 2)
    app = ctx.application
    ids = _ids(app, pid)
    if act == "stop":
        await q.answer()
        app.bot_data.pop("rv_flow", None)
        await q.edit_message_text("Проверка остановлена. Продолжить можно в «📋 Меню».")
        await _restore_keyboard(app)
        return
    if act == "skip":
        app.bot_data.setdefault("rv_skip", set()).add(pid)
        await q.answer()
        await next_card(app, edit=q)
        return
    if act == "ok":
        async def f():
            for i in ids:
                await notion_api.review_set(i, "Верно")
        if await _write(q, f):
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
        if await _write(q, lambda: notion_api.review_set(pid, "Неверно", reason=reason)):
            await q.answer("Записано")
            await _ask_text(app, q, pid, "bad",
                            "Напишите одним сообщением, в чём ошибка и как должно быть. Или нажмите «Без комментария».")
        return
    if act in ("gap", "off"):
        question = (app.bot_data.get("rv_q") or {}).get(pid, "")

        async def f():
            for i in ids:
                await notion_api.review_set(i, "Неверно" if act == "gap" else "Верно")
            if question:
                await notion_api.gap_set(question, "Проработать" if act == "gap" else "Оставить как есть")
        if not await _write(q, f):
            return
        await q.answer("Записано")
        if act == "off":
            await next_card(app, edit=q)
        elif act == "gap":
            await _ask_text(app, q, pid, "gap",
                            "Если знаете, как должен звучать ответ, напишите коротко (например: «заезд с животными разрешён, "
                            "доплата 500 ₽»). Это набросок: что и где менять в документах, определим при разборе. "
                            "Или нажмите «Без комментария».")
        return
    if act == "nc":
        await q.answer()
        app.bot_data.pop("rv_flow", None)
        await next_card(app, edit=q)


async def comment_text(update, ctx):
    """Текст владельца как комментарий к проверке. True — обработано."""
    app = ctx.application
    flow = app.bot_data.get("rv_flow")
    u = update.effective_user
    if not flow or not u or u.id != config.OWNER_TELEGRAM_ID:
        return False
    if time.time() - flow["ts"] > 3600:
        app.bot_data.pop("rv_flow", None)
        return False
    app.bot_data.pop("rv_flow", None)
    text = (update.effective_message.text or "").strip()[:1900]
    pid, kind = flow["pid"], flow["kind"]
    try:
        for i in _ids(app, pid):
            await notion_api.review_set(i, "Неверно" if kind == "bad" or kind == "gap" else "Верно", comment=text)
        if kind == "gap":
            q = (app.bot_data.get("rv_q") or {}).get(pid, "")
            if q:
                await notion_api.gap_set_comment(q, text)
        await update.effective_message.reply_text("Записал.")
    except Exception as e:
        log.warning("Комментарий не записан: %s", e)
        await update.effective_message.reply_text("Не удалось записать комментарий. Добавьте его в журнале Notion.")
    await next_card(app)
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
