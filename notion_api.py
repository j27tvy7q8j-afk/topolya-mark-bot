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
