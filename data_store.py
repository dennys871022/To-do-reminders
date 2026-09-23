"""
data_store.py
--------------
統一封裝所有資料讀寫邏輯（目前用 Google Sheets 當資料庫）。

【這版新增】用 st.cache_resource 快取連線物件跟試算表物件，
避免 Streamlit 每次互動（按按鈕、切頁籤）都重新整支腳本重跑時，
重複呼叫 Google API 導致觸發 API 頻率限制（rate limit）而報錯。
"""

import os
import json
import time
import uuid
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

TODO_HEADERS = [
    "id", "start_date", "end_date", "task", "location", "urgency",
    "work_item", "type", "completed", "created_at", "last_reminder_at",
]
EXP_HEADERS = ["id", "work_item", "source", "details", "mistakes", "created_at"]


def _get_credentials():
    try:
        import streamlit as st
        if "gcp_service_account" in st.secrets:
            info = dict(st.secrets["gcp_service_account"])
            return Credentials.from_service_account_info(info, scopes=SCOPES)
    except Exception:
        pass

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        info = json.loads(raw)
        return Credentials.from_service_account_info(info, scopes=SCOPES)

    raise RuntimeError(
        "找不到 Google 服務帳號憑證。"
        "請在 .streamlit/secrets.toml 設定 [gcp_service_account]，"
        "或設定環境變數 GOOGLE_SERVICE_ACCOUNT_JSON。"
    )


def _get_sheet_id():
    try:
        import streamlit as st
        if "GOOGLE_SHEET_ID" in st.secrets:
            return st.secrets["GOOGLE_SHEET_ID"]
    except Exception:
        pass
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise RuntimeError("找不到 GOOGLE_SHEET_ID，請設定 secrets 或環境變數。")
    return sheet_id


def _cache_resource(ttl=None):
    """
    在 Streamlit 環境下用 st.cache_resource 包裝；
    在非 Streamlit 環境（例如 GitHub Actions 執行 reminder_check.py）
    直接跳過快取，因為那邊本來就是跑一次就結束，不需要快取。
    """
    try:
        import streamlit as st
        return st.cache_resource(ttl=ttl)
    except Exception:
        def _noop(func):
            return func
        return _noop


@_cache_resource()
def _client():
    creds = _get_credentials()
    return gspread.authorize(creds)


@_cache_resource(ttl=300)  # 5 分鐘內重複開啟同一份表，直接用快取，不重新打 API
def _open_spreadsheet():
    gc = _client()
    return gc.open_by_key(_get_sheet_id())


def _get_or_create_worksheet(ss, title, headers):
    try:
        ws = ss.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = ss.add_worksheet(title=title, rows=1000, cols=len(headers))
        ws.append_row(headers)
        return ws

    existing = ws.row_values(1)
    if existing != headers:
        ws.update("A1", [headers])
    return ws


def _todo_ws():
    return _get_or_create_worksheet(_open_spreadsheet(), "Todos", TODO_HEADERS)


def _exp_ws():
    return _get_or_create_worksheet(_open_spreadsheet(), "Experiences", EXP_HEADERS)


def _gen_id(prefix):
    return f"{prefix}-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(headers, row):
    row = row + [""] * (len(headers) - len(row))
    return dict(zip(headers, row))


# ---------------- 代辦事項 ----------------

def get_todos():
    ws = _todo_ws()
    records = ws.get_all_records(expected_headers=TODO_HEADERS)
    return records


def add_todo(start_date, end_date, task, location, urgency, work_item, type_):
    ws = _todo_ws()
    row = {
        "id": _gen_id("todo"),
        "start_date": start_date,
        "end_date": end_date,
        "task": task,
        "location": location,
        "urgency": urgency,
        "work_item": work_item,
        "type": type_,
        "completed": "FALSE",
        "created_at": _now_iso(),
        "last_reminder_at": "",
    }
    ws.append_row([row[h] for h in TODO_HEADERS])
    return row


def _find_row_index(ws, headers, todo_id):
    ids = ws.col_values(headers.index("id") + 1)
    for i, val in enumerate(ids, start=1):
        if val == todo_id:
            return i
    return None


def update_todo(todo_id, **fields):
    ws = _todo_ws()
    idx = _find_row_index(ws, TODO_HEADERS, todo_id)
    if not idx:
        raise ValueError(f"找不到 id={todo_id} 的代辦事項")
    row_values = ws.row_values(idx)
    current = _row_to_dict(TODO_HEADERS, row_values)
    current.update(fields)
    ws.update(f"A{idx}", [[current[h] for h in TODO_HEADERS]])


def delete_todo(todo_id):
    ws = _todo_ws()
    idx = _find_row_index(ws, TODO_HEADERS, todo_id)
    if idx:
        ws.delete_rows(idx)


def mark_completed(todo_id, completed=True):
    update_todo(todo_id, completed="TRUE" if completed else "FALSE")


def mark_reminder_sent(todo_id, when_iso=None):
    update_todo(todo_id, last_reminder_at=when_iso or _now_iso())


# ---------------- 經驗分享 ----------------

def get_experiences():
    ws = _exp_ws()
    return ws.get_all_records(expected_headers=EXP_HEADERS)


def add_experience(work_item, details, mistakes, source="我的經驗"):
    ws = _exp_ws()
    row = {
        "id": _gen_id("exp"),
        "work_item": work_item,
        "source": source,
        "details": details,
        "mistakes": mistakes,
        "created_at": _now_iso(),
    }
    ws.append_row([row[h] for h in EXP_HEADERS])
    return row


def delete_experience(exp_id):
    ws = _exp_ws()
    idx = _find_row_index(ws, EXP_HEADERS, exp_id)
    if idx:
        ws.delete_rows(idx)
