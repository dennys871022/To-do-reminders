"""
discord_notify.py
-------------------
用 Discord Webhook 發送訊息到指定頻道。

Discord Webhook 完全免費、沒有月則數上限，只有短時間內的速率限制
（每 2 秒最多 5 次請求），對這個系統的用量來說完全用不到擔心額度。

設定方式：Discord 頻道設定 → Integrations → Webhooks → New Webhook，
複製網址設進 secrets 的 DISCORD_WEBHOOK_URL。
"""

import os
import requests

DISCORD_MESSAGE_LIMIT = 2000  # Discord 單則文字訊息上限


def _get_webhook_url():
    try:
        import streamlit as st
        if "DISCORD_WEBHOOK_URL" in st.secrets:
            return st.secrets["DISCORD_WEBHOOK_URL"]
    except Exception:
        pass
    return os.environ.get("DISCORD_WEBHOOK_URL")


def send_discord_message(text, button_label=None, button_url=None, webhook_url=None):
    """
    傳送一則訊息到 Discord 頻道。如果有給 button_label/button_url，
    會用 embed 的方式在訊息下方附上一個可以點的連結（Discord webhook
    訊息本身不支援互動按鈕，用「附上連結」的方式達到類似效果）。
    """
    url = webhook_url or _get_webhook_url()
    if not url:
        raise RuntimeError(
            "缺少 DISCORD_WEBHOOK_URL，請在 secrets 或環境變數中設定。"
        )

    text = text[:DISCORD_MESSAGE_LIMIT]

    payload = {
        "content": text,
        # 避免不小心提到 @everyone 或身分組造成大量通知
        "allowed_mentions": {"parse": []},
    }

    if button_label and button_url:
        payload["embeds"] = [
            {
                "description": f"[{button_label}]({button_url})",
                "color": 0x2ECC71,
            }
        ]

    resp = requests.post(url, json=payload, timeout=10)
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"Discord 推播失敗 ({resp.status_code}): {resp.text}")
    return True
