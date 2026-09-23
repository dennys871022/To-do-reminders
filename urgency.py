"""
urgency.py
-----------
共用的「緊急程度自動分級」邏輯。app.py（畫面顯示用）跟 reminder_check.py
（排程判斷用）都從這裡取用同一套規則，避免兩邊各寫一份導致邏輯兜不起來。

分級規則（依 end_date 距離「今天」還剩幾天決定）：
- 非常緊急：3 天內到期（含已過期）→ 一天提醒 3 次：08:00 / 13:30 / 17:00
- 緊急：本週內到期（4~7 天）→ 一天提醒 1 次：08:00
- 一般：2 週以上、至本月內到期（8~30 天）→ 每 3 天提醒 1 次：08:00
- 超過 30 天以上到期：尚未進入提醒範圍（回傳 None）
"""

from datetime import date

TIER_SLOTS = {
    "非常緊急": [(8, 0), (13, 30), (17, 0)],
    "緊急": [(8, 0)],
    "一般": [(8, 0)],
}

TIER_ICONS = {
    "非常緊急": "🔴",
    "緊急": "🟠",
    "一般": "🟡",
}

TIER_ORDER = ["非常緊急", "緊急", "一般"]


def classify_tier(end_date_str, today=None):
    """
    依到期日距離今天還剩幾天，回傳分級名稱（"非常緊急"/"緊急"/"一般"）；
    超過 30 天回傳 None，代表還沒進入提醒範圍。
    """
    if today is None:
        today = date.today()
    try:
        end_date = date.fromisoformat(end_date_str)
    except (ValueError, TypeError):
        return None

    days_remaining = (end_date - today).days

    if days_remaining <= 3:
        return "非常緊急"
    elif days_remaining <= 7:
        return "緊急"
    elif days_remaining <= 30:
        return "一般"
    else:
        return None


def tier_label(end_date_str, today=None):
    """回傳給畫面顯示用的文字，例如「🔴 非常緊急」；超出範圍回傳「⚪ 尚未進入提醒範圍」。"""
    tier = classify_tier(end_date_str, today)
    if tier is None:
        return "⚪ 尚未進入提醒範圍"
    return f"{TIER_ICONS[tier]} {tier}"
