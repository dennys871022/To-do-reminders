"""
reminder_check.py
-------------------
【省額度版：固定日期的早上彙整】
LINE 免費方案每月 200 則，而推播進群組會「每次 × 群組人數」計費（9 人群組 = 一次 9 則），
所以改成只在固定的日子發一則早上彙整：

- 發送日：週一、週三、週五、週日（SEND_WEEKDAYS），台灣時間 08:00 之後
- 內容：所有「未完成、30 天內到期」的事項，依分級（非常緊急／緊急／一般）排序合併成一則
- 當天已經發過就不重發；新增／編輯事項不會額外觸發推播，會併入下一個發送日
- 不怕 GitHub 排程延遲：08:00 之後 MAX_LATE_HOURS 小時內，只要還沒發就補發
- 晚上 21:00 ～ 早上 07:00 為安靜時段，不發送
- 額度保護：本月剩餘額度不夠再發一次時，不發送也不報錯，避免每 30 分鐘一封失敗通知信

分級規則由 urgency.py 提供（依結束日期自動判定）。

【測試用】環境變數 FORCE_SEND=true（GitHub Actions 手動執行時勾選 force）：
忽略日期、時段、額度保護，立刻發送，訊息開頭標示【測試】，不更新提醒紀錄。
"""

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import data_store
import urgency
import line_notify
from line_notify import send_line_group_message_with_button

TZ = ZoneInfo("Asia/Taipei")

# 發送日：0=週一 1=週二 2=週三 3=週四 4=週五 5=週六 6=週日
SEND_WEEKDAYS = {0, 2, 4, 6}

# 早上彙整的發送時間（台灣時間）
DIGEST_HOUR, DIGEST_MINUTE = 8, 0

# 過了發送時間之後，最晚還願意補發到幾小時內（GitHub 排程常延遲數小時）
MAX_LATE_HOURS = 10

# 安靜時段（台灣時間）：這段時間不發送
QUIET_START_HOUR = 21
QUIET_END_HOUR = 7

# 查不到群組人數時，用這個數字估算一次推播扣幾則額度
DEFAULT_RECIPIENTS = 9

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


def _build_batch_text(items_by_tier, test_mode=False):
    total = sum(len(v) for v in items_by_tier.values())
    head = "【測試】" if test_mode else ""
    lines = [f"{head}【代辦提醒】共 {total} 筆事項需要注意\n"]
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


def run(now_utc, force=False):
    now_local = now_utc.astimezone(TZ)
    today = now_local.date()
    slot = now_local.replace(hour=DIGEST_HOUR, minute=DIGEST_MINUTE, second=0, microsecond=0)
    print(f"[執行時間] 台灣時間 {now_local:%Y-%m-%d %H:%M}（週{'一二三四五六日'[now_local.weekday()]}）　強制測試模式：{force}")

    if not force:
        if now_local.weekday() not in SEND_WEEKDAYS:
            print("今天不是發送日（發送日：週一、三、五、日），不發送。")
            return
        if now_local.hour >= QUIET_START_HOUR or now_local.hour < QUIET_END_HOUR:
            print(f"目前是安靜時段（{QUIET_START_HOUR}:00～{QUIET_END_HOUR}:00），不發送。")
            return
        if now_local < slot:
            print(f"還沒到今天的發送時間 {DIGEST_HOUR:02d}:{DIGEST_MINUTE:02d}。")
            return
        late = now_local - slot
        if late > timedelta(hours=MAX_LATE_HOURS):
            print(f"已超過發送時間 {late.total_seconds()/3600:.1f} 小時，超過補發期限，今天不補發。")
            return

    todos = data_store.get_todos()
    print(f"[資料] 共讀到 {len(todos)} 筆代辦事項")

    if not force:
        for t in todos:
            last = _parse_iso(t.get("last_reminder_at"))
            if last and last.astimezone(TZ) >= slot:
                print(f"今天的彙整已在 {last.astimezone(TZ):%H:%M} 發送過，不重發。")
                return

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
        print(f"  - {label}：[{tier}] ✅列入彙整")
        t["_tier"] = tier
        due.append(t)

    print(f"[判斷結果] 本次彙整 {len(due)} 筆")
    if not due:
        print("目前沒有需要提醒的事項。")
        return

    if not force:
        status = line_notify.get_quota_status()
        recipients = line_notify.get_group_member_count(default=DEFAULT_RECIPIENTS)
        limit, used = status.get("limit"), status.get("used")
        print(f"[額度] 本月已用 {used} / 上限 {limit}；群組 {recipients} 人，一次推播約扣 {recipients} 則")
        if limit is not None and used is not None and limit - used < recipients:
            print(f"⚠️ 本月剩餘額度 {limit - used} 則，不夠再發一次（需 {recipients} 則），本次不發送。")
            return

    items_by_tier = {}
    for t in due:
        items_by_tier.setdefault(t["_tier"], []).append(t)
    text = _build_batch_text(items_by_tier, test_mode=force)

    try:
        send_line_group_message_with_button(
            text, button_label="📋 開啟代辦系統", button_url=APP_URL
        )
    except Exception as e:
        print(f"推播失敗，LINE 訊息沒有送出：{e}")
        raise SystemExit(1)

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
                f"但更新「上次提醒時間」失敗（{e}），今天稍後有機率重複發送。"
            )


def main():
    force = os.environ.get("FORCE_SEND", "").strip().lower() == "true"
    run(datetime.now(timezone.utc), force=force)


if __name__ == "__main__":
    main()
