"""Ознакомление сотрудников с документами (кнопка «Ознакомился») и мини-тесты по документам.
Всё запускает владелец командами /announce и /quiz_now; тест раз в неделю включается QUIZ_WEEKLY=1."""
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


def _flat(s):
    return re.sub(r"[^\w]+", " ", (s or "").lower().replace("ё", "е")).strip()


# ---------- рассылка «Ознакомился» ----------

def ann_text(title, change, reminder=False):
    head = "Напоминание. Вы ещё не отметили ознакомление." if reminder else "Новая или изменённая редакция документа."
    return (f"📄 {head}\nДокумент: «{title}»\nЧто изменилось: {change}\n\n"
            "Прочитать документ можно у администратора, а любой вопрос по нему можно задать мне прямо здесь. "
            "Когда ознакомитесь, нажмите кнопку.")


def ack_markup(row_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Ознакомился", callback_data="ack:" + row_id)]])


async def cmd_announce(update, ctx):
    if not _is_owner(update):
        return
    if not config.ACK_DB_ID:
        await update.effective_message.reply_text("Рассылка не настроена: в .env нет ACK_DB_ID.")
        return
    kb = ctx.application.bot_data["kb"]
    docs = await kb.catalog()
    ctx.application.bot_data["an_docs"] = [(d.title, d.file_id) for d in docs]
    rows = [[InlineKeyboardButton(d.title[:60], callback_data=f"an:{i}")] for i, d in enumerate(docs)]
    await update.effective_message.reply_text("О каком документе разослать? (только документы, подключённые к боту)",
                                              reply_markup=InlineKeyboardMarkup(rows))


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
        await q.answer("Список устарел, вызовите /announce ещё раз.", show_alert=True)
        return
    ctx.application.bot_data["an_flow"] = {"title": title, "file_id": fid}
    await q.answer()
    await q.edit_message_text(f"Документ: «{title}».\nНапишите одним-двумя предложениями, что нового или что "
                              "изменилось: это увидят сотрудники. Для отмены — /cancel.")


async def owner_flow_text(update, ctx):
    """Перехватывает текст владельца, когда бот ждёт описание изменений. True — сообщение обработано."""
    flow = ctx.application.bot_data.get("an_flow")
    if not flow or "change" in flow or not _is_owner(update):
        return False
    flow["change"] = (update.effective_message.text or "").strip()[:500]
    try:
        staff = await notion_api.active_staff()
    except Exception as e:
        await update.effective_message.reply_text(f"Не удалось прочитать список сотрудников в Notion: {e}")
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
    flow = ctx.application.bot_data.pop("an_flow", None)
    if not flow or "change" not in flow:
        await q.answer("Рассылка уже обработана или отменена.", show_alert=True)
        return
    await q.answer()
    if q.data == "ancancel":
        await q.edit_message_text("Рассылка отменена.")
        return
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
    await q.edit_message_text((q.message.text or "") + f"\n\n✅ Ознакомление отмечено {datetime.now(TZ):%d.%m.%Y %H:%M}.")


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


async def send_quiz(app, kb, targets):
    """targets: [{tg_id, name}]. Возвращает (qid, doc, отправлено, [не доставлено])."""
    st = load_state()
    doc, qs = await build_quiz(kb, st)
    qid = uuid.uuid4().hex[:6]
    quiz = {"doc": doc.title, "created": datetime.now(TZ).isoformat(timespec="seconds"),
            "questions": qs, "players": {}, "digest": False, "nudged": False}
    sent, failed = 0, []
    for t in targets:
        try:
            await app.bot.send_message(
                t["tg_id"], f"📝 Мини-тест по документу «{doc.title}»: {len(qs)} вопроса, 2 минуты. Отвечайте кнопками. "
                "Результаты видит владелец; цель не оценить вас, а понять, какие места в правилах непонятны.")
            quiz["players"][str(t["tg_id"])] = {"name": t["name"], "answers": {}}
            text, kbd = q_message(quiz, qid, 0)
            await app.bot.send_message(t["tg_id"], text, reply_markup=kbd)
            sent += 1
        except Exception as e:
            log.warning("Тест не дошёл до %s: %s", t["tg_id"], e)
            quiz["players"].pop(str(t["tg_id"]), None)
            failed.append(_short_name(t["name"]) or str(t["tg_id"]))
        await asyncio.sleep(0.2)
    st["quizzes"][qid] = quiz
    save_state(st)
    return qid, doc, sent, failed


async def cmd_quiz_now(update, ctx):
    if not _is_owner(update):
        return
    kb = ctx.application.bot_data["kb"]
    everyone = bool(ctx.args) and ctx.args[0].lower() in ("all", "все")
    try:
        if everyone:
            targets = [{"tg_id": s["tg_id"], "name": s["name"]} for s in await notion_api.active_staff()]
        else:
            targets = [{"tg_id": config.OWNER_TELEGRAM_ID, "name": "Владелец"}]
        await update.effective_message.reply_text("Составляю вопросы по документу, около минуты…")
        qid, doc, sent, failed = await send_quiz(ctx.application, kb, targets)
    except Exception as e:
        await update.effective_message.reply_text(f"Тест не отправлен: {e}")
        return
    msg = f"Тест по «{doc.title}» отправлен: {sent}."
    if failed:
        msg += " Не доставлено: " + ", ".join(failed) + "."
    if not everyone:
        msg += " Это был пробный тест только вам. Всем: /quiz_now all"
    await update.effective_message.reply_text(msg)


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
                    await app.bot.send_message(config.OWNER_TELEGRAM_ID, f"⚠️ Еженедельный тест не отправлен: {e}")
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


async def watch_pass(app, now=None):
    """Раз в 10 минут: если документ изменился и час не правился — предлагает владельцу разослать, с готовым описанием."""
    now = now or datetime.now(TZ)
    kb = app.bot_data["kb"]
    base = _load_json(BASE_FILE, {})
    st = load_state()
    pending = st.setdefault("pending", {})
    dirty_base = dirty_state = False
    for d in await kb.catalog():
        if d.kind != "doc":
            continue
        try:
            text = await kb.text(d)
        except Exception:
            continue
        if not text:
            continue
        h = _hash(text)
        b = base.get(d.file_id)
        if not b:
            base[d.file_id] = {"hash": h, "text": text}
            dirty_base = True
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
        change = await summarize_change(kb, old, text)
        pid = uuid.uuid4().hex[:6]
        st.setdefault("proposals", {})[pid] = {"title": d.title, "file_id": d.file_id, "change": change}
        dirty_state = True
        draft = change or "(описание составить не удалось, напишите его сами)"
        rows = []
        if change:
            rows.append([InlineKeyboardButton("✅ Разослать", callback_data=f"pr:{pid}:ok")])
        rows.append([InlineKeyboardButton("✏️ Своё описание", callback_data=f"pr:{pid}:edit"),
                     InlineKeyboardButton("Не нужно", callback_data=f"pr:{pid}:no")])
        kbd = InlineKeyboardMarkup(rows)
        try:
            await app.bot.send_message(
                config.OWNER_TELEGRAM_ID,
                f"Документ «{d.title}» изменился. Разослать сотрудникам с кнопкой «Ознакомился»?\n\n"
                f"Что изменилось (черновик):\n{draft}", reply_markup=kbd)
        except Exception:
            log.exception("не удалось предложить рассылку")
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
        await q.edit_message_text(f"«{prop['title']}»: напишите одним-двумя предложениями, что изменилось. Для отмены — /cancel.")
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
    """Меню команд: владелец видит /announce и /quiz_now в списке «/», набирать не нужно."""
    try:
        from telegram import BotCommand, BotCommandScopeChat
        await app.bot.set_my_commands([BotCommand("start", "Начало"), BotCommand("help", "Что я умею")])
        if config.OWNER_TELEGRAM_ID:
            await app.bot.set_my_commands(
                [BotCommand("start", "Начало"), BotCommand("help", "Что я умею"),
                 BotCommand("announce", "Разослать об изменении документа"),
                 BotCommand("quiz_now", "Пробный мини-тест (всем: /quiz_now all)")],
                scope=BotCommandScopeChat(config.OWNER_TELEGRAM_ID))
    except Exception as e:
        log.warning("Меню команд не установлено: %s", e)
