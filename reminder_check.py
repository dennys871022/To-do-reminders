"""
reminder_check.py
-------------------
這支腳本本身不含任何排程邏輯，只是「跑一次、檢查一次」。
真正的「定時執行」交給 GitHub Actions（見 .github/workflows/reminder.yml），
每次觸發就跑一次這支腳本。

判斷邏輯：
- 非常緊急：每 2 小時提醒一次
- 緊急：每 4 小時提醒一次
- 一般：不提醒
- 已完成的事項不提醒
- 從來沒提醒過的事項（last_reminder_at 是空的），不管建立多久了，
  這次執行就會立刻提醒一次（第一次提醒不看建立時間，讓緊急事項一建立、
  排程一跑就會通知，符合直覺）
- 提醒過之後，之後每次都以「上次提醒時間」為基準，超過對應間隔才再提醒一次
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

        last_reminder = _parse_iso(t.get("last_reminder_at"))
        if last_reminder is None:
            # 從來沒提醒過：不管建立多久了，馬上提醒一次
            due.append(t)
            continue

        elapsed_hours = (now - last_reminder).total_seconds() / 3600
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
