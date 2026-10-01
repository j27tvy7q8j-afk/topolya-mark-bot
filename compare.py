"""Сравнение моделей ответа на типичных вопросах сотрудников.
Запуск: venv/bin/python compare.py            (модели ищутся автоматически: текущая + DeepSeek, если есть в каталоге)
        venv/bin/python compare.py --model gpt://.../deepseek-... --model gpt://.../aliceai-llm
Полные ответы: compare_report.txt"""
import argparse
import asyncio
import time

import httpx

import config
import llm
import kb as kbmod

# (вопрос, [варианты подстрок — достаточно одной из группы; все группы обязательны], ожидается_ли_ответ)
QUESTIONS = [
    ("До какого времени ночью закрыта парковка?", [["24:00", "00:00"], ["06:00", "6:00"]], True),
    ("Сколько стоит парковка для посторонних в дни свадеб в кафе?", [["500"]], True),
    ("Какой пароль от Wi-Fi в номерах?", [["9620267474"]], True),
    ("Можно ли курить в номере?", [["нельзя", "запрещ", "нет"]], True),
    ("Где можно курить?", [["10 м", "10 метр"]], True),
    ("Во сколько подают завтрак?", [["6:00", "06:00"], ["11:00"]], True),
    ("Когда технический работник отключает автоматы сплит-систем?", [["02.10.2026", "2 октября"]], True),
    ("Что осенью администратор делает с пультами от сплит-систем?", [["пульт"], ["коробк"]], True),
    ("Гость приехал ночью, ворота закрыты. Что делать?", [["026", "7474"]], True),
    ("Есть ли зарядка для электромобилей?", [["Пункт", "платн"]], True),
    ("Какие документы подходят для удостоверения личности гостя?", [["паспорт"]], True),
    ("Где парковаться гостю и во сколько закрывают ворота?", [["24:00", "00:00"]], True),
    ("Какой сегодня курс доллара?", [], False),
    ("Сколько получает горничная в месяц?", [], False),
]


CANDIDATES = ["deepseek-v32/latest", "deepseek-v32", "deepseek-v4-flash/latest", "deepseek-v4-flash",
              "deepseek-v3.2/latest", "qwen3-235b-a22b-fp8/latest", "gpt-oss-120b/latest"]


async def discover():
    r = httpx.get("https://llm.api.cloud.yandex.net/foundationModels/v1/models",
                  params={"folderId": config.YANDEX_FOLDER_ID},
                  headers={"Authorization": f"Api-Key {config.YANDEX_API_KEY}"}, timeout=30)
    uris = []
    try:
        for m in r.json().get("models", []):
            uris.append(m.get("uri") or m.get("name") or "")
    except Exception:
        pass
    return [u for u in uris if u]


def check(ans, groups, expect_answer):
    if not expect_answer:
        return ans.status == "Не найдено"
    if ans.status != "Отвечено":
        return False
    low = " ".join(ans.text.lower().replace("\u00a0", " ").replace("\u202f", " ").split())
    return all(any(s.lower() in low for s in g) for g in groups)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", action="append")
    args = ap.parse_args()
    models = args.model
    if not models:
        models = [config.ANSWER_MODEL]
        prov = llm.get_provider()
        for name in CANDIDATES:
            uri = f"gpt://{config.YANDEX_FOLDER_ID}/{name}"
            try:
                await prov.chat(uri, [{"role": "user", "content": "Ответь одним словом: да"}], max_tokens=600, timeout=90)
                print("Модель доступна:", uri)
                models.append(uri)
            except Exception as e:
                print("Нет модели", name, "-", str(e)[:90])
    models = list(dict.fromkeys(models))
    kb = kbmod.KB(llm.get_provider())
    for d in await kb.catalog():      # прогрев кэша, как у настоящего бота
        try:
            await kb.text(d)
        except Exception as e:
            print("Не прочитан", d.title, "-", str(e)[:80])
    results = {m: [] for m in models}
    report = []
    for q, groups, expect in QUESTIONS:
        report.append(f"\n### {q}")
        for m in models:
            t0 = time.time()
            try:
                a = await kb.answer(q, [], model=m)
                ok, text = check(a, groups, expect), a.text
            except Exception as e:
                ok, text = False, f"ОШИБКА {e}"
            dt = time.time() - t0
            results[m].append((ok, dt, len(text)))
            report.append(f"[{m}] {'ВЕРНО' if ok else 'НЕВЕРНО'} {dt:.1f}с\n{text}")
    open("compare_report.txt", "w", encoding="utf-8").write("\n".join(report))
    print("\nИТОГ (верных ответов из %d, среднее время, средняя длина ответа):" % len(QUESTIONS))
    for m, rs in results.items():
        n = sum(1 for r in rs if r[0])
        print(f"  {m}: {n}/{len(rs)}, {sum(r[1] for r in rs)/len(rs):.1f}с, {sum(r[2] for r in rs)//len(rs)} симв.")
    print("\nПо вопросам (+ верно, - неверно):")
    for i, (q, _, _) in enumerate(QUESTIONS):
        print("  " + " ".join("+" if results[m][i][0] else "-" for m in models) + "  " + q)
    print("\nПолные ответы: compare_report.txt")


if __name__ == "__main__":
    asyncio.run(main())
