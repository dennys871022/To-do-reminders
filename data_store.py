"""
data_store.py
--------------
統一封裝所有資料讀寫邏輯（用 Google Sheets 當資料庫）。

用 st.cache_resource 快取連線物件跟試算表物件，避免 Streamlit 每次互動都重新
整支腳本重跑時，重複呼叫 Google API 導致觸發 API 頻率限制。

注意：TODO_HEADERS 裡仍保留 "urgency" 欄位以維持既有 Google Sheet 的欄位結構
不被打亂，但這個欄位現在已經不由使用者手動選擇、也不影響提醒排程——
實際的緊急程度分級一律由 urgency.py 依「結束日期」即時計算。
"""

import os
import json
import time
import uuid
import requests
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
    "image_url",
]
EXP_HEADERS = ["id", "work_item", "source", "details", "mistakes", "created_at"]
OPTION_HEADERS = ["category", "value"]

DEFAULT_OPTIONS = {
    "work_item": ["泥作", "木作", "水電", "連續壁"],
    "type": ["叫料", "派工", "查驗"],
}


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
    """在 Streamlit 環境下用 st.cache_resource 包裝；非 Streamlit 環境（例如
    GitHub Actions 執行 reminder_check.py）直接跳過快取，因為那邊跑一次就結束。"""
    try:
        import streamlit as st
        return st.cache_resource(ttl=ttl)
    except Exception:
        def _noop(func):
            return func
        return _noop


def _cache_data(ttl=15):
    """在 Streamlit 環境下用 st.cache_data 包裝讀取函式，短時間內（預設15秒）
    重複呼叫直接吃快取結果，不重打 Google API，避免觸發頻率限制。
    寫入操作後會手動呼叫對應函式的 .clear() 讓畫面立刻反映最新資料，不用等快取過期。"""
    try:
        import streamlit as st
        return st.cache_data(ttl=ttl)
    except Exception:
        def _noop(func):
            func.clear = lambda: None
            return func
        return _noop


@_cache_resource()
def _client():
    creds = _get_credentials()
    return gspread.authorize(creds)


@_cache_resource(ttl=300)
def _open_spreadsheet():
    gc = _client()
    return gc.open_by_key(_get_sheet_id())


def _get_imgur_client_id():
    try:
        import streamlit as st
        if "IMGUR_CLIENT_ID" in st.secrets:
            return st.secrets["IMGUR_CLIENT_ID"]
    except Exception:
        pass
    return os.environ.get("IMGUR_CLIENT_ID")


def upload_image(file_bytes, filename, mime_type):
    """
    把圖片上傳到 Imgur（匿名上傳，不需要任何人登入），回傳一個可以直接用
    st.image() 顯示的永久網址。

    改用 Imgur 而不是 Google Drive，是因為 Google 服務帳號本身沒有 Drive
    儲存空間（容量是 0），一般 Gmail 帳號又沒有「共用雲端硬碟」這個功能可以
    繞過這個限制（那是 Google Workspace 才有的付費功能），所以服務帳號
    上傳檔案到 Drive 這條路走不通，改用不需要 OAuth 的 Imgur 匿名上傳 API。
    """
    client_id = _get_imgur_client_id()
    if not client_id:
        raise RuntimeError(
            "找不到 IMGUR_CLIENT_ID。請先到 https://api.imgur.com/oauth2/addclient "
            "申請一組匿名上傳用的 Client ID，再設進 secrets 的 IMGUR_CLIENT_ID。"
        )

    resp = requests.post(
        "https://api.imgur.com/3/image",
        headers={"Authorization": f"Client-ID {client_id}"},
        files={"image": (filename, file_bytes, mime_type)},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Imgur 上傳失敗 ({resp.status_code}): {resp.text}")

    data = resp.json()
    return data["data"]["link"]


def _get_or_create_worksheet(ss, title, headers, seed_rows=None):
    try:
        ws = ss.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = ss.add_worksheet(title=title, rows=1000, cols=max(len(headers), 2))
        rows_to_write = [headers] + (seed_rows or [])
        ws.append_rows(rows_to_write)
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

@_cache_data()
def get_todos():
    ws = _todo_ws()
    records = ws.get_all_records(expected_headers=TODO_HEADERS)
    return records


def add_todo(start_date, end_date, task, location, work_item, type_, image_url=""):
    """新增代辦事項。緊急程度不再由使用者傳入，一律由到期日自動判定。"""
    ws = _todo_ws()
    row = {
        "id": _gen_id("todo"),
        "start_date": start_date,
        "end_date": end_date,
        "task": task,
        "location": location,
        "urgency": "",  # 不再使用，保留欄位只為了不打亂既有試算表結構
        "work_item": work_item,
        "type": type_,
        "completed": "FALSE",
        "created_at": _now_iso(),
        "last_reminder_at": "",
        "image_url": image_url,
    }
    ws.append_row([row[h] for h in TODO_HEADERS])
    get_todos.clear()
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
    get_todos.clear()


def delete_todo(todo_id):
    ws = _todo_ws()
    idx = _find_row_index(ws, TODO_HEADERS, todo_id)
    if idx:
        ws.delete_rows(idx)
    get_todos.clear()


def mark_completed(todo_id, completed=True):
    update_todo(todo_id, completed="TRUE" if completed else "FALSE")


def mark_reminder_sent(todo_id, when_iso=None):
    update_todo(todo_id, last_reminder_at=when_iso or _now_iso())


# ---------------- 經驗分享 ----------------

@_cache_data()
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
    get_experiences.clear()
    return row


def delete_experience(exp_id):
    ws = _exp_ws()
    idx = _find_row_index(ws, EXP_HEADERS, exp_id)
    if idx:
        ws.delete_rows(idx)
    get_experiences.clear()


# ---------------- 選單選項（工項／類型） ----------------

def _option_ws():
    seed_rows = [
        [category, v]
        for category, values in DEFAULT_OPTIONS.items()
        for v in values
    ]
    return _get_or_create_worksheet(
        _open_spreadsheet(), "Options", OPTION_HEADERS, seed_rows=seed_rows
    )


@_cache_data()
def get_options(category):
    ws = _option_ws()
    records = ws.get_all_records(expected_headers=OPTION_HEADERS)
    return [r["value"] for r in records if r["category"] == category]


def add_option(category, value):
    value = value.strip()
    if not value:
        return
    if value in get_options(category):
        return
    ws = _option_ws()
    ws.append_row([category, value])
    get_options.clear()


def delete_option(category, value):
    ws = _option_ws()
    records = ws.get_all_records(expected_headers=OPTION_HEADERS)
    for i, r in enumerate(records, start=2):  # 第 1 列是標題，資料從第 2 列開始
        if r["category"] == category and r["value"] == value:
            ws.delete_rows(i)
            break
    get_options.clear()
