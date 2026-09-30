"""
reminder_check.py
-------------------
【改用 Discord 後恢復原始設計：一天最多 3 次，不再需要額度保護】
Discord webhook 沒有月則數上限，不用再像 LINE 那樣把發送次數壓到最低，
所以恢復成最初設計：

- 非常緊急（3天內到期）：一天提醒 3 次：08:00 / 13:30 / 17:00
- 緊急（本週內到期）：一天提醒 1 次：08:00
- 一般（本月內到期）：每 3 天提醒 1 次：08:00
- 已完成、或超過 30 天到期的事項不提醒

不怕 GitHub 排程延遲：每個時段過了之後，MAX_LATE_HOURS 小時內只要還沒
提醒過就會補發；晚上 19:00 ～ 早上 07:00 為安靜時段不發送。

同一次執行有多筆事項到期，合併成一則 Discord 訊息（附開啟 App 連結）。

【測試用】環境變數 FORCE_SEND=true（GitHub Actions 手動執行時勾選 force）：
忽略時段與安靜時段，立刻把所有「未完成且 30 天內到期」的事項發到頻道，
訊息開頭標示【測試】，且不會更新提醒紀錄，不影響正式排程。
"""

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import data_store
import urgency
from discord_notify import send_discord_message

TZ = ZoneInfo("Asia/Taipei")

# 錯過提醒時段後，最晚還願意補發到幾小時內（GitHub 排程常延遲數小時）
MAX_LATE_HOURS = 6

# 安靜時段（台灣時間）：這段時間不發送
QUIET_START_HOUR = 19
QUIET_END_HOUR = 7

# 你的 Streamlit App 網址，附在訊息下方
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
    passed = [
        now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        for h, m in slots
        if now_local.replace(hour=h, minute=m, second=0, microsecond=0) <= now_local
    ]
    return max(passed) if passed else None


def _check_due(todo, tier, now_local):
    """回傳 (是否要提醒, 原因說明)，方便寫進 log 除錯。"""
    slot = _latest_slot(now_local, urgency.TIER_SLOTS[tier])
    if slot is None:
        return False, "今天還沒到第一個提醒時段"
    late = now_local - slot
    if late > timedelta(hours=MAX_LATE_HOURS):
        return False, f"最近時段 {slot:%H:%M} 已過 {late.total_seconds()/3600:.1f} 小時，超過補發期限"

    last = _parse_iso(todo.get("last_reminder_at"))
    if last is None:
        return True, f"從未提醒過（時段 {slot:%H:%M}）"
    last_local = last.astimezone(TZ)

    if tier == "一般":
        days = (now_local.date() - last_local.date()).days
        if days >= 3:
            return True, f"一般事項距上次提醒 {days} 天"
        return False, f"一般事項每 3 天一次，距上次提醒才 {days} 天"

    if last_local < slot:
        return True, f"時段 {slot:%H:%M} 尚未提醒（上次 {last_local:%m/%d %H:%M}）"
    return False, f"時段 {slot:%H:%M} 已提醒過（上次 {last_local:%m/%d %H:%M}）"


def _build_batch_text(items_by_tier, test_mode=False):
    total = sum(len(v) for v in items_by_tier.values())
    head = "【測試】" if test_mode else ""
    lines = [f"{head}**【代辦提醒】共 {total} 筆事項需要注意**\n"]
    for tier in urgency.TIER_ORDER:
        for t in items_by_tier.get(tier, []):
            loc = f"／{t.get('location')}" if t.get("location") else ""
            date_range = t["start_date"]
            if t["start_date"] != t["end_date"]:
                date_range += f"~{t['end_date']}"
            lines.append(
                f"・[{tier}] {t['task']}\n"
                f"　{t['work_item']}／{t['type']}{loc}／期限 {date_range}"
            )
    return "\n".join(lines)


def run(now_utc, force=False):
    now_local = now_utc.astimezone(TZ)
    today = now_local.date()
    print(f"[執行時間] 台灣時間 {now_local:%Y-%m-%d %H:%M}　強制測試模式：{force}")

    if not force:
        in_quiet = now_local.hour >= QUIET_START_HOUR or now_local.hour < QUIET_END_HOUR
        if in_quiet:
            print(f"目前是安靜時段（{QUIET_START_HOUR}:00～{QUIET_END_HOUR}:00），不發送。")
            return

    todos = data_store.get_todos()
    print(f"[資料] 共讀到 {len(todos)} 筆代辦事項")

    due = []
    for t in todos:
        label = f"{str(t.get('task', ''))[:14]}（期限 {t.get('end_date')}）"
        if _is_completed(t):
            print(f"  - {label}：已完成，略過")
            continue

        tier = urgency.classify_tier(t.get("end_date"), today)
        if tier is None:
            print(f"  - {label}：超過 30 天尚未進入提醒範圍，略過")
            continue

        if force:
            ok, reason = True, "強制測試模式"
        else:
            ok, reason = _check_due(t, tier, now_local)
        print(f"  - {label}：[{tier}] {'✅提醒' if ok else '略過'}：{reason}")
        if ok:
            t["_tier"] = tier
            due.append(t)

    print(f"[判斷結果] 需提醒 {len(due)} 筆")
    if not due:
        print("目前沒有需要提醒的事項。")
        return

    items_by_tier = {}
    for t in due:
        items_by_tier.setdefault(t["_tier"], []).append(t)

    text = _build_batch_text(items_by_tier, test_mode=force)

    try:
        send_discord_message(text, button_label="📋 開啟代辦系統", button_url=APP_URL)
    except Exception as e:
        print(f"推播失敗，Discord 訊息沒有送出：{e}")
        raise SystemExit(1)  # 讓 workflow 顯示紅色失敗，方便及早發現

    print(f"已推播 1 則訊息（涵蓋 {len(due)} 筆事項）。")

    if force:
        print("（測試模式：不更新提醒紀錄）")
        return

    for t in due:
        try:
            data_store.mark_reminder_sent(t["id"], now_utc.isoformat())
        except Exception as e:
            print(
                f"⚠️ 警告：id={t['id']} 的提醒訊息已送出，"
                f"但更新「上次提醒時間」失敗（{e}），下次執行有機率重複提醒這筆。"
            )


def main():
    force = os.environ.get("FORCE_SEND", "").strip().lower() == "true"
    run(datetime.now(timezone.utc), force=force)


if __name__ == "__main__":
    main()
