"""Работа с Notion: реестр документов, список сотрудников, журнал, пробелы."""
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

import config

log = logging.getLogger("notion")
API = "https://api.notion.com/v1"
TZ = ZoneInfo("Europe/Moscow")


def _headers():
    return {"Authorization": f"Bearer {config.NOTION_TOKEN}", "Notion-Version": "2022-06-28",
            "Content-Type": "application/json"}


async def _req(method, path, body=None):
    last = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.request(method, API + path, headers=_headers(), json=body)
        except httpx.HTTPError as e:
            last = RuntimeError(f"Notion сеть: {e}")
        else:
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                last = RuntimeError(f"Notion HTTP {r.status_code}")
            else:
                raise RuntimeError(f"Notion HTTP {r.status_code}: {r.text[:300]}")
        await asyncio.sleep(1.5 * (attempt + 1))
    raise last


async def query_all(db_id, flt=None, limit=None):
    out, cursor = [], None
    while True:
        body = {"page_size": 100}
        if flt:
            body["filter"] = flt
        if cursor:
            body["start_cursor"] = cursor
        data = await _req("POST", f"/databases/{db_id}/query", body)
        out += data["results"]
        if limit and len(out) >= limit:
            return out[:limit]
        if not data.get("has_more"):
            return out
        cursor = data["next_cursor"]


def _text(prop):
    if not prop:
        return ""
    t = prop.get("type")
    if t in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in prop.get(t, []))
    if t == "url":
        return prop.get("url") or ""
    if t in ("select", "status"):
        return (prop.get(t) or {}).get("name", "")
    return ""


async def docs_registry():
    """Карточки с галочкой «Для бота»: [{page_id, title, url, comment}]."""
    pages = await query_all(config.DOCS_DB_ID,
                            {"property": "Для бота", "checkbox": {"equals": True}})
    docs = []
    for p in pages:
        props = p["properties"]
        title = next((_text(v) for v in props.values() if v.get("type") == "title"), "")
        docs.append({"page_id": p["id"], "title": title.strip(),
                     "url": _text(props.get("Ссылка на файл")),
                     "comment": _text(props.get("Комментарий"))})
    return docs


async def signed_not_in_bot():
    """Карточки «Документы» со статусом «Подписан» без галочки «Для бота»: [{page_id, title}]."""
    pages = await query_all(config.DOCS_DB_ID, {"property": "Для бота", "checkbox": {"equals": False}})
    out = []
    for p in pages:
        props = p["properties"]
        if _text(props.get("Статус")).strip().lower() != "подписан":
            continue
        title = next((_text(v) for v in props.values() if v.get("type") == "title"), "").strip()
        if not title or title.lower().startswith("архив"):
            continue
        out.append({"page_id": p["id"], "title": title, "url": _text(props.get("Ссылка на файл"))})
    return out


async def doc_card(page_id):
    page = await _req("GET", f"/pages/{page_id}")
    props = page["properties"]
    title = next((_text(v) for v in props.values() if v.get("type") == "title"), "").strip()
    return {"page_id": page_id, "title": title, "url": _text(props.get("Ссылка на файл"))}


async def enable_for_bot(page_id, who):
    """Ставит «Для бота» и дописывает в «Комментарий» пометку о подключении."""
    page = await _req("GET", f"/pages/{page_id}")
    old = _text(page["properties"].get("Комментарий"))
    note = f"Подключён к боту {datetime.now(TZ):%d.%m.%Y} по подтверждению {who}."
    props = {"Для бота": {"checkbox": True}, "Комментарий": _rt((old + " " + note).strip())}
    await _req("PATCH", f"/pages/{page_id}", {"properties": props})


async def staff_lookup(tg_id):
    """Активный сотрудник по Telegram ID (без кэша). None — доступа нет."""
    flt = {"and": [{"property": "Telegram ID", "number": {"equals": tg_id}},
                   {"property": "Статус", "select": {"equals": "Активен"}}]}
    rows = await query_all(config.STAFF_DB_ID, flt, limit=1)
    if not rows:
        return None
    return {"page_id": rows[0]["id"], "name": _text(rows[0]["properties"].get("Имя")).strip()}


async def pending_welcomes():
    """Активные сотрудники с Telegram ID, которым ещё не отправлено приветствие."""
    flt = {"and": [{"property": "Статус", "select": {"equals": "Активен"}},
                   {"property": "Telegram ID", "number": {"is_not_empty": True}},
                   {"property": "Приветствие отправлено", "checkbox": {"equals": False}}]}
    rows = await query_all(config.STAFF_DB_ID, flt)
    out = []
    for r in rows:
        pr = r["properties"]
        tg = (pr.get("Telegram ID") or {}).get("number")
        if tg:
            out.append({"page_id": r["id"], "tg_id": int(tg), "name": _text(pr.get("Имя")).strip()})
    return out


async def mark_welcomed(page_id):
    await _req("PATCH", f"/pages/{page_id}", {"properties": {"Приветствие отправлено": {"checkbox": True}}})


async def ping(db_id):
    await query_all(db_id, limit=1)


def _now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def _rt(text):
    return {"rich_text": [{"text": {"content": (text or "")[:1900]}}]}


async def log_question(question, staff_name, tg_id, answer, sources, status):
    props = {"Вопрос": {"title": [{"text": {"content": question[:300] or "—"}}]},
             "Дата и время": {"date": {"start": _now()}},
             "Сотрудник": _rt(staff_name), "Telegram ID": {"number": tg_id},
             "Ответ": _rt(answer), "Документ-источник": _rt(sources),
             "Статус": {"select": {"name": status}}}
    await _req("POST", "/pages", {"parent": {"database_id": config.JOURNAL_DB_ID}, "properties": props})


async def log_gap(question, staff_name):
    props = {"Вопрос": {"title": [{"text": {"content": question[:300] or "—"}}]},
             "Дата": {"date": {"start": _now()}}, "Сотрудник": _rt(staff_name),
             "Статус": {"select": {"name": "Новый"}}}
    await _req("POST", "/pages", {"parent": {"database_id": config.GAPS_DB_ID}, "properties": props})


# ---------- Ознакомление с документами и мини-тесты ----------

async def active_staff():
    """Все активные сотрудники с Telegram ID: [{page_id, tg_id, name}]."""
    flt = {"and": [{"property": "Статус", "select": {"equals": "Активен"}},
                   {"property": "Telegram ID", "number": {"is_not_empty": True}}]}
    out = []
    for r in await query_all(config.STAFF_DB_ID, flt):
        pr = r["properties"]
        tg = (pr.get("Telegram ID") or {}).get("number")
        if tg:
            out.append({"page_id": r["id"], "tg_id": int(tg), "name": _text(pr.get("Имя")).strip()})
    return out


def _title(text):
    return {"title": [{"text": {"content": (text or "—")[:300]}}]}


async def ack_create(doc_title, change, name, tg_id, bid):
    props = {"Документ": _title(doc_title), "Что изменилось": _rt(change), "Сотрудник": _rt(name),
             "Telegram ID": {"number": tg_id}, "Отправлено": {"date": {"start": _now()}},
             "Ознакомился": {"checkbox": False}, "Напоминаний": {"number": 0}, "Рассылка": _rt(bid)}
    page = await _req("POST", "/pages", {"parent": {"database_id": config.ACK_DB_ID}, "properties": props})
    return page["id"]


def _ack_row(r):
    pr = r["properties"]
    sent = ((pr.get("Отправлено") or {}).get("date") or {}).get("start")
    return {"page_id": r["id"], "doc": _text(pr.get("Документ")), "change": _text(pr.get("Что изменилось")),
            "name": _text(pr.get("Сотрудник")), "tg_id": int((pr.get("Telegram ID") or {}).get("number") or 0),
            "done": bool((pr.get("Ознакомился") or {}).get("checkbox")),
            "opened": bool(((pr.get("Открыл документ") or {}).get("date") or {}).get("start")),
            "reminders": int((pr.get("Напоминаний") or {}).get("number") or 0),
            "sent": datetime.fromisoformat(sent) if sent else None, "bid": _text(pr.get("Рассылка"))}


async def ack_get(page_id):
    return _ack_row(await _req("GET", f"/pages/{page_id}"))


async def ack_mark(page_id):
    await _req("PATCH", f"/pages/{page_id}", {"properties": {
        "Ознакомился": {"checkbox": True}, "Дата ознакомления": {"date": {"start": _now()}}}})


async def ack_opened(page_id):
    await _req("PATCH", f"/pages/{page_id}", {"properties": {"Открыл документ": {"date": {"start": _now()}}}})


async def ack_open():
    """Строки, где ознакомления ещё нет (и не закрыты после эскалации): список dict."""
    flt = {"and": [{"property": "Ознакомился", "checkbox": {"equals": False}},
                   {"property": "Напоминаний", "number": {"less_than": 3}}]}
    return [_ack_row(r) for r in await query_all(config.ACK_DB_ID, flt)]


async def ack_set_reminders(page_id, n):
    await _req("PATCH", f"/pages/{page_id}", {"properties": {"Напоминаний": {"number": n}}})


async def quiz_log(question, doc, name, tg_id, chosen, correct_text, ok, test_id):
    props = {"Вопрос": _title(question), "Дата": {"date": {"start": _now()}}, "Сотрудник": _rt(name),
             "Telegram ID": {"number": tg_id}, "Документ": _rt(doc), "Верно": {"checkbox": bool(ok)},
             "Ответ сотрудника": _rt(chosen), "Правильный ответ": _rt(correct_text), "Тест": _rt(test_id)}
    await _req("POST", "/pages", {"parent": {"database_id": config.QUIZ_DB_ID}, "properties": props})


async def ack_archive(page_id):
    """Убирает строку (рассылка не доставлена)."""
    await _req("PATCH", f"/pages/{page_id}", {"archived": True})


# ---------- Проверка ответов владельцем ----------

REVIEW_REASONS = ["Нет в документе или устарело", "Юридический вопрос", "Выбран не тот фрагмент", "Вопрос про цену"]


def _review_filter():
    return {"property": "Проверка", "select": {"is_empty": True}}


async def review_pending():
    """Непроверенные записи журнала (и ответы, и «Не найдено»), старые первыми."""
    out, cursor = [], None
    while True:
        body = {"page_size": 100, "filter": _review_filter(),
                "sorts": [{"property": "Дата и время", "direction": "ascending"}]}
        if cursor:
            body["start_cursor"] = cursor
        data = await _req("POST", f"/databases/{config.JOURNAL_DB_ID}/query", body)
        for r in data["results"]:
            p = r["properties"]
            out.append({"id": r["id"].replace("-", ""), "question": _text(p.get("Вопрос")), "answer": _text(p.get("Ответ")),
                        "source": _text(p.get("Документ-источник")), "who": _text(p.get("Сотрудник")),
                        "status": _text(p.get("Статус")),
                        "when": ((p.get("Дата и время") or {}).get("date") or {}).get("start", "")})
        if not data.get("has_more"):
            return out
        cursor = data["next_cursor"]


async def review_count():
    return len(await review_pending())


async def review_set(page_id, verdict, reason=None, comment=None):
    props = {"Проверка": {"select": {"name": verdict}}}
    if reason:
        props["Причина"] = {"select": {"name": reason}}
    if comment is not None:
        props["Комментарий проверяющего"] = _rt(comment)
    await _req("PATCH", f"/pages/{page_id}", {"properties": props})


async def gap_set(question, status, comment=None):
    """Все новые строки «Пробелов» с этим вопросом: поставить статус (и комментарий владельца)."""
    flt = {"and": [{"property": "Вопрос", "title": {"equals": question[:300]}},
                   {"property": "Статус", "select": {"equals": "Новый"}}]}
    for r in await query_all(config.GAPS_DB_ID, flt):
        props = {"Статус": {"select": {"name": status}}}
        if comment is not None:
            props["Комментарий владельца"] = _rt(comment)
        await _req("PATCH", f"/pages/{r['id']}", {"properties": props})


async def gap_set_comment(question, comment):
    """Комментарий владельца в уже обработанные строки «Пробелов» с этим вопросом."""
    flt = {"and": [{"property": "Вопрос", "title": {"equals": question[:300]}},
                   {"property": "Статус", "select": {"equals": "Проработать"}}]}
    for r in await query_all(config.GAPS_DB_ID, flt):
        await _req("PATCH", f"/pages/{r['id']}", {"properties": {"Комментарий владельца": _rt(comment)}})
