"""Самопроверка бота на сервере (токены не печатает).
  venv/bin/python selftest.py                      — проверка всех подключений
  venv/bin/python selftest.py --ask "вопрос"       — полный ответ без Telegram и без записи в журнал
  venv/bin/python selftest.py --ask "вопрос" --model aliceai-llm --model <другая>   — сравнение моделей
  venv/bin/python selftest.py --models             — список моделей каталога Yandex
"""
import argparse
import asyncio

import config
import llm
import notion_api
from kb import KB


FAILS = []


def ok(flag, text):
    print(("OK   " if flag else "FAIL ") + text)
    if not flag:
        FAILS.append(text)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ask")
    ap.add_argument("--model", action="append")
    ap.add_argument("--models", action="store_true")
    args = ap.parse_args()
    miss = config.missing()
    if miss:
        print("В .env не хватает:", ", ".join(miss))
        return
    provider = llm.get_provider()
    kb = KB(provider)

    if args.models:
        import httpx
        r = httpx.get("https://llm.api.cloud.yandex.net/foundationModels/v1/models",
                      params={"folderId": config.YANDEX_FOLDER_ID},
                      headers={"Authorization": f"Api-Key {config.YANDEX_API_KEY}"}, timeout=30)
        print("HTTP", r.status_code)
        print(r.text[:4000])
        return

    if args.ask:
        history = []
        docs = await kb.catalog()
        picks = await kb.select(args.ask, history, docs)
        print("Выбраны документы:", [d.title for d in picks] or "нет")
        for m in (args.model or [config.ANSWER_MODEL]):
            try:
                a = await kb.answer(args.ask, history, model=m)
                print(f"\n=== Модель {m} | статус: {a.status} | источники: {a.sources}\n{a.text}")
            except Exception as e:
                print(f"\n=== Модель {m}: ОШИБКА {e}")
        return

    # 1. Реестр документов и чтение файлов
    try:
        docs = await kb.catalog()
        ok(len(docs) > 0, f"Реестр «Документы» (карточек «Для бота»): {len(docs)} (ожидается 26)")
    except Exception as e:
        ok(False, f"Реестр «Документы»: {e}")
        docs = []
    bad = 0
    for d in docs:
        try:
            t = await kb.text(d)
            flag = bool(t.strip())
            note = f"{len(t)} симв." + (" [листы ограничены]" if d.title.lower() in config.SHEET_TABS else "")
            ok(flag, f"  {d.title}: {note}")
            bad += 0 if flag else 1
        except Exception as e:
            bad += 1
            ok(False, f"  {d.title}: {str(e)[:150]}")
    ok(bad == 0, f"Чтение документов: ошибок {bad}")
    # 2. Служебные базы
    for name, db in (("Сотрудники", config.STAFF_DB_ID), ("Журнал вопросов", config.JOURNAL_DB_ID),
                     ("Пробелы", config.GAPS_DB_ID)):
        try:
            await notion_api.ping(db)
            ok(True, f"База «{name}» доступна")
        except Exception as e:
            ok(False, f"База «{name}»: {str(e)[:150]}")
    # 3. Модели
    for m in dict.fromkeys([config.SELECT_MODEL, config.ANSWER_MODEL]):
        try:
            out = await provider.chat(m, [{"role": "user", "content": "Ответь одним словом: работает?"}], max_tokens=30)
            ok(True, f"Модель {m}: ответила («{out[:40]}»)")
        except Exception as e:
            ok(False, f"Модель {m}: {str(e)[:200]}")


asyncio.run(main())
if FAILS:
    raise SystemExit(1)
