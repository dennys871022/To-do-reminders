"""
line_notify.py
---------------
用 LINE Messaging API 發送訊息到 LINE 群組。

【這版新增】send_line_group_message_with_button()：在同一次 push 裡，
除了文字訊息，還附加一個「按鈕訊息」（Template Message），點了直接開啟指定網址。
官方文件明確說明：一次推播最多 3 個訊息物件（吹泡）都只算「1 則」，
所以附加按鈕不會增加你的月費用量。
"""

import os
import requests

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_QUOTA_URL = "https://api.line.me/v2/bot/message/quota"
LINE_QUOTA_CONSUMPTION_URL = "https://api.line.me/v2/bot/message/quota/consumption"


def _get_secret(name):
    try:
        import streamlit as st
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name)


def _auth_and_target(channel_access_token, group_id):
    token = channel_access_token or _get_secret("LINE_CHANNEL_ACCESS_TOKEN")
    group = group_id or _get_secret("LINE_GROUP_ID")
    if not token or not group:
        raise RuntimeError(
            "缺少 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_GROUP_ID，"
            "請在 secrets 或環境變數中設定。"
        )
    return token, group


def _post(token, group, messages):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    payload = {"to": group, "messages": messages}
    resp = requests.post(LINE_PUSH_URL, headers=headers, json=payload, timeout=10)
    if resp.status_code != 200:
        raise RuntimeError(f"LINE 推播失敗 ({resp.status_code}): {resp.text}")
    return resp.json() if resp.text else {}


def send_line_group_message(text, channel_access_token=None, group_id=None):
    """傳送一則純文字訊息到指定 LINE 群組。"""
    token, group = _auth_and_target(channel_access_token, group_id)
    text = text[:1900]  # LINE 單則文字訊息上限保守截斷
    return _post(token, group, [{"type": "text", "text": text}])


def send_line_group_message_with_button(
    text, button_label, button_url, channel_access_token=None, group_id=None
):
    """
    傳送「文字訊息 + 按鈕」到指定 LINE 群組，一次 push 包含 2 個訊息物件，
    但仍只算「1 則」（官方文件：最多 3 個吹泡都算 1 則）。
    """
    token, group = _auth_and_target(channel_access_token, group_id)
    text = text[:1900]
    button_label = button_label[:20]  # LINE 按鈕文字上限 20 字

    messages = [
        {"type": "text", "text": text},
        {
            "type": "template",
            "altText": button_label,
            "template": {
                "type": "buttons",
                "text": "點擊下方按鈕立即處理",
                "actions": [
                    {"type": "uri", "label": button_label, "uri": button_url}
                ],
            },
        },
    ]
    return _post(token, group, messages)


def build_reminder_text(todo):
    """把單筆代辦事項組成推播文字（保留給單筆場景使用）。"""
    loc = f"／地點：{todo.get('location')}" if todo.get("location") else ""
    date_range = todo["start_date"]
    if todo["start_date"] != todo["end_date"]:
        date_range += f"~{todo['end_date']}"
    return (
        f"【{todo.get('_tier', '')}提醒】\n"
        f"{todo['task']}\n"
        f"工項：{todo['work_item']}／類型：{todo['type']}{loc}\n"
        f"期限：{date_range}"
    )


def get_quota_status(channel_access_token=None):
    """
    查詢這個 LINE 官方帳號本月的訊息額度與已使用量。
    回傳 {"limit": 額度上限(None代表沒有上限或查不到), "used": 本月已用則數}。
    任何一步查詢失敗都不會丟例外，缺的欄位用 None 表示，方便畫面上優雅地顯示「暫時無法取得」。
    """
    token = channel_access_token or _get_secret("LINE_CHANNEL_ACCESS_TOKEN")
    if not token:
        return {"limit": None, "used": None, "error": "尚未設定 LINE_CHANNEL_ACCESS_TOKEN"}

    headers = {"Authorization": f"Bearer {token}"}
    limit, used, error = None, None, None

    try:
        resp = requests.get(LINE_QUOTA_URL, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            # type 為 "limited" 才有 value（月額度上限）；"none" 代表沒有上限（付費方案）
            if data.get("type") == "limited":
                limit = data.get("value")
        else:
            error = f"quota 查詢失敗 ({resp.status_code})"
    except Exception as e:
        error = f"quota 查詢發生錯誤：{e}"

    try:
        resp = requests.get(LINE_QUOTA_CONSUMPTION_URL, headers=headers, timeout=10)
        if resp.status_code == 200:
            used = resp.json().get("totalUsage")
        elif not error:
            error = f"用量查詢失敗 ({resp.status_code})"
    except Exception as e:
        if not error:
            error = f"用量查詢發生錯誤：{e}"

    return {"limit": limit, "used": used, "error": error}
