"""
reminder_check.py
-------------------
分級規則現在統一由 urgency.py 提供，跟 app.py 共用同一套邏輯，兩邊不會兜不起來。

這支腳本必須搭配 GitHub Actions 在「台灣時間 08:00 / 13:30 / 17:00」各觸發一次
（見 .github/workflows/reminder.yml，共 3 條 cron）。每次執行時會用「現在的
台灣時間」去比對，只有真正命中該時段的事項才會被收進本次提醒；同一次執行如果
有多筆事項到期，會合併成一則 LINE 訊息（附開啟 App 按鈕）發送，節省每月用量。
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import data_store
import urgency
from line_notify import send_line_group_message_with_button

TZ = ZoneInfo("Asia/Taipei")

# 判斷「現在」是否命中某個時段，允許正負 20 分鐘 的誤差
# （GitHub Actions 的 schedule 觸發本來就不保證準時，尖峰時段延遲 15~30 分鐘很常見，
#  容許範圍抓寬一點，避免延遲導致整個錯過時段而漏發）
SLOT_TOLERANCE_MINUTES = 20

# 你的 Streamlit App 網址，按鈕點下去會開啟這裡
# ⚠️ 請確認這個網址是不是你目前實際部署的網址，如果不是請改成正確的
APP_URL = "https://to-do-reminders-kcltnn6fycdrcr2pd5t4rh.streamlit.app/"


def _is_completed(todo):
    return str(todo.get("completed", "")).strip().upper() == "TRUE"


def _matches_current_slot(now_local, slots):
    now_minutes = now_local.hour * 60 + now_local.minute
    for h, m in slots:
        target_minutes = h * 60 + m
        if abs(now_minutes - target_minutes) <= SLOT_TOLERANCE_MINUTES:
            return True
    return False


def _days_since(last_iso, now_utc):
    if not last_iso:
        return None
    try:
        last = datetime.fromisoformat(last_iso)
    except ValueError:
        return None
    return (now_utc - last).total_seconds() / 86400


def _build_batch_text(items_by_tier):
    total = sum(len(v) for v in items_by_tier.values())
    lines = [f"【代辦提醒】共 {total} 筆事項需要注意\n"]
    for tier in urgency.TIER_ORDER:
        items = items_by_tier.get(tier, [])
        for t in items:
            loc = f"／{t.get('location')}" if t.get("location") else ""
            date_range = t["start_date"]
            if t["start_date"] != t["end_date"]:
                date_range += f"~{t['end_date']}"
            lines.append(
                f"・[{tier}] {t['task']}\n"
                f"  {t['work_item']}／{t['type']}{loc}／期限 {date_range}"
            )
    return "\n".join(lines)


def main():
    now_utc = datetime.now(ZoneInfo("UTC"))
    now_local = now_utc.astimezone(TZ)
    today = now_local.date()

    todos = data_store.get_todos()

    due = []
    for t in todos:
        if _is_completed(t):
            continue

        tier = urgency.classify_tier(t.get("end_date"), today)
        if tier is None:
            continue

        if not _matches_current_slot(now_local, urgency.TIER_SLOTS[tier]):
            continue

        if tier == "一般":
            days_since = _days_since(t.get("last_reminder_at"), now_utc)
            if days_since is not None and days_since < 3:
                continue

        t["_tier"] = tier
        due.append(t)

    if not due:
        print(f"[{now_local:%Y-%m-%d %H:%M} 台灣時間] 目前沒有命中提醒時段的事項。")
        return

    items_by_tier = {}
    for t in due:
        items_by_tier.setdefault(t["_tier"], []).append(t)

    text = _build_batch_text(items_by_tier)

    try:
        send_line_group_message_with_button(
            text, button_label="📋 開啟代辦系統", button_url=APP_URL
        )
        for t in due:
            data_store.mark_reminder_sent(t["id"], now_utc.isoformat())
        print(f"已推播 1 則訊息（涵蓋 {len(due)} 筆事項）並更新提醒時間。")
    except Exception as e:
        print(f"推播失敗：{e}")


if __name__ == "__main__":
    main()
