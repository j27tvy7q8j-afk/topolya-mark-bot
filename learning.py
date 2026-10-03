"""Ознакомление сотрудников с документами (кнопка «Ознакомился») и мини-тесты по документам.
Владелец всё делает кнопками: «📋 Меню» внизу чата (команды /menu, /announce, /quiz_now остаются запасными).
Тест раз в неделю включён по умолчанию (QUIZ_WEEKLY=0 выключает)."""
import asyncio
import json
import logging
import os
import random
import re
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import config
import notion_api

log = logging.getLogger("learning")
TZ = ZoneInfo("Europe/Moscow")
STATE_FILE = os.path.join(config.BASE_DIR, "learning_state.json")
LETTERS = "АБВГ"
QUIZ_QUESTIONS = 3


# ---------- состояние на диске ----------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    for k, v in (("announced", {}), ("quiz_used", []), ("quizzes", {})):
        st.setdefault(k, v)
    return st


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def _is_owner(update):
    u, c = update.effective_user, update.effective_chat
    return bool(u and config.OWNER_TELEGRAM_ID and u.id == config.OWNER_TELEGRAM_ID
                and c and getattr(c.type, "value", c.type) == "private")


def _short_name(name):
    return re.sub(r"\s*\(.*?\)", "", name or "").strip()


MENU_BTN, HELP_BTN = "📋 Меню", "ℹ️ Помощь"


def owner_keyboard():
    from telegram import ReplyKeyboardMarkup
    return ReplyKeyboardMarkup([[MENU_BTN, HELP_BTN]], resize_keyboard=True, is_persistent=True)


def staff_keyboard():
    from telegram import ReplyKeyboardMarkup
    return ReplyKeyboardMarkup([[HELP_BTN]], resize_keyboard=True, is_persistent=True)


def keyboard_for(tg_id):
    return owner_keyboard() if tg_id == config.OWNER_TELEGRAM_ID else staff_keyboard()


CANCEL_ROW = [InlineKeyboardButton("Отмена", callback_data="ancancel")]


async def show_menu(app):
    label = "🔍 Проверить ответы Марка"
    try:
        if config.JOURNAL_DB_ID:
            label += f" ({await notion_api.review_count()})"
    except Exception:
        pass
    kbd = InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data="mn:rev")],
        [InlineKeyboardButton("📣 Разослать сотрудникам об изменении документа", callback_data="mn:ann")],
        [InlineKeyboardButton("📝 Мини-тест по документу (сначала вам)", callback_data="mn:quiz")]])
    await app.bot.send_message(config.OWNER_TELEGRAM_ID, "Что сделать?", reply_markup=kbd)


async def cmd_menu(update, ctx):
    if _is_owner(update):
        await show_menu(ctx.application)


async def on_menu_button(update, ctx):
    q = update.callback_query
    if q.from_user.id != config.OWNER_TELEGRAM_ID:
        await q.answer("Это меню владельца.", show_alert=True)
        return
    await q.answer()
    if q.data == "mn:ann":
        await q.edit_message_text("Выберите документ ниже.")
        await _announce_list(ctx.application)
    elif q.data == "mn:rev":
        import review
        await q.edit_message_text("Открываю проверку ответов…")
        await review.start(ctx.application)
    elif q.data == "mn:quiz":
        await q.edit_message_text("Составляю мини-тест по документу, около минуты…")
        await start_trial(ctx.application)


def _flat(s):
    return re.sub(r"[^\w]+", " ", (s or "").lower().replace("ё", "е")).strip()


# ---------- рассылка «Ознакомился» ----------

def ann_text(title, change, reminder=False):
    head = "Напоминание. Вы ещё не отметили ознакомление." if reminder else "Новая или изменённая редакция документа."
    return (f"📄 {head}\nДокумент: «{title}»\nКоротко: {change}\n\n"
            "Откройте документ кнопкой ниже и прочитайте. Любой вопрос по нему можно задать мне прямо здесь. "
            "Когда ознакомитесь, нажмите «Ознакомился».")


def ack_markup(row_id, done=False):
    open_btn = InlineKeyboardButton("📄 Открыть документ", callback_data="od:" + row_id)
    if done:
        return InlineKeyboardMarkup([[open_btn]])
    return InlineKeyboardMarkup([[open_btn, InlineKeyboardButton("✅ Ознакомился", callback_data="ack:" + row_id)]])


def _chunks_text(text, size=3800):
    out, cur = [], ""
    for line in (text or "").splitlines(keepends=True):
        while len(line) > size:
            if cur:
                out.append(cur); cur = ""
            out.append(line[:size]); line = line[size:]
        if len(cur) + len(line) > size:
            out.append(cur); cur = ""
        cur += line
    if cur.strip():
        out.append(cur)
    return out


async def on_open_doc(update, ctx):
    """Кнопка «Открыть документ»: бот присылает документ сотруднику (PDF; таблицы и запасной вариант — текстом)."""
    import io
    import drive_reader
    q = update.callback_query
    row_id = (q.data or "").split(":", 1)[1]
    try:
        row = await notion_api.ack_get(row_id)
    except Exception as e:
        log.warning("od ack_get %s: %s", row_id, e)
        await q.answer("Не получилось открыть. Нажмите ещё раз через минуту.", show_alert=True)
        return
    if q.from_user.id != row["tg_id"]:
        await q.answer("Эта кнопка не для вас.", show_alert=True)
        return
    kb = ctx.application.bot_data["kb"]
    try:
        doc = next((d for d in await kb.catalog() if d.title == row["doc"]), None)
    except Exception:
        doc = None
    if not doc:
        await q.answer("Документ сейчас недоступен. Обратитесь к администратору.", show_alert=True)
        return
    await q.answer("Отправляю документ…")
    chat_id = q.from_user.id
    if doc.kind == "doc":
        try:
            data = await asyncio.to_thread(drive_reader.export_pdf, doc.file_id)
            f = io.BytesIO(data)
            f.name = re.sub(r"[\\/:*?\"<>|]+", " ", doc.title).strip()[:80] + ".pdf"
            await ctx.application.bot.send_document(chat_id, f, caption=f"«{doc.title}»")
            return
        except Exception as e:
            log.warning("PDF не выгрузился для «%s»: %s", doc.title, e)
    try:
        text = await kb.text(doc)
    except Exception as e:
        log.warning("Текст документа «%s» недоступен: %s", doc.title, e)
        text = ""
    if not text:
        await ctx.application.bot.send_message(chat_id, "Не получилось открыть документ. Обратитесь к администратору.")
        return
    parts = _chunks_text(text)
    for i, part in enumerate(parts):
        head = f"«{doc.title}»\n\n" if i == 0 else ""
        await ctx.application.bot.send_message(chat_id, head + part)


async def _announce_list(app):
    if not config.ACK_DB_ID:
        await app.bot.send_message(config.OWNER_TELEGRAM_ID, "Рассылка пока не настроена. Сообщите разработчику.")
        return
    docs = await app.bot_data["kb"].catalog()
    app.bot_data["an_docs"] = [(d.title, d.file_id) for d in docs]
    rows = [[InlineKeyboardButton(d.title[:60], callback_data=f"an:{i}")] for i, d in enumerate(docs)]
    await app.bot.send_message(config.OWNER_TELEGRAM_ID,
                               "О каком документе сообщить сотрудникам? Выберите из списка (только документы, подключённые к боту).",
                               reply_markup=InlineKeyboardMarkup(rows + [CANCEL_ROW]))


async def cmd_announce(update, ctx):
    if _is_owner(update):
        await _announce_list(ctx.application)


async def cmd_cancel(update, ctx):
    if _is_owner(update):
        ctx.application.bot_data.pop("an_flow", None)
        await update.effective_message.reply_text("Отменено.")


async def on_announce_pick(update, ctx):
    q = update.callback_query
    if q.from_user.id != config.OWNER_TELEGRAM_ID:
        await q.answer("Это делает владелец.", show_alert=True)
        return
    docs = ctx.application.bot_data.get("an_docs") or []
    try:
        title, fid = docs[int(q.data.split(":", 1)[1])]
    except Exception:
        await q.answer("Список устарел: нажмите «📋 Меню» и выберите рассылку заново.", show_alert=True)
        return
    await q.answer()
    await q.edit_message_text(f"Документ: «{title}». Готовлю текст для сотрудников, около минуты…")
    kb = ctx.application.bot_data["kb"]
    draft = ""
    try:
        doc = next((d for d in await kb.catalog() if d.file_id == fid), None)
        text = await kb.text(doc) if doc else ""
        draft = await describe_doc(kb, text) if text else ""
    except Exception as e:
        log.warning("Черновик рассылки не составлен: %s", e)
    st = load_state()
    await _propose(ctx.application, st, title, fid, draft, f"Документ: «{title}».")
    save_state(st)


async def owner_flow_text(update, ctx):
    """Кнопка «📋 Меню» и текст владельца, когда бот ждёт описание изменений. True — сообщение обработано."""
    if _is_owner(update) and (update.effective_message.text or "").strip() == MENU_BTN:
        await show_menu(ctx.application)
        return True
    import review
    if await review.comment_text(update, ctx):
        return True
    flow = ctx.application.bot_data.get("an_flow")
    if not flow or "change" in flow or not _is_owner(update):
        return False
    flow["change"] = (update.effective_message.text or "").strip()[:500]
    try:
        staff = await notion_api.active_staff()
    except Exception as e:
        log.warning("Список сотрудников не получен: %s", e)
        await update.effective_message.reply_text("Не удалось получить список сотрудников. Попробуйте ещё раз через минуту.")
        return True
    flow["n"] = len(staff)
    kbd = InlineKeyboardMarkup([[InlineKeyboardButton(f"Отправить ({len(staff)} чел.)", callback_data="ansend"),
                                 InlineKeyboardButton("Отмена", callback_data="ancancel")]])
    await update.effective_message.reply_text(
        "Так увидят сотрудники:\n\n" + ann_text(flow["title"], flow["change"]), reply_markup=kbd)
    return True


async def send_announcement(app, title, file_id, change):
    """Рассылает и пишет строки в «Ознакомления». Возвращает (отправлено, [не доставлено])."""
    bid = datetime.now(TZ).strftime("%Y%m%d-%H%M")
    sent, failed = 0, []
    for st in await notion_api.active_staff():
        row = None
        try:
            row = await notion_api.ack_create(title, change, st["name"], st["tg_id"], bid)
            await app.bot.send_message(st["tg_id"], ann_text(title, change), reply_markup=ack_markup(row))
            sent += 1
        except Exception as e:
            log.warning("Рассылка не дошла до %s: %s", st["tg_id"], e)
            failed.append(_short_name(st["name"]) or str(st["tg_id"]))
            if row:
                try:
                    await notion_api.ack_archive(row)
                except Exception:
                    pass
        await asyncio.sleep(0.2)
    s = load_state()
    s["announced"][file_id] = datetime.now(TZ).isoformat(timespec="seconds")
    save_state(s)
    return sent, failed


async def on_announce_confirm(update, ctx):
    q = update.callback_query
    if q.from_user.id != config.OWNER_TELEGRAM_ID:
        await q.answer("Это делает владелец.", show_alert=True)
        return
    if q.data == "ancancel":
        ctx.application.bot_data.pop("an_flow", None)
        await q.answer()
        await q.edit_message_text("Хорошо, отменено.")
        return
    flow = ctx.application.bot_data.pop("an_flow", None)
    if not flow or "change" not in flow:
        await q.answer("Рассылка уже обработана или отменена.", show_alert=True)
        return
    await q.answer()
    await q.edit_message_text("Отправляю…")
    sent, failed = await send_announcement(ctx.application, flow["title"], flow["file_id"], flow["change"])
    text = f"Готово: отправлено {sent}."
    if failed:
        text += " Не доставлено: " + ", ".join(failed) + " (пусть откроют бота и нажмут «Старт»)."
    await q.edit_message_text(text)


async def on_ack_button(update, ctx):
    q = update.callback_query
    row_id = (q.data or "").split(":", 1)[1]
    try:
        row = await notion_api.ack_get(row_id)
    except Exception as e:
        log.warning("ack_get %s: %s", row_id, e)
        await q.answer("Не удалось сохранить отметку, нажмите ещё раз через минуту.", show_alert=True)
        return
    if q.from_user.id != row["tg_id"]:
        await q.answer("Эта кнопка не для вас.", show_alert=True)
        return
    if not row["done"]:
        try:
            await notion_api.ack_mark(row_id)
        except Exception as e:
            log.warning("ack_mark %s: %s", row_id, e)
            await q.answer("Не удалось сохранить отметку, нажмите ещё раз через минуту.", show_alert=True)
            return
    await q.answer("Отмечено")
    await q.edit_message_text((q.message.text or "") + f"\n\n✅ Ознакомление отмечено {datetime.now(TZ):%d.%m.%Y %H:%M}.",
                              reply_markup=ack_markup(row_id, done=True))


async def reminder_pass(app, now=None):
    """Напоминания 24 и 48 часов; через 72 часа — сводка владельцу. Только днём (9–20 МСК)."""
    now = now or datetime.now(TZ)
    if not 9 <= now.hour < 20:
        return
    escalate = []
    for r in await notion_api.ack_open():
        if not r["sent"]:
            continue
        age = now - r["sent"]
        n = r["reminders"]
        if n in (0, 1) and age >= timedelta(hours=24 * (n + 1)):
            try:
                await app.bot.send_message(r["tg_id"], ann_text(r["doc"], r["change"], reminder=True),
                                           reply_markup=ack_markup(r["page_id"]))
                await notion_api.ack_set_reminders(r["page_id"], n + 1)
            except Exception as e:
                log.warning("Напоминание не отправлено %s: %s", r["tg_id"], e)
        elif n == 2 and age >= timedelta(hours=72):
            escalate.append(r)
    if escalate and config.OWNER_TELEGRAM_ID:
        by_doc = {}
        for r in escalate:
            by_doc.setdefault(r["doc"], []).append(_short_name(r["name"]) or str(r["tg_id"]))
        text = "Не ознакомились за 3 суток (после двух напоминаний):\n" + "\n".join(
            f"- «{d}»: {', '.join(names)}" for d, names in by_doc.items())
        try:
            await app.bot.send_message(config.OWNER_TELEGRAM_ID, text)
        except Exception:
            log.exception("не удалось отправить сводку об ознакомлении")
            return
        for r in escalate:
            try:
                await notion_api.ack_set_reminders(r["page_id"], 3)
            except Exception:
                pass


async def reminder_loop(app):
    await asyncio.sleep(120)
    while True:
        if config.ACK_DB_ID:
            try:
                await reminder_pass(app)
            except Exception as e:
                log.warning("Напоминания об ознакомлении: %s", e)
        await asyncio.sleep(1800)


# ---------- мини-тесты ----------

QUIZ_SYSTEM = (
    "Ты составляешь проверочные вопросы для сотрудников гостиницы по тексту ОДНОГО документа. "
    f"Составь {QUIZ_QUESTIONS} вопроса, у каждого 4 варианта ответа, один верный. Только по тексту документа: "
    "правила, порядок действий, числа, сроки, обязанности. Неверные варианты должны быть правдоподобными, но "
    "противоречить документу. Не спрашивай про пароли, реквизиты и персональные данные. Для каждого вопроса дай "
    "дословную цитату из документа (10–200 символов), подтверждающую верный ответ, и пояснение в одно предложение. "
    "Ответ — ТОЛЬКО JSON-массив без пояснений и без markdown: "
    '[{"q": "...", "options": ["...", "...", "...", "..."], "correct": 0, "quote": "...", "explain": "..."}]')


def parse_quiz(raw, doc_text):
    """Проверенные вопросы: цитата реально есть в документе, 4 разных варианта, верный индекс. Варианты перемешаны."""
    try:
        data = json.loads(raw[raw.index("["): raw.rindex("]") + 1])
    except Exception:
        return []
    flat_doc = _flat(doc_text)
    out = []
    for it in data if isinstance(data, list) else []:
        try:
            q, opts, corr = str(it["q"]).strip(), [str(o).strip() for o in it["options"]], int(it["correct"])
            quote, expl = _flat(it["quote"]), str(it.get("explain", "")).strip()
        except Exception:
            continue
        if (not q or len(opts) != 4 or len(set(opts)) != 4 or "" in opts or not 0 <= corr < 4
                or len(quote) < 10 or quote not in flat_doc):
            continue
        right = opts[corr]
        random.shuffle(opts)
        out.append({"q": q, "options": opts, "correct": opts.index(right), "explain": expl})
    return out[:QUIZ_QUESTIONS]


def pick_doc(docs, st, now=None):
    """Документ для теста: недавно разосланные, потом ещё не использованные, потом по кругу."""
    now = now or datetime.now(TZ)
    docs = [d for d in docs if d.kind == "doc"]
    used = set(st["quiz_used"])
    recent = [d for d in docs if d.file_id in st["announced"] and d.file_id not in used
              and now - datetime.fromisoformat(st["announced"][d.file_id]) < timedelta(days=14)]
    fresh = [d for d in docs if d.file_id not in used]
    pool = recent or fresh
    if not pool:
        st["quiz_used"] = []
        pool = docs
    return random.choice(pool) if pool else None


async def build_quiz(kb, st):
    for _ in range(4):
        doc = pick_doc(await kb.catalog(), st)
        if not doc:
            break
        text = await kb.text(doc)
        if len(text) < 800:
            st["quiz_used"].append(doc.file_id)
            continue
        raw = await kb.provider.chat(config.ANSWER_MODEL, [
            {"role": "system", "content": QUIZ_SYSTEM},
            {"role": "user", "content": f"Документ «{doc.title}»:\n\n{text[:14000]}"}],
            temperature=0.4, max_tokens=2500, timeout=120)
        qs = parse_quiz(raw, text)
        st["quiz_used"].append(doc.file_id)
        if len(qs) >= 2:
            return doc, qs
    raise RuntimeError("не удалось составить вопросы по документам")


def q_message(quiz, qid, i):
    qq = quiz["questions"][i]
    body = "\n".join(f"{LETTERS[j]}. {o}" for j, o in enumerate(qq["options"]))
    text = f"Вопрос {i + 1} из {len(quiz['questions'])}\n\n{qq['q']}\n\n{body}"
    kbd = InlineKeyboardMarkup([[InlineKeyboardButton(LETTERS[j], callback_data=f"qz:{qid}:{i}:{j}") for j in range(4)]])
    return text, kbd


async def _deliver(app, qid, quiz, targets):
    """Отправляет тест тем из targets, кто его ещё не получил. Возвращает (отправлено, [не доставлено])."""
    sent, failed = 0, []
    for t in targets:
        key = str(t["tg_id"])
        if key in quiz["players"]:
            continue
        try:
            await app.bot.send_message(
                t["tg_id"], f"📝 Мини-тест по документу «{quiz['doc']}»: {len(quiz['questions'])} вопроса, 2 минуты. "
                "Отвечайте кнопками. Результаты видит владелец; цель не оценить вас, а понять, какие места в правилах непонятны.")
            quiz["players"][key] = {"name": t["name"], "answers": {}}
            text, kbd = q_message(quiz, qid, 0)
            await app.bot.send_message(t["tg_id"], text, reply_markup=kbd)
            sent += 1
        except Exception as e:
            log.warning("Тест не дошёл до %s: %s", t["tg_id"], e)
            quiz["players"].pop(key, None)
            failed.append(_short_name(t["name"]) or key)
        await asyncio.sleep(0.2)
    return sent, failed


async def send_quiz(app, kb, targets, trial=False):
    """targets: [{tg_id, name}]. Возвращает (qid, doc, отправлено, [не доставлено])."""
    st = load_state()
    doc, qs = await build_quiz(kb, st)
    qid = uuid.uuid4().hex[:6]
    quiz = {"doc": doc.title, "created": datetime.now(TZ).isoformat(timespec="seconds"),
            "questions": qs, "players": {}, "digest": False, "nudged": False, "trial": trial}
    sent, failed = await _deliver(app, qid, quiz, targets)
    st["quizzes"][qid] = quiz
    save_state(st)
    return qid, doc, sent, failed


async def start_trial(app):
    """Пробный тест только владельцу; после прохождения бот предложит отправить его всем."""
    owner = config.OWNER_TELEGRAM_ID
    try:
        qid, doc, sent, failed = await send_quiz(app, app.bot_data["kb"], [{"tg_id": owner, "name": "Владелец"}], trial=True)
        await app.bot.send_message(owner, f"Тест по документу «{doc.title}» отправлен вам для проверки. Пройдите его, "
                                          "и я предложу отправить такой же тест сотрудникам.")
    except Exception as e:
        log.warning("Пробный тест не составлен: %s", e)
        try:
            await app.bot.send_message(owner, "Не получилось составить тест. Попробуйте ещё раз через несколько минут "
                                              "(кнопка «📋 Меню»). Если повторится, сообщите разработчику.")
        except Exception:
            pass


async def cmd_quiz_now(update, ctx):
    if not _is_owner(update):
        return
    app = ctx.application
    if ctx.args and ctx.args[0].lower() in ("all", "все"):     # запасной путь для разработчика
        try:
            targets = [{"tg_id": s["tg_id"], "name": s["name"]} for s in await notion_api.active_staff()]
            qid, doc, sent, failed = await send_quiz(app, app.bot_data["kb"], targets)
        except Exception as e:
            log.warning("Тест всем не отправлен: %s", e)
            await update.effective_message.reply_text("Не получилось отправить тест. Попробуйте позже.")
            return
        await update.effective_message.reply_text(f"Тест по «{doc.title}» отправлен: {sent}.")
        return
    await update.effective_message.reply_text("Составляю мини-тест по документу, около минуты…")
    await start_trial(app)


async def on_quiz_all(update, ctx):
    """«Отправить всем» / «Не сейчас» после пробного теста."""
    q = update.callback_query
    if q.from_user.id != config.OWNER_TELEGRAM_ID:
        await q.answer("Это решает владелец.", show_alert=True)
        return
    try:
        _, action, qid = q.data.split(":")
    except ValueError:
        await q.answer()
        return
    st = load_state()
    quiz = st["quizzes"].get(qid)
    if not quiz:
        await q.answer("Этот тест уже закрыт.", show_alert=True)
        return
    await q.answer()
    if action == "no":
        await q.edit_message_text("Хорошо, сотрудникам не отправляю.")
        return
    await q.edit_message_text("Отправляю сотрудникам…")
    try:
        staff = await notion_api.active_staff()
        sent, failed = await _deliver(ctx.application, qid, quiz,
                                      [{"tg_id": x["tg_id"], "name": x["name"]} for x in staff])
    except Exception as e:
        log.warning("Тест сотрудникам не отправлен: %s", e)
        await q.edit_message_text("Не получилось отправить тест. Попробуйте позже через «📋 Меню».")
        return
    quiz.update(trial=False, created=datetime.now(TZ).isoformat(timespec="seconds"), nudged=False, digest=False)
    st = load_state()
    st["quizzes"][qid] = quiz
    save_state(st)
    text = f"Готово: тест отправлен {sent} сотрудникам."
    if failed:
        text += " Не получили: " + ", ".join(failed) + " (пусть откроют бота и нажмут «Старт»)."
    text += " Через 2 дня пришлю итоги."
    await q.edit_message_text(text)


async def on_quiz_button(update, ctx):
    q = update.callback_query
    try:
        _, qid, i, j = q.data.split(":")
        i, j = int(i), int(j)
    except Exception:
        await q.answer()
        return
    st = load_state()
    quiz = st["quizzes"].get(qid)
    player = quiz and quiz["players"].get(str(q.from_user.id))
    if not quiz or not player or not 0 <= i < len(quiz["questions"]):
        await q.answer("Этот тест не для вас или уже закрыт.", show_alert=True)
        return
    if str(i) in player["answers"]:
        await q.answer("Вы уже ответили на этот вопрос.")
        return
    player["answers"][str(i)] = j
    save_state(st)
    qq = quiz["questions"][i]
    ok = j == qq["correct"]
    right = f"{LETTERS[qq['correct']]}. {qq['options'][qq['correct']]}"
    res = f"\n\nВаш ответ: {LETTERS[j]}. " + ("Верно ✅" if ok else f"Неверно ❌\nПравильно: {right}")
    if qq["explain"]:
        res += "\n" + qq["explain"]
    await q.answer()
    await q.edit_message_text((q.message.text or "") + res)
    if config.QUIZ_DB_ID:
        try:
            await notion_api.quiz_log(qq["q"], quiz["doc"], player["name"], q.from_user.id,
                                      f"{LETTERS[j]}. {qq['options'][j]}", right, ok, qid)
        except Exception as e:
            log.warning("Результат теста не записан в Notion: %s", e)
    chat_id = q.message.chat_id
    if i + 1 < len(quiz["questions"]):
        text, kbd = q_message(quiz, qid, i + 1)
        await ctx.application.bot.send_message(chat_id, text, reply_markup=kbd)
    else:
        score = sum(1 for k, a in player["answers"].items() if quiz["questions"][int(k)]["correct"] == a)
        await ctx.application.bot.send_message(chat_id, f"Готово: {score} из {len(quiz['questions'])}. Спасибо!")
        if quiz.get("trial") and q.from_user.id == config.OWNER_TELEGRAM_ID and not quiz.get("offered"):
            quiz["offered"] = True
            save_state(st)
            await ctx.application.bot.send_message(
                chat_id, "Это был пробный тест, он пришёл только вам. Отправить такой же тест всем сотрудникам?",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Отправить всем", callback_data=f"qa:all:{qid}"),
                                                    InlineKeyboardButton("Не сейчас", callback_data=f"qa:no:{qid}")]]))


def digest_text(quiz):
    n = len(quiz["questions"])
    lines, wrong = [], [0] * n
    for p in quiz["players"].values():
        a = p["answers"]
        if len(a) < n:
            lines.append(f"- {_short_name(p['name'])}: не прошёл(а)" + (f" (ответов {len(a)} из {n})" if a else ""))
        else:
            sc = sum(1 for k, v in a.items() if quiz["questions"][int(k)]["correct"] == v)
            lines.append(f"- {_short_name(p['name'])}: {sc} из {n}")
        for k, v in a.items():
            if quiz["questions"][int(k)]["correct"] != v:
                wrong[int(k)] += 1
    text = f"Итоги мини-теста по «{quiz['doc']}»:\n" + "\n".join(lines)
    bad = [(w, quiz["questions"][i]["q"]) for i, w in enumerate(wrong) if w]
    if bad:
        text += "\n\nОшибались чаще всего (возможно, место в документе непонятно):\n" + "\n".join(
            f"- {q} ({w} чел.)" for w, q in sorted(bad, reverse=True)[:3])
    return text


async def quiz_pass(app, now=None):
    now = now or datetime.now(TZ)
    st = load_state()
    changed = False
    # еженедельный тест: понедельник с 11:00 МСК
    wk = f"{now.isocalendar().year}-W{now.isocalendar().week}"
    if (config.QUIZ_WEEKLY and now.weekday() == 0 and now.hour >= 11 and st.get("last_week") != wk
            and st.get("week_tries", {}).get(wk, 0) < 3):
        st.setdefault("week_tries", {})[wk] = st.get("week_tries", {}).get(wk, 0) + 1
        save_state(st)
        try:
            targets = [{"tg_id": s["tg_id"], "name": s["name"]} for s in await notion_api.active_staff()]
            qid, doc, sent, failed = await send_quiz(app, app.bot_data["kb"], targets)
            st = load_state()
            st["last_week"] = wk
            changed = True
            if config.OWNER_TELEGRAM_ID:
                await app.bot.send_message(config.OWNER_TELEGRAM_ID,
                    f"Еженедельный тест по «{doc.title}» отправлен: {sent}." + (f" Не доставлено: {', '.join(failed)}." if failed else ""))
        except Exception as e:
            log.warning("Еженедельный тест не отправлен: %s", e)
            if config.OWNER_TELEGRAM_ID:
                try:
                    await app.bot.send_message(config.OWNER_TELEGRAM_ID,
                        "⚠️ Еженедельный тест не отправился: не получилось составить вопросы. Бот попробует ещё раз; "
                        "если не выйдет, отправьте тест вручную через «📋 Меню».")
                except Exception:
                    pass
            st = load_state()
    for qid, quiz in list(st["quizzes"].items()):
        age = now - datetime.fromisoformat(quiz["created"])
        n = len(quiz["questions"])
        if age >= timedelta(hours=24) and not quiz["nudged"] and 9 <= now.hour < 20:
            quiz["nudged"] = changed = True
            for tg, p in quiz["players"].items():
                if len(p["answers"]) < n:
                    try:
                        text, kbd = q_message(quiz, qid, len(p["answers"]))
                        await app.bot.send_message(int(tg), "Напоминание: мини-тест ждёт вас.\n\n" + text, reply_markup=kbd)
                    except Exception as e:
                        log.warning("Напоминание о тесте не отправлено %s: %s", tg, e)
        if age >= timedelta(hours=48) and not quiz["digest"] and config.OWNER_TELEGRAM_ID:
            try:
                await app.bot.send_message(config.OWNER_TELEGRAM_ID, digest_text(quiz))
                quiz["digest"] = changed = True
            except Exception:
                log.exception("не удалось отправить итоги теста")
        if age >= timedelta(days=14):
            del st["quizzes"][qid]
            changed = True
    if changed:
        save_state(st)


async def quiz_loop(app):
    await asyncio.sleep(180)
    while True:
        try:
            await quiz_pass(app)
        except Exception as e:
            log.warning("Цикл тестов: %s", e)
        await asyncio.sleep(300)


# ---------- автоматическое предложение рассылки при изменении документа ----------

BASE_FILE = os.path.join(config.BASE_DIR, "baselines.json")
SETTLE = timedelta(minutes=60)      # документ должен «отстояться» час без новых правок
MIN_CHANGED_WORDS = 3               # опечатки и мелкая правка не повод для рассылки

SUMMARY_SYSTEM = (
    "Ты помогаешь владельцу гостиницы. Даны удалённые и добавленные строки документа после правки. "
    "Опиши для сотрудников в 1–2 коротких предложениях, что изменилось по сути (какие правила, числа, сроки, "
    "обязанности). Только факты из изменений, без вступлений, без оценок и без слов «документ изменён».")


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _hash(text):
    import hashlib
    return hashlib.sha1(_flat(text).encode()).hexdigest()


def changed_words(old, new):
    import difflib
    a, b = _flat(old).split(), _flat(new).split()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal")


def diff_excerpt(old, new, limit=4000):
    import difflib
    lines = difflib.unified_diff([l.strip() for l in old.splitlines() if l.strip()],
                                 [l.strip() for l in new.splitlines() if l.strip()], lineterm="", n=0)
    keep = [l for l in lines if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    return "\n".join(keep)[:limit]


async def summarize_change(kb, old, new):
    try:
        out = await kb.provider.chat(config.ANSWER_MODEL, [
            {"role": "system", "content": SUMMARY_SYSTEM},
            {"role": "user", "content": diff_excerpt(old, new)}], temperature=0.2, max_tokens=300, timeout=60)
        return out.strip()[:400]
    except Exception as e:
        log.warning("Не удалось описать изменение: %s", e)
        return ""


async def _propose(app, st, title, fid, change, header):
    """Предложение владельцу разослать документ: черновик описания + кнопки."""
    pid = uuid.uuid4().hex[:6]
    st.setdefault("proposals", {})[pid] = {"title": title, "file_id": fid, "change": change}
    draft = change or "(описание составить не удалось, напишите его сами)"
    rows = []
    if change:
        rows.append([InlineKeyboardButton("✅ Разослать", callback_data=f"pr:{pid}:ok")])
    rows.append([InlineKeyboardButton("✏️ Своё описание", callback_data=f"pr:{pid}:edit"),
                 InlineKeyboardButton("Не нужно", callback_data=f"pr:{pid}:no")])
    try:
        await app.bot.send_message(config.OWNER_TELEGRAM_ID, f"{header}\n\nЧто увидят сотрудники (черновик):\n{draft}",
                                   reply_markup=InlineKeyboardMarkup(rows))
    except Exception:
        log.exception("не удалось предложить рассылку")


async def describe_doc(kb, text):
    try:
        out = await kb.provider.chat(config.ANSWER_MODEL, [
            {"role": "system", "content": "Опиши в одном-двух коротких предложениях, о чём этот документ гостиницы и "
             "для кого он (какие правила или порядок в нём). Только по тексту, без вступлений."},
            {"role": "user", "content": text[:6000]}], temperature=0.2, max_tokens=250, timeout=60)
        return out.strip()[:400]
    except Exception as e:
        log.warning("Не удалось описать документ: %s", e)
        return ""


async def watch_pass(app, now=None):
    """Раз в 10 минут: если документ изменился и час не правился — предлагает владельцу разослать, с готовым описанием."""
    now = now or datetime.now(TZ)
    kb = app.bot_data["kb"]
    base = _load_json(BASE_FILE, {})
    first_run = not base            # самый первый проход только запоминает текущие версии
    st = load_state()
    pending = st.setdefault("pending", {})
    dirty_base = dirty_state = False
    for d in await kb.catalog():
        if d.kind != "doc":
            continue
        try:
            text = await kb.text(d)
        except Exception:
            text = ""
        if not text:
            if first_run and d.file_id not in base:    # не прочитался при первом проходе: запомнить, что он уже был
                base[d.file_id] = {"hash": "", "text": ""}
                dirty_base = True
            continue
        h = _hash(text)
        b = base.get(d.file_id)
        if b and not b["hash"]:                         # раньше не читался: просто считаем текущую версию исходной
            base[d.file_id] = {"hash": h, "text": text}
            dirty_base = True
            continue
        if not b:
            base[d.file_id] = {"hash": h, "text": text}
            dirty_base = dirty_state = True
            if not first_run and config.OWNER_TELEGRAM_ID and config.ACK_DB_ID:
                # документ появился в списке бота: предлагаем ознакомить сотрудников
                await _propose(app, st, d.title, d.file_id, await describe_doc(kb, text),
                               f"К боту подключён новый документ «{d.title}». Разослать сотрудникам, чтобы ознакомились?")
            continue
        if h == b["hash"]:
            if pending.pop(d.file_id, None):
                dirty_state = True
            continue
        p = pending.get(d.file_id)
        if not p or p["hash"] != h:
            pending[d.file_id] = {"hash": h, "since": now.isoformat(timespec="seconds")}
            dirty_state = True
            continue
        if now - datetime.fromisoformat(p["since"]) < SETTLE:
            continue
        # документ отстоялся: решаем, предлагать ли рассылку
        pending.pop(d.file_id, None)
        dirty_state = True
        old = b["text"]
        base[d.file_id] = {"hash": h, "text": text}
        dirty_base = True
        if changed_words(old, text) < MIN_CHANGED_WORDS or not config.OWNER_TELEGRAM_ID or not config.ACK_DB_ID:
            continue
        await _propose(app, st, d.title, d.file_id, await summarize_change(kb, old, text),
                       f"Документ «{d.title}» изменился. Разослать сотрудникам с кнопкой «Ознакомился»?")
    if dirty_base:
        _save_json(BASE_FILE, base)
    if dirty_state:
        save_state(st)
    # state мог быть переписан внутри (proposals) — save_state выше сохраняет актуальный объект


async def on_proposal_button(update, ctx):
    q = update.callback_query
    if q.from_user.id != config.OWNER_TELEGRAM_ID:
        await q.answer("Это решает владелец.", show_alert=True)
        return
    try:
        _, pid, action = q.data.split(":")
    except ValueError:
        await q.answer()
        return
    st = load_state()
    prop = st.get("proposals", {}).get(pid)
    if not prop:
        await q.answer("Предложение уже обработано.", show_alert=True)
        return
    await q.answer()
    if action == "no":
        st["proposals"].pop(pid, None)
        save_state(st)
        await q.edit_message_text(f"«{prop['title']}»: рассылка не нужна.")
    elif action == "edit":
        st["proposals"].pop(pid, None)
        save_state(st)
        ctx.application.bot_data["an_flow"] = {"title": prop["title"], "file_id": prop["file_id"]}
        await q.edit_message_text(f"«{prop['title']}»: напишите одним-двумя предложениями, что изменилось.",
                                reply_markup=InlineKeyboardMarkup([CANCEL_ROW]))
    elif action == "ok":
        st["proposals"].pop(pid, None)
        save_state(st)
        await q.edit_message_text("Отправляю…")
        sent, failed = await send_announcement(ctx.application, prop["title"], prop["file_id"], prop["change"])
        text = f"«{prop['title']}»: отправлено {sent}."
        if failed:
            text += " Не доставлено: " + ", ".join(failed) + " (пусть откроют бота и нажмут «Старт»)."
        await q.edit_message_text(text)


async def watch_loop(app):
    await asyncio.sleep(420)   # дать прогреть кэш документов
    while True:
        if config.ACK_DB_ID:
            try:
                await watch_pass(app)
            except Exception as e:
                log.warning("Слежение за изменениями документов: %s", e)
        await asyncio.sleep(600)


async def set_menu(app):
    """Список команд у кнопки «/»: только понятные пункты, без служебных команд."""
    try:
        from telegram import BotCommand, BotCommandScopeChat
        base = [BotCommand("start", "Начало"), BotCommand("help", "Что я умею")]
        await app.bot.set_my_commands(base)
        if config.OWNER_TELEGRAM_ID:
            await app.bot.set_my_commands(base + [BotCommand("menu", "Меню: рассылка и тесты")],
                                          scope=BotCommandScopeChat(config.OWNER_TELEGRAM_ID))
    except Exception as e:
        log.warning("Меню команд не установлено: %s", e)


async def startup_check(app):
    """После запуска проверяет доступ к базам рассылок и тестов. Владельцу: один раз «готово» или тревога, если нет доступа."""
    await asyncio.sleep(90)
    bad = []
    for name, db in (("Марк — Ознакомления", config.ACK_DB_ID), ("Марк — Результаты тестов", config.QUIZ_DB_ID)):
        try:
            await notion_api.ping(db)
        except Exception as e:
            log.warning("База «%s» недоступна: %s", name, e)
            bad.append(name)
    if not config.OWNER_TELEGRAM_ID:
        return
    st = load_state()
    if bad:
        try:
            from bot import get_notifier
            await get_notifier(app)("learning-db",
                "рассылки и тесты пока не работают: у бота нет доступа к таблицам в Notion (" + ", ".join(bad) + "). "
                "Что сделать: откройте в Notion страницу «Служебные базы», нажмите «···» → «Подключения» и подключите «Марк». "
                "Или передайте это разработчику.")
        except Exception:
            log.exception("не удалось отправить тревогу о базах")
        return
    if not st.get("ready_notified"):
        try:
            await app.bot.send_message(
                config.OWNER_TELEGRAM_ID,
                "✅ Рассылки и мини-тесты готовы. Внизу появилась кнопка «📋 Меню»: через неё можно сообщить сотрудникам "
                "об изменении документа или отправить мини-тест. Бот сам предложит рассылку, когда документ изменится "
                "или появится новый. Мини-тест сотрудникам уходит автоматически по понедельникам в 11:00.",
                reply_markup=owner_keyboard())
            st["ready_notified"] = True
            save_state(st)
        except Exception:
            log.exception("не удалось отправить уведомление о готовности")
    if not st.get("trial_sent"):
        st["trial_sent"] = True      # один раз: пробный тест только владельцу, чтобы он увидел вопросы
        save_state(st)
        await start_trial(app)
