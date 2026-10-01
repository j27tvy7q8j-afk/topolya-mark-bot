"""Чтение Google Docs и Sheets сервисным аккаунтом (только чтение)."""
import re
import threading

import config

_lock = threading.Lock()
_svc = None
SCOPES = ["https://www.googleapis.com/auth/drive.readonly",
          "https://www.googleapis.com/auth/spreadsheets.readonly"]


def _services():
    global _svc
    if _svc is None:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds = service_account.Credentials.from_service_account_file(config.GOOGLE_SA_FILE, scopes=SCOPES)
        _svc = (build("drive", "v3", credentials=creds, cache_discovery=False),
                build("sheets", "v4", credentials=creds, cache_discovery=False))
    return _svc


def parse_url(url):
    """Возвращает (file_id, kind) где kind — 'sheet' или 'doc'; (None, None) если ссылка не распознана."""
    m = re.search(r"/d/([A-Za-z0-9_-]{20,})", url or "")
    if not m:
        return None, None
    return m.group(1), ("sheet" if "/spreadsheets/" in url else "doc")


def _norm(s):
    return (s or "").strip().lower()


def read_document(file_id):
    with _lock:
        drive, _ = _services()
        data = drive.files().export(fileId=file_id, mimeType="text/plain").execute()
    return data.decode("utf-8", "replace").lstrip("﻿").strip()


def read_sheet(file_id, allowed_tabs=None):
    """Текст таблицы. allowed_tabs — множество разрешённых листов (нижний регистр) или None (все)."""
    with _lock:
        _, sheets = _services()
        meta = sheets.spreadsheets().get(spreadsheetId=file_id, fields="sheets.properties.title").execute()
        titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
        if allowed_tabs is not None:
            titles = [t for t in titles if _norm(t) in allowed_tabs]
            if not titles:
                raise RuntimeError("в файле нет разрешённых листов: " + ", ".join(sorted(allowed_tabs)))
        ranges = ["'" + t.replace("'", "''") + "'" for t in titles]
        vr = sheets.spreadsheets().values().batchGet(spreadsheetId=file_id, ranges=ranges).execute()
    parts = []
    for title, rng in zip(titles, vr.get("valueRanges", [])):
        rows = [" | ".join(str(c).strip() for c in row if str(c).strip())
                for row in rng.get("values", [])]
        rows = [r for r in rows if r]
        if rows:
            parts.append(f"[Лист: {title}]\n" + "\n".join(rows))
    return "\n\n".join(parts).strip()
