"""Настройки бота «Марк». Все значения берутся из .env (секреты в код не пишем)."""
import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))


def _get(name, default=""):
    return os.environ.get(name, default).strip()


TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN")
BOT_USERNAME = _get("BOT_USERNAME", "topolya_mark_bot").lstrip("@")
NOTION_TOKEN = _get("NOTION_TOKEN")
YANDEX_API_KEY = _get("YANDEX_API_KEY")
YANDEX_FOLDER_ID = _get("YANDEX_FOLDER_ID")

# Модель меняется одной строкой в .env (провайдер и модель — заменяемые).
LLM_BASE_URL = _get("LLM_BASE_URL", "https://ai.api.cloud.yandex.net/v1").rstrip("/")
SELECT_MODEL = _get("SELECT_MODEL", "aliceai-llm-flash")   # выбор документов
ANSWER_MODEL = _get("ANSWER_MODEL", "aliceai-llm")         # ответ сотруднику

_sa = _get("GOOGLE_SA_FILE", "google-sa.json")
GOOGLE_SA_FILE = _sa if os.path.isabs(_sa) else os.path.join(BASE_DIR, _sa)

# Базы Notion (ID берутся из .env, в публичный репозиторий не попадают)
DOCS_DB_ID = _get("DOCS_DB_ID")        # «Документы» (карточки с галочкой «Для бота»)
STAFF_DB_ID = _get("STAFF_DB_ID")      # «Марк — Сотрудники»
JOURNAL_DB_ID = _get("JOURNAL_DB_ID")  # «Марк — Журнал вопросов»
GAPS_DB_ID = _get("GAPS_DB_ID")        # «Марк — Пробелы в базе знаний»

# ID баз не секретны (без токена интеграции они бесполезны), поэтому заданы по умолчанию; .env может переопределить
ACK_DB_ID = _get("ACK_DB_ID") or "9f4f6d47b8414485841eee1998bc79f1"          # «Марк — Ознакомления» (необязательно: без него рассылка выключена)
QUIZ_DB_ID = _get("QUIZ_DB_ID") or "ac84a8c5cc9f4f8bbcb8be8fb44b7750"        # «Марк — Результаты тестов» (необязательно)
QUIZ_WEEKLY = _get("QUIZ_WEEKLY", "0") in ("1", "true", "yes")  # автоматический тест раз в неделю (пн 11:00 МСК)

_owner = _get("OWNER_TELEGRAM_ID")
OWNER_TELEGRAM_ID = int(_owner) if _owner.isdigit() else None  # куда слать тревоги

HEALTHCHECK_URL = _get("HEALTHCHECK_URL")   # адрес «пинга» внешнего мониторинга (healthchecks.io), необязательно
SNAPSHOT_DIR = os.path.join(BASE_DIR, "snapshots")

CACHE_TTL = int(_get("CACHE_TTL_SECONDS", "720") or 720)  # кэш документов, сек (12 мин)
SESSION_SECONDS = 3600     # сессия разговора: 1 час без сообщений
HISTORY_MESSAGES = 10      # сколько последних сообщений помнит бот
MAX_DOCS = 3               # документов на один ответ
DOC_CHARS_TOTAL = 90000    # лимит текста документов в одном запросе

# В этих файлах бот читает ТОЛЬКО перечисленные листы (ключ — название карточки, в нижнем регистре).
SHEET_TABS = {"доступ wi-fi": {"ресепшен", "в номера"}}

REQUIRED = ["TELEGRAM_BOT_TOKEN", "NOTION_TOKEN", "YANDEX_API_KEY", "YANDEX_FOLDER_ID",
            "DOCS_DB_ID", "STAFF_DB_ID", "JOURNAL_DB_ID", "GAPS_DB_ID"]


def missing():
    g = globals()
    return [n for n in REQUIRED if not g.get(n)]
