"""
reminder_check.py
-------------------
這支腳本本身不含任何排程邏輯，只是「跑一次、檢查一次」。
真正的「定時執行」交給 GitHub Actions（見 .github/workflows/reminder.yml），
每次觸發就跑一次這支腳本。

判斷邏輯（沿用原本 HTML 版本的設計）：
- 非常緊急：每 2 小時提醒一次
- 緊急：每 4 小時提醒一次
- 一般：不提醒
- 已完成的事項不提醒
- 以「上次提醒時間」(last_reminder_at) 或「建立時間」(created_at) 為基準，
  超過對應間隔就再推播一次，並更新 last_reminder_at
"""

from datetime import datetime, timezone

import data_store
from line_notify import send_line_group_message, build_reminder_text

URGENCY_INTERVAL_HOURS = {
    "非常緊急": 2,
    "緊急": 4,
    "一般": None,  # 不提醒
}


def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _is_completed(todo):
    return str(todo.get("completed", "")).strip().upper() == "TRUE"


def main():
    now = datetime.now(timezone.utc)
    todos = data_store.get_todos()

    due = []
    for t in todos:
        if _is_completed(t):
            continue

        interval_hours = URGENCY_INTERVAL_HOURS.get(t.get("urgency"))
        if not interval_hours:
            continue

        base = _parse_iso(t.get("last_reminder_at")) or _parse_iso(t.get("created_at"))
        if base is None:
            # 沒有任何時間戳可以比對，保守起見直接視為到期，先提醒一次
            due.append(t)
            continue

        elapsed_hours = (now - base).total_seconds() / 3600
        if elapsed_hours >= interval_hours:
            due.append(t)

    if not due:
        print("目前沒有需要提醒的事項。")
        return

    for t in due:
        text = build_reminder_text(t)
        try:
            send_line_group_message(text)
            data_store.mark_reminder_sent(t["id"], now.isoformat())
            print(f"已推播並更新提醒時間：{t['id']}")
        except Exception as e:
            print(f"推播失敗（id={t['id']}）：{e}")

    print(f"本次共推播 {len(due)} 則提醒。")


if __name__ == "__main__":
    main()
