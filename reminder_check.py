"""
reminder_check.py
-------------------
【改版：用時間決定內容，不再用緊急程度決定頻率】

- 08:00：完整晨報。分兩段——① 今天已有派工的事項（不分緊急程度，顯示對應移工）
  ② 其餘未派工事項，依非常緊急／緊急／一般分類列出
- 13:30：只列「非常緊急」的事項，當天再提醒一次最急的
- 17:00：完整晚報，內容格式跟 08:00 相同，收工前再看一次全貌，方便安排隔天派工

已完成、或超過 30 天到期的事項不列入（超過 30 天的太早，還不用管）。
晚上 21:00 ～ 早上 07:00 為安靜時段不發送。

這支腳本現在搭配外部排程服務（例如 cron-job.org）在台灣時間 08:00／13:30／17:00
準時呼叫 GitHub 的 workflow_dispatch 來觸發，不再依賴 GitHub 自己的 schedule
（已證實常被節流、不準時）。MAX_LATE_HOURS 只是留一點緩衝，應付外部服務偶爾的
極小延遲，不是主要的準時機制。

【測試用】環境變數 FORCE_SEND=true（GitHub Actions 手動執行時勾選 force）：
忽略時段，立刻發送完整報告，訊息開頭標示【測試】。
"""

import os
from datetime import datetime, timedelta, timezone, date as date_cls
from zoneinfo import ZoneInfo

import data_store
import urgency
from discord_notify import send_discord_message

TZ = ZoneInfo("Asia/Taipei")

# 三個固定時段（台灣時間）
TIME_SLOTS = [(8, 0), (13, 30), (17, 0)]

# 時段過了之後，最晚還願意算數的緩衝時間（外部排程服務應該都很準，這裡抓小一點）
MAX_LATE_HOURS = 2

# 安靜時段（台灣時間）：這段時間不發送
QUIET_START_HOUR = 19
QUIET_END_HOUR = 7

# 你的 Streamlit App 網址，附在訊息下方
APP_URL = "https://to-do-reminders-kcltnn6fycdrcr2pd5t4rh.streamlit.app/"


def _is_completed(todo):
    return str(todo.get("completed", "")).strip().upper() == "TRUE"


def _current_slot(now_local):
    """回傳現在對應到的那個時段（datetime），在任何時段的 MAX_LATE_HOURS 內才算數；
    都不符合就回傳 None。"""
    candidates = []
    for h, m in TIME_SLOTS:
        s = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        if s <= now_local and (now_local - s) <= timedelta(hours=MAX_LATE_HOURS):
            candidates.append(s)
    return max(candidates) if candidates else None


def _today_workers(todo_id, today):
    """這筆事項「今天」有效的派工紀錄裡，指派了哪些移工（跨好幾筆派工紀錄會合併去重）。"""
    workers = set()
    for d in data_store.get_dispatches_for_todo(todo_id):
        try:
            ds = date_cls.fromisoformat(d["start_date"])
            de = date_cls.fromisoformat(d["end_date"])
        except (ValueError, TypeError):
            continue
        if ds <= today <= de:
            workers.update(data_store.split_workers(d.get("workers", "")))
    return workers


def _item_lines(t, tier, icon, workers):
    date_range = t["start_date"]
    if t["start_date"] != t["end_date"]:
        date_range += f"~{t['end_date']}"

    title = f"{icon} **{t['task']}**"
    if tier:
        title += f"　［{tier}］"
    lines = [title]

    meta = f"　🏗️ {t['work_item']}／{t['type']}"
    if t.get("location"):
        meta += f"　📍 {t['location']}"
    lines.append(meta)
    lines.append(f"　📅 期限：{date_range}")

    if workers:
        lines.append(f"　👷 今日移工：{'、'.join(sorted(workers))}")

    return lines


def _build_full_report(active_todos, today, test_mode=False):
    """08:00／17:00 用的完整報告：今日派工段落 + 依緊急程度的未派工段落。"""
    dispatched = []  # (tier, todo, workers)
    pending_by_tier = {tier: [] for tier in urgency.TIER_ORDER}

    for t in active_todos:
        tier = urgency.classify_tier(t.get("end_date"), today)
        workers = _today_workers(t["id"], today)
        if workers:
            dispatched.append((tier, t, workers))
        elif tier:
            pending_by_tier[tier].append(t)

    total = len(dispatched) + sum(len(v) for v in pending_by_tier.values())
    head = "【測試】" if test_mode else ""
    lines = [f"{head}**【代辦提醒】共 {total} 筆事項需要注意**"]

    if dispatched:
        dispatched.sort(
            key=lambda x: urgency.TIER_ORDER.index(x[0]) if x[0] in urgency.TIER_ORDER else len(urgency.TIER_ORDER)
        )
        lines.append("")
        lines.append("📌 **今日派工項目**")
        for tier, t, workers in dispatched:
            icon = urgency.TIER_ICONS.get(tier, "⚪") if tier else "⚪"
            lines.append("")
            lines.extend(_item_lines(t, tier, icon, workers))

    if any(pending_by_tier.values()):
        lines.append("")
        lines.append("⏰ **依緊急程度（尚未安排移工）**")
        for tier in urgency.TIER_ORDER:
            for t in pending_by_tier[tier]:
                lines.append("")
                lines.extend(_item_lines(t, tier, urgency.TIER_ICONS.get(tier, "⚪"), None))

    return total, "\n".join(lines)


def _build_urgent_only_text(active_todos, today, test_mode=False):
    """13:30 用的簡短提醒：只列「非常緊急」的事項。"""
    items = []
    for t in active_todos:
        tier = urgency.classify_tier(t.get("end_date"), today)
        if tier == "非常緊急":
            items.append((t, _today_workers(t["id"], today)))

    head = "【測試】" if test_mode else ""
    lines = [f"{head}**【午間提醒】非常緊急事項共 {len(items)} 筆**"]
    for t, workers in items:
        lines.append("")
        lines.extend(_item_lines(t, "非常緊急", "🔴", workers))

    return len(items), "\n".join(lines)


def run(now_utc, force=False):
    now_local = now_utc.astimezone(TZ)
    today = now_local.date()
    print(f"[執行時間] 台灣時間 {now_local:%Y-%m-%d %H:%M}　強制測試模式：{force}")

    if force:
        report_type = "full"
    else:
        in_quiet = now_local.hour >= QUIET_START_HOUR or now_local.hour < QUIET_END_HOUR
        if in_quiet:
            print(f"目前是安靜時段（{QUIET_START_HOUR}:00～{QUIET_END_HOUR}:00），不發送。")
            return

        slot = _current_slot(now_local)
        if slot is None:
            print("目前不在任何提醒時段附近（08:00／13:30／17:00），不發送。")
            return
        slot_label = f"{slot.hour:02d}:{slot.minute:02d}"
        print(f"[對應時段] {slot_label}")
        report_type = "urgent_only" if slot_label == "13:30" else "full"

    todos = data_store.get_todos()
    active_todos = [t for t in todos if not _is_completed(t)]
    print(f"[資料] 共 {len(todos)} 筆代辦事項，{len(active_todos)} 筆進行中")

    if report_type == "full":
        total, text = _build_full_report(active_todos, today, test_mode=force)
    else:
        total, text = _build_urgent_only_text(active_todos, today, test_mode=force)

    print(f"[判斷結果] 本次報告涵蓋 {total} 筆事項")
    if total == 0:
        print("目前沒有需要提醒的事項。")
        return

    try:
        send_discord_message(text, button_label="📋 開啟代辦系統", button_url=APP_URL)
    except Exception as e:
        print(f"推播失敗，Discord 訊息沒有送出：{e}")
        raise SystemExit(1)  # 讓 workflow 顯示紅色失敗，方便及早發現

    print(f"已推播 1 則訊息（涵蓋 {total} 筆事項）。")

    if force:
        print("（測試模式：不更新提醒紀錄）")
        return

    # 這個新模型不再用 last_reminder_at 來決定「要不要發」，純粹留作歷史參考用。
    for t in active_todos:
        try:
            data_store.mark_reminder_sent(t["id"], now_utc.isoformat())
        except Exception as e:
            print(f"⚠️ 警告：id={t['id']} 更新提醒時間失敗（{e}），不影響本次推播結果。")


def main():
    force = os.environ.get("FORCE_SEND", "").strip().lower() == "true"
    run(datetime.now(timezone.utc), force=force)


if __name__ == "__main__":
    main()
