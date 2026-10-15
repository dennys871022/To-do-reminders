"""
data_store.py
--------------
統一封裝所有資料讀寫邏輯（用 Google Sheets 當資料庫）。

【效能與多人使用的設計重點】
1. 讀取結果用 st.cache_data 快取 CACHE_TTL_SECONDS 秒（所有使用者共用同一份快取）；
   任何人新增／修改／刪除之後會立刻清掉快取，所以大家看到的永遠是最新資料，
   只有「直接在 Google 試算表手動改」才會最多延遲一個 TTL（App 上有「重新整理」按鈕）。
2. 下拉選單的 4 個類別合併成「一次讀取」(get_all_options)，不再讀 4 次。
3. 遇到 Google API 的頻率限制（429）會自動等待後重試，不會直接跳紅色錯誤畫面；
   讀取遇到暫時性的網路錯誤／伺服器錯誤（5xx）也會重試。
4. 所有「寫入」動作用同一把鎖序列化，避免多人同時新增／刪除時，
   因為列號位移而改到錯誤的列。
5. 快取需要真的去讀 Google 的時候，畫面上會顯示「讀取雲端資料中…」讓人知道在處理。

注意：TODO_HEADERS 裡仍保留 "urgency"、"workers" 欄位以維持既有 Google Sheet 的欄位
結構不被打亂，但這兩個欄位目前都已不使用（緊急程度由 urgency.py 依結束日期即時計算，
移工改記錄在 Dispatches 分頁）。
"""

import functools
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

import gspread
import requests
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# 讀取快取秒數。調大＝更快但「直接改試算表」要等比較久才看得到；調小＝更即時但更容易變慢。
CACHE_TTL_SECONDS = 120

TODO_HEADERS = [
    "id", "start_date", "end_date", "task", "location", "urgency",
    "work_item", "type", "completed", "created_at", "last_reminder_at",
    "image_url", "engineer", "workers",
]
EXP_HEADERS = ["id", "work_item", "source", "details", "mistakes", "created_at"]
OPTION_HEADERS = ["category", "value"]
DISPATCH_HEADERS = ["id", "todo_id", "start_date", "end_date", "workers", "created_at"]

# 移工名單用「、」分隔存成一個字串（一筆派工紀錄可能同時指派多位移工）
WORKERS_SEPARATOR = "、"

DEFAULT_OPTIONS = {
    "work_item": ["泥作", "木作", "水電", "連續壁"],
    "type": ["叫料", "派工", "查驗"],
    "engineer": ["林智捷", "孫永明", "黃元杰"],
    "worker": [
        "15020 黎文海", "15028 李文藥", "15030 阮進康", "15033 阮江南",
        "15034 阮英俊", "15038 高德", "15048 阮文財", "15049 馮孫昭",
        "9015 楊文豐", "FY-19 阮文情",
    ],
}


def split_workers(workers_str):
    """把儲存格裡用「、」分隔的移工字串拆回清單。"""
    if not workers_str:
        return []
    return [w.strip() for w in workers_str.split(WORKERS_SEPARATOR) if w.strip()]


def join_workers(workers_list):
    return WORKERS_SEPARATOR.join(workers_list)


# ====================================================================
# 憑證與設定
# ====================================================================

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


# ====================================================================
# 快取、自動重試、寫入鎖
# ====================================================================

def _cache_resource(ttl=None):
    """在 Streamlit 環境下用 st.cache_resource 包裝；非 Streamlit 環境直接跳過快取。"""
    try:
        import streamlit as st
        return st.cache_resource(ttl=ttl, show_spinner=False)
    except Exception:
        def _noop(func):
            func.clear = lambda: None
            return func
        return _noop


def _cache_data(ttl=CACHE_TTL_SECONDS, spinner="讀取雲端資料中…"):
    """讀取函式的共用快取（所有使用者共用）。真的需要去 Google 讀取時，
    畫面上會顯示 spinner 文字；命中快取則瞬間回傳、不顯示任何東西。"""
    try:
        import streamlit as st
        return st.cache_data(ttl=ttl, show_spinner=spinner)
    except Exception:
        def _noop(func):
            func.clear = lambda: None
            return func
        return _noop


def _status_of(err):
    resp = getattr(err, "response", None)
    status = getattr(resp, "status_code", None)
    if status is None:
        status = getattr(err, "code", None)
    return status if isinstance(status, int) else None


def _retry_on_quota(retry_5xx, tries=4, base_delay=2.0):
    """
    遇到暫時性錯誤時，等一下再重試（等待 2、4、8 秒），而不是直接丟出錯誤。
    - 429（Google 頻率限制）：一律重試（請求被拒絕、沒有執行，重試是安全的）。
    - 5xx 與網路逾時：只有「讀取」才重試（retry_5xx=True）；寫入不重試，
      因為伺服器可能其實已經寫入成功，重試會造成重複資料。
    """
    def deco(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            delay = base_delay
            for attempt in range(1, tries + 1):
                try:
                    return func(*args, **kwargs)
                except gspread.exceptions.APIError as e:
                    status = _status_of(e)
                    retryable = status == 429 or (retry_5xx and status is not None and status >= 500)
                    if not retryable or attempt == tries:
                        raise
                except requests.exceptions.RequestException:
                    if not retry_5xx or attempt == tries:
                        raise
                time.sleep(delay)
                delay *= 2
        return wrapper
    return deco


_read_op = _retry_on_quota(retry_5xx=True)

# 同一個伺服器行程內，所有寫入動作共用一把鎖（可重入，因為 delete_todo 會呼叫 delete_dispatch）
_WRITE_LOCK = threading.RLock()


def _write_op(func):
    retrying = _retry_on_quota(retry_5xx=False)(func)

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _WRITE_LOCK:
            return retrying(*args, **kwargs)
    return wrapper


# ====================================================================
# 連線、工作表
# ====================================================================

@_cache_resource()
def _client():
    return gspread.authorize(_get_credentials())


@_cache_resource(ttl=300)
@_read_op
def _open_spreadsheet():
    return _client().open_by_key(_get_sheet_id())


def _get_or_create_worksheet(ss, title, headers, seed_rows=None):
    try:
        ws = ss.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = ss.add_worksheet(title=title, rows=1000, cols=max(len(headers), 2))
        ws.append_rows([headers] + (seed_rows or []))
        return ws

    existing = ws.row_values(1)
    if existing != headers:
        ws.update(values=[headers], range_name="A1")
    return ws


@_cache_resource(ttl=300)
@_read_op
def _todo_ws():
    return _get_or_create_worksheet(_open_spreadsheet(), "Todos", TODO_HEADERS)


@_cache_resource(ttl=300)
@_read_op
def _exp_ws():
    return _get_or_create_worksheet(_open_spreadsheet(), "Experiences", EXP_HEADERS)


@_cache_resource(ttl=300)
@_read_op
def _option_ws():
    seed_rows = [
        [category, v]
        for category, values in DEFAULT_OPTIONS.items()
        for v in values
    ]
    return _get_or_create_worksheet(
        _open_spreadsheet(), "Options", OPTION_HEADERS, seed_rows=seed_rows
    )


@_cache_resource(ttl=300)
@_read_op
def _dispatch_ws():
    return _get_or_create_worksheet(_open_spreadsheet(), "Dispatches", DISPATCH_HEADERS)


# ====================================================================
# 小工具
# ====================================================================

def _gen_id(prefix):
    return f"{prefix}-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(headers, row):
    row = list(row) + [""] * (len(headers) - len(row))
    return dict(zip(headers, row))


def _find_row_index(ws, headers, row_id):
    """只讀 id 那一欄，找出該筆資料在第幾列（第 1 列是標題）。"""
    ids = ws.col_values(headers.index("id") + 1)
    for i, val in enumerate(ids, start=1):
        if val == row_id:
            return i
    return None


def _find_row(ws, headers, row_id):
    """一次讀整張表，同時回傳（列號, 該列內容），比分開讀少一次 API 呼叫。"""
    values = ws.get_all_values()
    id_col = headers.index("id")
    for i, row in enumerate(values, start=1):
        if len(row) > id_col and row[id_col] == row_id:
            return i, row
    return None, None


# ====================================================================
# 圖片上傳
# ====================================================================

def _get_imgbb_api_key():
    try:
        import streamlit as st
        if "IMGBB_API_KEY" in st.secrets:
            return st.secrets["IMGBB_API_KEY"]
    except Exception:
        pass
    return os.environ.get("IMGBB_API_KEY")


def upload_image(file_bytes, filename, mime_type):
    """把圖片上傳到 ImgBB（免費圖片託管，不需要登入或 OAuth），回傳可直接顯示的網址。"""
    api_key = _get_imgbb_api_key()
    if not api_key:
        raise RuntimeError(
            "找不到 IMGBB_API_KEY。請先到 https://api.imgbb.com/ 免費申請一組 API Key，"
            "再設進 secrets 的 IMGBB_API_KEY。"
        )

    resp = requests.post(
        "https://api.imgbb.com/1/upload",
        params={"key": api_key},
        files={"image": (filename, file_bytes, mime_type)},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"ImgBB 上傳失敗 ({resp.status_code}): {resp.text}")

    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"ImgBB 上傳失敗：{data}")

    return data["data"]["url"]


# ====================================================================
# 代辦事項
# ====================================================================

@_cache_data()
@_read_op
def get_todos():
    return _todo_ws().get_all_records(expected_headers=TODO_HEADERS)


@_write_op
def add_todo(start_date, end_date, task, location, work_item, type_,
             image_url="", engineer="", workers=""):
    """新增代辦事項。緊急程度不由使用者傳入，一律由到期日自動判定。"""
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
        "engineer": engineer,
        "workers": workers,
    }
    _todo_ws().append_row([row[h] for h in TODO_HEADERS])
    get_todos.clear()
    return row


@_write_op
def update_todo(todo_id, **fields):
    ws = _todo_ws()
    idx, row_values = _find_row(ws, TODO_HEADERS, todo_id)
    if not idx:
        raise ValueError(f"找不到 id={todo_id} 的代辦事項")
    current = _row_to_dict(TODO_HEADERS, row_values)
    current.update(fields)
    ws.update(values=[[current[h] for h in TODO_HEADERS]], range_name=f"A{idx}")
    get_todos.clear()


@_write_op
def delete_todo(todo_id):
    ws = _todo_ws()
    idx = _find_row_index(ws, TODO_HEADERS, todo_id)
    if idx:
        ws.delete_rows(idx)
    get_todos.clear()
    for d in get_dispatches_for_todo(todo_id):
        delete_dispatch(d["id"])


def mark_completed(todo_id, completed=True):
    update_todo(todo_id, completed="TRUE" if completed else "FALSE")


def mark_reminder_sent(todo_id, when_iso=None):
    update_todo(todo_id, last_reminder_at=when_iso or _now_iso())


# ====================================================================
# 經驗分享
# ====================================================================

@_cache_data()
@_read_op
def get_experiences():
    return _exp_ws().get_all_records(expected_headers=EXP_HEADERS)


@_write_op
def add_experience(work_item, details, mistakes, source="我的經驗"):
    row = {
        "id": _gen_id("exp"),
        "work_item": work_item,
        "source": source,
        "details": details,
        "mistakes": mistakes,
        "created_at": _now_iso(),
    }
    _exp_ws().append_row([row[h] for h in EXP_HEADERS])
    get_experiences.clear()
    return row


@_write_op
def delete_experience(exp_id):
    ws = _exp_ws()
    idx = _find_row_index(ws, EXP_HEADERS, exp_id)
    if idx:
        ws.delete_rows(idx)
    get_experiences.clear()


# ====================================================================
# 選單選項（工項／類型／工程師／移工）
# ====================================================================

@_cache_data()
@_read_op
def get_all_options():
    """4 個類別一次讀完（只打 1 次 API），回傳 {類別: [選項, ...]}。"""
    records = _option_ws().get_all_records(expected_headers=OPTION_HEADERS)
    result = {}
    for r in records:
        result.setdefault(r["category"], []).append(r["value"])
    return result


def get_options(category):
    return list(get_all_options().get(category, []))


@_write_op
def add_option(category, value):
    value = value.strip()
    if not value:
        return
    if value in get_options(category):
        return
    _option_ws().append_row([category, value])
    get_all_options.clear()


@_write_op
def delete_option(category, value):
    ws = _option_ws()
    records = ws.get_all_records(expected_headers=OPTION_HEADERS)
    for i, r in enumerate(records, start=2):  # 第 1 列是標題，資料從第 2 列開始
        if r["category"] == category and r["value"] == value:
            ws.delete_rows(i)
            break
    get_all_options.clear()


# ====================================================================
# 派工紀錄（Dispatch）
# 一筆代辦事項可以有多筆派工紀錄，每筆有自己獨立的日期（預設只有選定那一天生效，
# 隔天自動不算數，除非明確選一段區間）與自己的移工名單。
# ====================================================================

@_cache_data()
@_read_op
def get_dispatches():
    return _dispatch_ws().get_all_records(expected_headers=DISPATCH_HEADERS)


def get_dispatches_for_todo(todo_id):
    return [d for d in get_dispatches() if d["todo_id"] == todo_id]


def get_dispatches_by_todo():
    """一次把所有派工紀錄依事項分好組：{todo_id: [派工紀錄, ...]}。
    迴圈裡要逐筆查詢時請用這個，比每筆事項各呼叫一次 get_dispatches_for_todo 快很多。"""
    grouped = {}
    for d in get_dispatches():
        grouped.setdefault(d["todo_id"], []).append(d)
    return grouped


@_write_op
def add_dispatch(todo_id, start_date, end_date, workers):
    row = {
        "id": _gen_id("dispatch"),
        "todo_id": todo_id,
        "start_date": start_date,
        "end_date": end_date,
        "workers": workers,
        "created_at": _now_iso(),
    }
    _dispatch_ws().append_row([row[h] for h in DISPATCH_HEADERS])
    get_dispatches.clear()
    return row


@_write_op
def delete_dispatch(dispatch_id):
    ws = _dispatch_ws()
    idx = _find_row_index(ws, DISPATCH_HEADERS, dispatch_id)
    if idx:
        ws.delete_rows(idx)
    get_dispatches.clear()


# ====================================================================
# 一鍵清除所有快取（App 上的「重新整理」按鈕用）
# ====================================================================

def clear_all_caches():
    for fn in (get_todos, get_dispatches, get_experiences, get_all_options):
        fn.clear()
    for fn in (_todo_ws, _exp_ws, _option_ws, _dispatch_ws, _open_spreadsheet):
        clear = getattr(fn, "clear", None)
        if clear:
            clear()
