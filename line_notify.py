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
