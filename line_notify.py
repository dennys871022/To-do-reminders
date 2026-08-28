"""
line_notify.py
---------------
用 LINE Messaging API 發送訊息到 LINE 群組。

注意：LINE Notify 已於 2025/3/31 停止服務，這裡改用官方建議的 Messaging API。

事前準備：
1. 到 https://developers.line.biz/ 建立一個 Provider 與 Messaging API Channel（等於建立一個 LINE 官方帳號機器人）
2. 在 Channel 設定頁面取得 Channel Access Token（長期）
3. 把這個官方帳號加入你要推播的 LINE 群組
4. 取得該群組的 Group ID（做法：讓 bot 暫時回覆訊息內容印出 event.source.group_id，
   或參考網路上「取得 LINE group id」教學，用一個簡單的 webhook 印出來）
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


def send_line_group_message(text, channel_access_token=None, group_id=None):
    """
    傳送一則純文字訊息到指定 LINE 群組。
    channel_access_token / group_id 沒指定時，會自動從
    Streamlit secrets 或環境變數（LINE_CHANNEL_ACCESS_TOKEN / LINE_GROUP_ID）取得。
    """
    token = channel_access_token or _get_secret("LINE_CHANNEL_ACCESS_TOKEN")
    group = group_id or _get_secret("LINE_GROUP_ID")

    if not token or not group:
        raise RuntimeError(
            "缺少 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_GROUP_ID，"
            "請在 secrets 或環境變數中設定。"
        )

    # LINE 單則文字訊息上限約 5000 字，這裡保守截斷避免發送失敗
    text = text[:1900]

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    payload = {
        "to": group,
        "messages": [{"type": "text", "text": text}],
    }

    resp = requests.post(LINE_PUSH_URL, headers=headers, json=payload, timeout=10)
    if resp.status_code != 200:
        raise RuntimeError(f"LINE 推播失敗 ({resp.status_code}): {resp.text}")
    return resp.json() if resp.text else {}


def build_reminder_text(todo):
    """把一筆代辦事項組成推播文字。"""
    loc = f"／地點：{todo.get('location')}" if todo.get("location") else ""
    date_range = todo["start_date"]
    if todo["start_date"] != todo["end_date"]:
        date_range += f"~{todo['end_date']}"
    return (
        f"【{todo['urgency']}提醒】\n"
        f"{todo['task']}\n"
        f"工項：{todo['work_item']}／類型：{todo['type']}{loc}\n"
        f"期限：{date_range}"
    )
