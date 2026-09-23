"""
reminder_check.py
-------------------
【這版改版重點】
提醒規則改成「依到期日自動分級 + 固定時段推播」，不再依賴使用者手動選的
「緊急程度」欄位，也不再是「距離上次提醒滿幾小時再提醒一次」的邏輯。

分級規則（依 end_date 距離「今天」還剩幾天決定）：
- 非常緊急：3 天內到期（含已過期）→ 一天提醒 3 次：08:00 / 13:30 / 17:00
- 緊急：本週內到期（4~7 天）→ 一天提醒 1 次：08:00
- 一般：2 週以上、至本月內到期（8~30 天）→ 每 3 天提醒 1 次：08:00
- 超過 30 天以上到期：暫不提醒
- 已完成的事項不提醒

這支腳本必須搭配 GitHub Actions 在「台灣時間 08:00 / 13:30 / 17:00」各觸發一次
（見 .github/workflows/reminder.yml，共 3 條 cron）。
每次執行時會用「現在的台灣時間」去比對，只有真正命中該時段的事項才會被收進本次提醒，
同一次執行如果有多筆事項到期，會合併成一則 LINE 訊息發送，節省每月用量。

注意：app.py 裡原本讓使用者手動選「緊急程度」的下拉選單，現在對提醒排程
已經沒有作用了（純粹只是紀錄用的欄位），實際分級完全由到期日自動計算。
"""

from datetime import datetime, date
from zoneinfo import ZoneInfo

import data_store
from line_notify import send_line_group_message

TZ = ZoneInfo("Asia/Taipei")

# 各分級：一天內要提醒的時段（時, 分），台灣時間
TIER_SLOTS = {
    "非常緊急": [(8, 0), (13, 30), (17, 0)],
    "緊急": [(8, 0)],
    "一般": [(8, 0)],
}

# 判斷「現在」是否命中某個時段，允許正負 10 分鐘 的誤差
# （避免 GitHub Actions 排程稍微延遲觸發，就整個錯過那個時段）
SLOT_TOLERANCE_MINUTES = 10


def _is_completed(todo):
    return str(todo.get("completed", "")).strip().upper() == "TRUE"


def _classify_tier(end_date_str, today):
    """依到期日距離今天還剩幾天，回傳分級名稱；超過範圍回傳 None（暫不提醒）。"""
    try:
        end_date = date.fromisoformat(end_date_str)
    except (ValueError, TypeError):
        return None

    days_remaining = (end_date - today).days

    if days_remaining <= 3:
        return "非常緊急"  # 含已過期的事項
    elif days_remaining <= 7:
        return "緊急"
    elif days_remaining <= 30:
        return "一般"
    else:
        return None


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
    for tier in ["非常緊急", "緊急", "一般"]:
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

        tier = _classify_tier(t.get("end_date"), today)
        if tier is None:
            continue

        if not _matches_current_slot(now_local, TIER_SLOTS[tier]):
            continue

        if tier == "一般":
            # 每 3 天才提醒一次：檢查距離上次提醒是否已滿 3 天
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
        send_line_group_message(text)
        for t in due:
            data_store.mark_reminder_sent(t["id"], now_utc.isoformat())
        print(f"已推播 1 則訊息（涵蓋 {len(due)} 筆事項）並更新提醒時間。")
    except Exception as e:
        print(f"推播失敗：{e}")


if __name__ == "__main__":
    main()
