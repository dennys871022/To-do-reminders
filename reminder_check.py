"""
reminder_check.py
-------------------
【這版改版重點：不怕 GitHub 排程延遲】
舊版只在「執行當下剛好落在提醒時段前後 20 分鐘內」才發送，但 GitHub Actions 的
定時觸發常常延遲（實測過差一小時以上），一延遲就整個錯過、什麼都沒發。

新版改成「補發」邏輯：
- 每個分級有固定的提醒時段（台灣時間）：
    非常緊急：08:00 / 13:30 / 17:00　緊急：每天 08:00　一般：每 3 天 08:00
- 每次執行時，找出「今天已經過了的最近一個時段」，如果這個時段還沒提醒過
  （上次提醒時間比該時段早），而且離該時段不超過 4 小時，就補發一次。
- 所以排程可以安排得比較密（見 reminder.yml），不管哪一次先跑到、跑得多晚，
  每個時段都只會發一次，不會漏、也不會重複。

分級規則由 urgency.py 提供（依結束日期自動判定）。
同一次執行如果有多筆事項到期，合併成一則 LINE 訊息（附開啟 App 按鈕）。
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import data_store
import urgency
from line_notify import send_line_group_message_with_button

TZ = ZoneInfo("Asia/Taipei")

# 錯過提醒時段後，最晚還願意補發到幾小時內（超過就不補，避免半夜才收到早上的提醒）
MAX_LATE_HOURS = 4

# 你的 Streamlit App 網址，按鈕點下去會開啟這裡
APP_URL = "https://to-do-reminders-kcltnn6fycdrcr2pd5t4rh.streamlit.app/"


def _is_completed(todo):
    return str(todo.get("completed", "")).strip().upper() == "TRUE"


def _parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _latest_slot(now_local, slots):
    """今天已經過了的、最近的一個提醒時段；還沒到任何時段則回傳 None。"""
    passed = []
    for h, m in slots:
        s = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        if s <= now_local:
            passed.append(s)
    return max(passed) if passed else None


def _is_due(todo, tier, now_local):
    slot = _latest_slot(now_local, urgency.TIER_SLOTS[tier])
    if slot is None:
        return False
    if now_local - slot > timedelta(hours=MAX_LATE_HOURS):
        return False

    last = _parse_iso(todo.get("last_reminder_at"))
    if last is None:
        return True  # 從來沒提醒過
    last_local = last.astimezone(TZ)

    if tier == "一般":
        # 每 3 天提醒一次
        return (now_local.date() - last_local.date()).days >= 3
    # 非常緊急 / 緊急：這個時段還沒提醒過才發
    return last_local < slot


def _build_batch_text(items_by_tier):
    total = sum(len(v) for v in items_by_tier.values())
    lines = [f"【代辦提醒】共 {total} 筆事項需要注意\n"]
    for tier in urgency.TIER_ORDER:
        for t in items_by_tier.get(tier, []):
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
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(TZ)
    today = now_local.date()
    print(f"[執行時間] 台灣時間 {now_local:%Y-%m-%d %H:%M}")

    todos = data_store.get_todos()
    print(f"[資料] 共讀到 {len(todos)} 筆代辦事項")

    due = []
    skipped_summary = {"已完成": 0, "超過30天尚未進入提醒範圍": 0, "這個時段已提醒過或不在補發時間內": 0}
    for t in todos:
        if _is_completed(t):
            skipped_summary["已完成"] += 1
            continue

        tier = urgency.classify_tier(t.get("end_date"), today)
        if tier is None:
            skipped_summary["超過30天尚未進入提醒範圍"] += 1
            continue

        if not _is_due(t, tier, now_local):
            skipped_summary["這個時段已提醒過或不在補發時間內"] += 1
            continue

        t["_tier"] = tier
        due.append(t)

    print(f"[判斷結果] 需提醒 {len(due)} 筆；略過：{skipped_summary}")
    if not due:
        print("目前沒有需要提醒的事項。")
        return

    items_by_tier = {}
    for t in due:
        items_by_tier.setdefault(t["_tier"], []).append(t)

    text = _build_batch_text(items_by_tier)

    try:
        send_line_group_message_with_button(
            text, button_label="📋 開啟代辦系統", button_url=APP_URL
        )
    except Exception as e:
        # 真的沒發出去，不標記，下次執行會自動重試
        print(f"推播失敗，LINE 訊息沒有送出：{e}")
        raise SystemExit(1)  # 讓 workflow 顯示紅色失敗，才不會又默默綠勾勾

    print(f"已推播 1 則訊息（涵蓋 {len(due)} 筆事項）。")
    for t in due:
        try:
            data_store.mark_reminder_sent(t["id"], now_utc.isoformat())
        except Exception as e:
            print(
                f"⚠️ 警告：id={t['id']} 的提醒訊息已送出，"
                f"但更新「上次提醒時間」失敗（{e}），下次執行有機率重複提醒這筆。"
            )


if __name__ == "__main__":
    main()
