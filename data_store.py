"""
data_store.py
--------------
統一封裝所有資料讀寫邏輯（目前用 Google Sheets 當資料庫）。

之後如果要換成 Supabase / SQLite，只需要改這支檔案裡的實作，
app.py 跟 reminder_check.py 都不用動，因為它們只呼叫這裡定義的函式。

需要的憑證：一個 Google 服務帳號（Service Account）的 JSON 金鑰，
並把該服務帳號的 email 加入你的 Google Sheet 的共用權限（編輯者）。

本機開發：把金鑰內容放進 .streamlit/secrets.toml
GitHub Actions：把金鑰內容放進 repo 的 Secrets，用環境變數 GOOGLE_SERVICE_ACCOUNT_JSON 帶入
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
    """
    支援兩種來源：
    1. Streamlit secrets（本機 / Streamlit Cloud 執行 app.py 時）
    2. 環境變數 GOOGLE_SERVICE_ACCOUNT_JSON（GitHub Actions 執行 reminder_check.py 時）
    """
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


def _client():
    creds = _get_credentials()
    return gspread.authorize(creds)


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
