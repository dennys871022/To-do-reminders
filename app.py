"""
app.py
-------
營造管理系統 - Streamlit 版本

【效能與多人使用的設計重點】
1. 用「頁面切換」取代 st.tabs：st.tabs 會讓 5 個分頁的程式每次互動都全部執行一遍，
   現在只執行目前所在的那一頁。
2. 儲存／刪除時顯示「儲存中…」，並在存檔後顯示成功訊息（原本 st.rerun() 會把訊息洗掉）。
3. 連點保護：畫面轉圈圈時使用者常會連點送出，10 秒內內容完全相同的重複送出會被擋掉。
4. Discord 即時通知改在背景發送，不讓使用者等它。
5. 遇到 Google 試算表暫時忙碌時顯示友善提示，而不是整頁紅色錯誤。
"""

import threading
import time
import traceback

import streamlit as st
import pandas as pd
from datetime import date
from zoneinfo import ZoneInfo

import data_store
import urgency
import discord_notify

st.set_page_config(page_title="營造管理系統", page_icon="🏗️", layout="wide")

TZ = ZoneInfo("Asia/Taipei")


def _init_state():
    if "editing_id" not in st.session_state:
        st.session_state.editing_id = None


def _flash(message):
    """存檔後馬上 st.rerun() 會把當下的 st.success 洗掉，所以先記起來，下一次畫面更新時再顯示。"""
    st.session_state["_flash"] = message


def _show_flash():
    msg = st.session_state.pop("_flash", None)
    if msg:
        st.success(msg)
    errors = st.session_state.get("_notify_errors")
    while errors:
        st.warning(f"Discord 即時通知發送失敗（{errors.pop(0)}）。資料已經儲存，不受影響。")


def _is_duplicate_submit(token, window_seconds=10):
    """畫面轉圈圈時使用者常會連點好幾次送出，這裡把 10 秒內「內容完全相同」的重複送出擋掉，
    避免產生重複資料、重複通知。若這次儲存失敗，main() 會把紀錄清掉，讓使用者可以馬上重試。"""
    now = time.time()
    last = st.session_state.get("_last_submit")
    if last and last[0] == token and now - last[1] < window_seconds:
        return True
    st.session_state["_last_submit"] = (token, now)
    return False


def _send_discord_async(text):
    """在背景發送 Discord 訊息，不讓使用者等它；失敗時記錄起來，下次畫面更新時提醒。"""
    errors = st.session_state.setdefault("_notify_errors", [])
    try:
        webhook_url = discord_notify._get_webhook_url()  # 在主執行緒先取好，背景執行緒不碰 st
    except Exception as e:
        errors.append(str(e))
        return

    def _worker():
        try:
            discord_notify.send_discord_message(text, webhook_url=webhook_url)
        except Exception as e:  # 通知失敗不影響資料
            errors.append(str(e))

    threading.Thread(target=_worker, daemon=True).start()


def _select_with_add(label, category, key):
    """純下拉選單，選項存在 Google Sheets 的 Options 分頁。
    要新增新選項，請到上方「⚙️ 管理工項／類型選單」新增（不能在這個表單裡直接打新選項，
    因為 Streamlit 表單裡的元件不會即時刷新，體驗上會像打不進去）。"""
    options = data_store.get_options(category)
    if not options:
        st.warning(f"目前沒有{label}選項，請先到上方「⚙️ 管理選單」新增。")
        return ""
    return st.selectbox(label, options, key=key)


def render_option_manager():
    """管理工項／類型／工程師／移工選項：新增、刪除都在這裡做（表單外，才能即時刷新）。
    這裡有幾十個按鈕，所以只有在使用者打開開關時才會建立，平常不拖慢頁面。"""
    with st.container(border=True):
        _render_option_column("work_item", "工項")
        _render_option_column("type", "類型")
        _render_option_column("engineer", "工程師（指派人）")
        _render_option_column("worker", "移工")


def _render_option_column(category, label):
    st.markdown(f"**{label}**")
    for v in data_store.get_options(category):
        c1, c2 = st.columns([5, 1])
        c1.write(v)
        if c2.button("🗑", key=f"del_{category}_{v}"):
            with st.spinner("刪除中…"):
                data_store.delete_option(category, v)
            st.rerun()
    with st.form(f"add_{category}_form", clear_on_submit=True):
        c1, c2 = st.columns([4, 1])
        new_val = c1.text_input(f"新增{label}", key=f"new_{category}_input", label_visibility="collapsed", placeholder=f"新增{label}")
        if c2.form_submit_button("新增"):
            if new_val.strip():
                with st.spinner("新增中…"):
                    data_store.add_option(category, new_val.strip())
                st.rerun()
    st.divider()


def _notify_discord_instant(todo, action):
    """新增或編輯代辦事項後，在背景發一則 Discord 通知（跟排程的每日報告是獨立的兩條路）。"""
    try:
        today = datetime_now_taipei_date()
        tier = urgency.classify_tier(todo.get("end_date"), today) or "一般"
        icon = "🆕" if action == "新增" else "✏️"

        date_range = todo["start_date"]
        if todo["start_date"] != todo["end_date"]:
            date_range += f"~{todo['end_date']}"

        lines = [
            f"{icon} **{action}代辦事項** ［{tier}］",
            f"{todo['task']}",
            f"工項：{todo['work_item']}／類型：{todo['type']}",
        ]
        if todo.get("location"):
            lines.append(f"地點：{todo['location']}")
        lines.append(f"期限：{date_range}")
        if todo.get("engineer"):
            lines.append(f"指派人：{todo['engineer']}")

        _send_discord_async("\n".join(lines))
    except Exception:
        traceback.print_exc()  # 組訊息出錯也不能影響已經存好的資料


def _compute_busy_workers_dispatch(all_dispatches, start_date, end_date, exclude_id=None, completed_todo_ids=None):
    """
    算出在 [start_date, end_date] 這段期間，已經被排進「其他」派工紀錄的移工。
    completed_todo_ids：所屬事項已標記完成的 todo_id 集合，這些派工紀錄不算佔用
    （事項做完了，人就該空出來）。
    回傳 {移工姓名: 佔用的那筆派工紀錄 dict}（取第一筆佔用的，方便顯示原因）。
    """
    completed_todo_ids = completed_todo_ids or set()
    busy = {}
    for d in all_dispatches:
        if exclude_id and d["id"] == exclude_id:
            continue
        if d.get("todo_id") in completed_todo_ids:
            continue
        try:
            ds = date.fromisoformat(d["start_date"])
            de = date.fromisoformat(d["end_date"])
        except (ValueError, TypeError):
            continue
        if ds <= end_date and de >= start_date:  # 期間重疊
            for w in data_store.split_workers(d.get("workers", "")):
                busy.setdefault(w, d)
    return busy


def _notify_discord_dispatch(todo, dispatch):
    """新增派工紀錄後，在背景發一則 Discord 通知。"""
    try:
        rng = dispatch["start_date"] if dispatch["start_date"] == dispatch["end_date"] else f"{dispatch['start_date']}~{dispatch['end_date']}"
        lines = [
            "👷 **新增派工**",
            f"{todo['task']}",
            f"工項：{todo['work_item']}／類型：{todo['type']}",
        ]
        if todo.get("location"):
            lines.append(f"地點：{todo['location']}")
        if todo.get("engineer"):
            lines.append(f"指派人：{todo['engineer']}")
        lines.append(f"派工日期：{rng}")
        lines.append(f"移工：{dispatch['workers']}")
        _send_discord_async("\n".join(lines))
    except Exception:
        traceback.print_exc()


def render_todo_tab():
    if st.toggle("⚙️ 管理選單（工項／類型／工程師／移工）", key="show_option_manager"):
        render_option_manager()

    st.subheader("新增 / 編輯代辦事項")

    editing = None
    if st.session_state.editing_id:
        todos = data_store.get_todos()
        editing = next((t for t in todos if t["id"] == st.session_state.editing_id), None)
        if editing is None:  # 正在編輯的事項已經被別人刪除或改掉
            st.session_state.editing_id = None

    with st.form("todo_form", clear_on_submit=False):
        col1, col2 = st.columns(2)
        with col1:
            start_date = st.date_input(
                "開始日期",
                value=date.fromisoformat(editing["start_date"]) if editing else date.today(),
            )
        with col2:
            end_date = st.date_input(
                "結束日期（緊急程度會依這個日期自動判定）",
                value=date.fromisoformat(editing["end_date"]) if editing else date.today(),
            )

        task = st.text_input("事項說明", value=editing["task"] if editing else "")
        location = st.text_input(
            "地點（例如：B1停車場、3樓東側）", value=editing["location"] if editing else ""
        )

        work_item = _select_with_add("工項", "work_item", "work_item_select")
        type_ = _select_with_add("類型", "type", "type_select")
        engineer = _select_with_add("指派人（工程師）", "engineer", "engineer_select")

        st.caption("💡 移工的每日派工，請到「👷 移工指派」分頁另外安排（同一個工項橫跨多天時，每天可以換不同的人）。")

        if editing and editing.get("image_url"):
            st.image(editing["image_url"], caption="目前的圖說", width=250)
            st.caption("如果不重新上傳，會保留這張圖")

        image_file = st.file_uploader(
            "匯入圖說（選填，上傳新圖片會取代舊的）", type=["png", "jpg", "jpeg"]
        )
        if image_file:
            st.image(image_file, caption="新圖片預覽", width=250)

        col_a, col_b = st.columns(2)
        submit_label = "更新事項" if editing else "新增事項"
        submitted = col_a.form_submit_button(submit_label, width="stretch")
        cancel = False
        if editing:
            cancel = col_b.form_submit_button("取消編輯", width="stretch")

    if cancel:
        st.session_state.editing_id = None
        st.rerun()

    if submitted:
        if not task.strip():
            st.error("請輸入事項說明")
        elif end_date < start_date:
            st.error("結束日期不能早於開始日期")
        elif not work_item or not type_:
            st.error("請完整選擇工項與類型")
        elif _is_duplicate_submit((
            "todo", st.session_state.editing_id, task.strip(), location,
            str(start_date), str(end_date), work_item, type_, engineer,
        )):
            st.info("偵測到重複送出（10 秒內內容完全相同），已略過，沒有重複儲存。")
        else:
            action = "更新" if editing else "新增"
            with st.spinner("儲存中，請稍候…"):
                image_url = None
                if image_file is not None:
                    image_url = data_store.upload_image(
                        image_file.getvalue(), image_file.name, image_file.type
                    )

                if editing:
                    fields = dict(
                        start_date=str(start_date), end_date=str(end_date),
                        task=task, location=location,
                        work_item=work_item, type=type_,
                        engineer=engineer,
                        last_reminder_at="",
                    )
                    if image_url is not None:
                        fields["image_url"] = image_url
                    data_store.update_todo(editing["id"], **fields)
                    saved_todo = {**editing, **fields}
                else:
                    saved_todo = data_store.add_todo(
                        str(start_date), str(end_date), task, location,
                        work_item, type_, image_url=image_url or "",
                        engineer=engineer,
                    )

            st.session_state.editing_id = None
            _flash(f"✅ 已{action}事項「{task.strip()}」")
            _notify_discord_instant(saved_todo, action)
            st.rerun()

    st.divider()
    st.subheader("代辦事項清單")

    todos = data_store.get_todos()
    active_todos = [t for t in todos if str(t.get("completed", "")).strip().upper() != "TRUE"]

    if not active_todos:
        st.info("目前沒有進行中的代辦事項。已完成的事項可以到「📜 歷史記錄」分頁查詢。")
        return

    today = datetime_now_taipei_date()
    dispatches_by_todo = data_store.get_dispatches_by_todo()  # 一次分好組，不要每筆事項各查一次

    # 依緊急程度排序：非常緊急 > 緊急 > 一般 > 尚未進入提醒範圍
    def _sort_key(t):
        tier = urgency.classify_tier(t["end_date"], today)
        if tier in urgency.TIER_ORDER:
            return urgency.TIER_ORDER.index(tier)
        return len(urgency.TIER_ORDER)

    todos_sorted = sorted(active_todos, key=_sort_key)

    for t in todos_sorted:
        label = urgency.tier_label(t["end_date"], today)
        date_range = t["start_date"] if t["start_date"] == t["end_date"] else f"{t['start_date']} ~ {t['end_date']}"
        title = f"{label} {t['task']}"

        with st.expander(title):
            st.write(f"**期限：** {date_range}")
            st.write(f"**工項／類型：** {t['work_item']} ／ {t['type']}")
            if t.get("location"):
                st.write(f"**地點：** {t['location']}")
            if t.get("engineer"):
                st.write(f"**指派人：** {t['engineer']}")
            st.write(f"**目前分級：** {label}")

            dispatches = sorted(dispatches_by_todo.get(t["id"], []), key=lambda d: d["start_date"])
            if dispatches:
                st.write("**派工紀錄：**")
                for d in dispatches:
                    rng = d["start_date"] if d["start_date"] == d["end_date"] else f"{d['start_date']}~{d['end_date']}"
                    st.caption(f"　{rng}：{d['workers']}")
            else:
                st.caption("尚未安排移工，請到「👷 移工指派」分頁指派。")

            if t.get("image_url"):
                st.image(t["image_url"], caption="圖說", width=300)

            c1, c2 = st.columns(2)
            if c1.button("標記完成", key=f"done_{t['id']}"):
                with st.spinner("更新中…"):
                    data_store.mark_completed(t["id"], True)
                _flash(f"✅ 已將「{t['task']}」標記完成（可到「📜 歷史記錄」查詢）")
                st.rerun()
            if c2.button("編輯", key=f"edit_{t['id']}"):
                st.session_state.editing_id = t["id"]
                st.rerun()


def render_dispatch_assignment_tab():
    st.subheader("👷 移工指派")

    all_todos = data_store.get_todos()
    active_todos = [t for t in all_todos if str(t.get("completed", "")).strip().upper() != "TRUE"]

    if not active_todos:
        st.info("目前沒有可指派的代辦事項，請先到「📋 代辦事項」分頁新增。")
        return

    today = datetime_now_taipei_date()
    active_todos.sort(key=lambda t: t["start_date"])

    def _fmt(t):
        rng = t["start_date"] if t["start_date"] == t["end_date"] else f"{t['start_date']}~{t['end_date']}"
        loc = f"／{t['location']}" if t.get("location") else ""
        return f"{t['task']}（{rng}{loc}）"

    # 選項用「事項 id」而不是「第幾筆」：多人同時使用時，別人新增或完成了事項，
    # 清單的順序會變，用順序當選項的話你選的事項會悄悄變成別的事項。
    ids = [t["id"] for t in active_todos]
    labels_by_id = {t["id"]: _fmt(t) for t in active_todos}
    remembered = st.session_state.get("_dispatch_todo_id")
    picked_id = st.selectbox(
        "選擇代辦事項", ids,
        index=ids.index(remembered) if remembered in ids else 0,
        format_func=lambda i: labels_by_id[i],
        key="dispatch_todo_select",
    )
    st.session_state["_dispatch_todo_id"] = picked_id  # 切到別頁再回來時，還記得選到哪一筆
    todo = next(t for t in active_todos if t["id"] == picked_id)

    todo_start = date.fromisoformat(todo["start_date"])
    todo_end = date.fromisoformat(todo["end_date"])
    st.caption(f"這筆事項整體區間：{todo_start} ～ {todo_end}")

    use_range = st.checkbox("指定一段區間（不勾選的話，預設只有選的那一天生效）", key="dispatch_use_range")

    # 日期欄位的 key 帶上事項 id：每筆事項的可選範圍不同，換事項時要重新初始化，
    # 否則上一筆事項選的日期可能超出新事項的範圍而出錯。
    default_single = min(max(today, todo_start), todo_end)
    if use_range:
        c1, c2 = st.columns(2)
        d_start = c1.date_input("派工開始日期", value=todo_start, min_value=todo_start, max_value=todo_end, key=f"dispatch_range_start_{picked_id}")
        d_end = c2.date_input("派工結束日期", value=todo_end, min_value=todo_start, max_value=todo_end, key=f"dispatch_range_end_{picked_id}")
    else:
        d_start = st.date_input("派工日期（只有這一天生效）", value=default_single, min_value=todo_start, max_value=todo_end, key=f"dispatch_single_date_{picked_id}")
        d_end = d_start

    completed_ids = {t["id"] for t in all_todos if str(t.get("completed", "")).strip().upper() == "TRUE"}
    all_dispatches = data_store.get_dispatches()
    todos_by_id = {t["id"]: t for t in all_todos}
    busy = _compute_busy_workers_dispatch(all_dispatches, d_start, d_end, completed_todo_ids=completed_ids)

    worker_options = data_store.get_options("worker")
    if not worker_options:
        st.warning("目前沒有移工選項，請先到「📋 代辦事項」分頁打開「⚙️ 管理選單」新增。")
        return

    with st.form("dispatch_form", clear_on_submit=True):
        st.markdown("**選擇移工（可複選；灰色代表這段期間已被其他派工佔用）**")
        selected = []
        for w in worker_options:
            is_busy = w in busy
            label = w
            if is_busy:
                occ = busy[w]
                occ_todo = todos_by_id.get(occ["todo_id"])
                occ_task = occ_todo["task"] if occ_todo else "（事項已刪除）"
                occ_rng = occ["start_date"] if occ["start_date"] == occ["end_date"] else f"{occ['start_date']}~{occ['end_date']}"
                label = f"{w}　⚠️ 已指派於「{occ_task}」（{occ_rng}）"
            checked = st.checkbox(label, value=False, disabled=is_busy, key=f"dispatch_worker_{w}")
            if checked:
                selected.append(w)

        submitted = st.form_submit_button("指派")

    if submitted:
        if d_end < d_start:
            st.error("結束日期不能早於開始日期")
        elif not selected:
            st.error("請至少選一位移工")
        elif _is_duplicate_submit(("dispatch", picked_id, str(d_start), str(d_end), tuple(selected))):
            st.info("偵測到重複送出（10 秒內內容完全相同），已略過，沒有重複指派。")
        else:
            with st.spinner("指派中，請稍候…"):
                record = data_store.add_dispatch(todo["id"], str(d_start), str(d_end), data_store.join_workers(selected))
            _flash(f"✅ 已指派：{'、'.join(selected)}")
            _notify_discord_dispatch(todo, record)
            st.rerun()

    st.divider()
    st.subheader(f"「{todo['task']}」目前的派工紀錄")

    dispatches_for_todo = sorted(
        [d for d in all_dispatches if d["todo_id"] == picked_id], key=lambda d: d["start_date"]
    )
    if not dispatches_for_todo:
        st.info("這筆事項目前還沒有任何派工紀錄。")
        return

    for d in dispatches_for_todo:
        rng = d["start_date"] if d["start_date"] == d["end_date"] else f"{d['start_date']}~{d['end_date']}"
        c1, c2 = st.columns([5, 1])
        c1.write(f"**{rng}**：{d['workers']}")
        if c2.button("刪除", key=f"del_dispatch_{d['id']}"):
            with st.spinner("刪除中…"):
                data_store.delete_dispatch(d["id"])
            _flash(f"已刪除 {rng} 的派工紀錄")
            st.rerun()


def render_dispatch_tab():
    st.subheader("📅 每日派工總表")

    pick_date = st.date_input("選擇日期", value=datetime_now_taipei_date(), key="dispatch_board_date")

    todos = data_store.get_todos()
    todos_by_id = {t["id"]: t for t in todos}
    dispatches = data_store.get_dispatches()

    def _d_in_range(d):
        try:
            s = date.fromisoformat(d["start_date"])
            e = date.fromisoformat(d["end_date"])
        except (ValueError, TypeError):
            return False
        return s <= pick_date <= e

    matched_dispatches = [d for d in dispatches if _d_in_range(d)]

    # 同一個代辦事項當天可能有好幾筆派工紀錄（分好幾次指派），合併成一筆，
    # 移工名單取聯集（去重），這樣同一事項在總表上只會出現一張卡片。
    by_todo = {}
    for d in matched_dispatches:
        t = todos_by_id.get(d["todo_id"])
        if not t:
            continue  # 事項已被刪除，這筆派工紀錄是孤兒資料，略過不顯示
        entry = by_todo.setdefault(t["id"], {"todo": t, "workers": set()})
        entry["workers"].update(data_store.split_workers(d.get("workers", "")))

    today = datetime_now_taipei_date()

    def _sort_key(entry):
        tier = urgency.classify_tier(entry["todo"]["end_date"], today)
        return urgency.TIER_ORDER.index(tier) if tier in urgency.TIER_ORDER else len(urgency.TIER_ORDER)

    merged_entries = sorted(by_todo.values(), key=_sort_key)

    total_workers = set()
    for entry in merged_entries:
        total_workers.update(entry["workers"])

    c1, c2 = st.columns(2)
    c1.metric("當天派工項目數", len(merged_entries))
    c2.metric("涉及移工人數", len(total_workers))

    if not merged_entries:
        st.info(f"{pick_date} 目前沒有任何派工紀錄。")
    else:
        for entry in merged_entries:
            t = entry["todo"]
            completed = str(t.get("completed", "")).strip().upper() == "TRUE"
            tier = urgency.classify_tier(t["end_date"], today)
            icon = urgency.TIER_ICONS.get(tier, "⚪") if tier else "⚪"
            workers_text = "、".join(sorted(entry["workers"])) if entry["workers"] else "－"

            with st.container(border=True):
                st.markdown(f"{icon} **{t['task']}**　{'（已完成）' if completed else ''}")
                st.write(f"工項／類型：{t['work_item']}／{t['type']}　　地點：{t.get('location') or '－'}")
                if t.get("engineer"):
                    st.write(f"指派人：{t['engineer']}")
                st.write(f"移工：{workers_text}")

    dispatched_todo_ids = set(by_todo.keys())

    # 當天區間內、但還沒有派工紀錄覆蓋到的未完成事項，提醒要盡快安排人力
    def _t_in_range(t):
        try:
            s = date.fromisoformat(t["start_date"])
            e = date.fromisoformat(t["end_date"])
        except (ValueError, TypeError):
            return False
        return s <= pick_date <= e

    unassigned = [
        t for t in todos
        if t["id"] not in dispatched_todo_ids
        and str(t.get("completed", "")).strip().upper() != "TRUE"
        and _t_in_range(t)
    ]
    if unassigned:
        st.warning(f"⚠️ 以下 {len(unassigned)} 筆事項在 {pick_date} 這天還沒有安排移工：")
        for t in unassigned:
            st.write(f"・{t['task']}（{t['work_item']}／{t.get('location') or '無地點'}）")


def render_history_tab():
    st.subheader("📜 歷史記錄查詢")
    st.caption("這裡可以查到所有代辦事項，包含已完成、已不在主清單顯示的項目。")

    todos = data_store.get_todos()
    if not todos:
        st.info("目前沒有任何代辦事項紀錄。")
        return

    c1, c2, c3 = st.columns(3)
    keyword = c1.text_input("關鍵字搜尋（事項說明）", key="history_keyword")
    status_filter = c2.selectbox("完成狀態", ["全部", "已完成", "進行中"], key="history_status")
    work_item_options = ["全部"] + data_store.get_options("work_item")
    work_item_filter = c3.selectbox("工項", work_item_options, key="history_work_item")

    c4, c5 = st.columns(2)
    date_from = c4.date_input("期限（結束日期）從", value=None, key="history_date_from")
    date_to = c5.date_input("期限（結束日期）到", value=None, key="history_date_to")

    filtered = todos
    if keyword.strip():
        filtered = [t for t in filtered if keyword.strip() in t.get("task", "")]
    if status_filter == "已完成":
        filtered = [t for t in filtered if str(t.get("completed", "")).strip().upper() == "TRUE"]
    elif status_filter == "進行中":
        filtered = [t for t in filtered if str(t.get("completed", "")).strip().upper() != "TRUE"]
    if work_item_filter != "全部":
        filtered = [t for t in filtered if t.get("work_item") == work_item_filter]
    if date_from:
        filtered = [t for t in filtered if _safe_date(t.get("end_date")) and _safe_date(t["end_date"]) >= date_from]
    if date_to:
        filtered = [t for t in filtered if _safe_date(t.get("end_date")) and _safe_date(t["end_date"]) <= date_to]

    filtered = sorted(filtered, key=lambda t: t.get("end_date", ""), reverse=True)

    st.caption(f"共找到 {len(filtered)} 筆")
    if not filtered:
        st.info("沒有符合條件的紀錄。")
        return

    dispatches_by_todo = data_store.get_dispatches_by_todo()  # 一次分好組

    rows = []
    for t in filtered:
        completed = str(t.get("completed", "")).strip().upper() == "TRUE"
        date_range = t["start_date"] if t["start_date"] == t["end_date"] else f"{t['start_date']}~{t['end_date']}"
        dispatches = dispatches_by_todo.get(t["id"], [])
        workers_set = {w for d in dispatches for w in data_store.split_workers(d.get("workers", ""))}
        rows.append({
            "狀態": "✅已完成" if completed else "🔸進行中",
            "事項說明": t["task"],
            "工項": t["work_item"],
            "類型": t["type"],
            "地點": t.get("location") or "－",
            "指派人": t.get("engineer") or "－",
            "期限": date_range,
            "曾派工移工": "、".join(sorted(workers_set)) if workers_set else "－",
        })

    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    with st.expander("✏️ 修正完成狀態（例如不小心標錯）"):
        ids = [t["id"] for t in filtered]
        by_id = {t["id"]: t for t in filtered}

        def _label(i):
            t = by_id[i]
            state = "已完成" if str(t.get("completed", "")).strip().upper() == "TRUE" else "進行中"
            return f"{t['task']}（期限 {t['end_date']}，{state}）"

        target_id = st.selectbox("選擇事項", ids, format_func=_label, key="history_toggle_select")
        target = by_id[target_id]
        target_completed = str(target.get("completed", "")).strip().upper() == "TRUE"
        btn_label = "取消完成（改回進行中）" if target_completed else "標記為已完成"
        if st.button(btn_label, key="history_toggle_btn"):
            with st.spinner("更新中…"):
                data_store.mark_completed(target["id"], not target_completed)
            _flash(f"已將「{target['task']}」{'改回進行中' if target_completed else '標記為已完成'}")
            st.rerun()


def _safe_date(value):
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def render_experience_tab():
    st.subheader("新增經驗")
    with st.form("exp_form", clear_on_submit=True):
        work_item = st.text_input("工項名稱（例如：連續壁、泥作）")
        details = st.text_area("須注意細節")
        mistakes = st.text_area("錯誤經驗")
        submitted = st.form_submit_button("發布經驗")

    if submitted:
        if not work_item.strip() or not details.strip() or not mistakes.strip():
            st.error("請完整填寫工項名稱、須注意細節與錯誤經驗")
        elif _is_duplicate_submit(("exp", work_item.strip(), details.strip(), mistakes.strip())):
            st.info("偵測到重複送出（10 秒內內容完全相同），已略過，沒有重複發布。")
        else:
            with st.spinner("發布中，請稍候…"):
                data_store.add_experience(work_item, details, mistakes)
            _flash("✅ 已發布經驗")
            st.rerun()

    st.divider()
    search = st.text_input("🔍 搜尋工項", key="exp_search")

    experiences = data_store.get_experiences()
    if search:
        experiences = [e for e in experiences if search.lower() in e["work_item"].lower()]

    if not experiences:
        st.info("沒有符合的經驗分享。")
        return

    for e in experiences:
        with st.container(border=True):
            st.markdown(f"#### {e['work_item']} （{e['source']}）")
            st.write(f"**須注意細節：** {e['details']}")
            st.write(f"**錯誤經驗：** {e['mistakes']}")
            if st.button("刪除", key=f"expdel_{e['id']}"):
                with st.spinner("刪除中…"):
                    data_store.delete_experience(e["id"])
                _flash("已刪除該則經驗")
                st.rerun()


def datetime_now_taipei_date():
    import datetime as _dt
    return _dt.datetime.now(TZ).date()


def _show_friendly_error(err):
    """資料讀寫失敗時顯示的友善提示（取代整頁紅色的 traceback）。"""
    st.error(
        "⚠️ 連線雲端資料時發生問題。最常見的原因是多人同時使用、Google 試算表暫時忙碌"
        "（每分鐘有讀取次數上限），通常等 10～20 秒再重試就會恢復。"
    )
    # 用 on_click 回呼：它會在下一次重跑「開始時」就執行。若改成 `if st.button(...)`，
    # 按下去後頁面會先重跑一次，雲端一旦恢復，這顆按鈕就不會再出現，清快取的程式碼也就沒機會執行。
    st.button("🔁 重試", key="retry_after_error", on_click=data_store.clear_all_caches)
    with st.expander("技術細節（回報問題時可以提供）"):
        st.code(f"{type(err).__name__}: {str(err)[:300]}")


def main():
    _init_state()

    head_l, head_r = st.columns([6, 1])
    head_l.title("🏗️ 營造管理系統")
    if head_r.button("🔄 重新整理", help="從雲端重新讀取最新資料。如果你剛剛直接在 Google 試算表裡改了資料，按這個才會立刻看到。"):
        data_store.clear_all_caches()
        st.rerun()

    # 用「頁面切換」取代 st.tabs：st.tabs 每次互動都會把 5 個分頁的程式全部執行一遍，
    # 這裡只會執行你目前所在的那一頁，速度差很多。
    pages = {
        "📋 代辦事項": render_todo_tab,
        "👷 移工指派": render_dispatch_assignment_tab,
        "📅 派工總表": render_dispatch_tab,
        "📜 歷史記錄": render_history_tab,
        "📚 經驗分享區": render_experience_tab,
    }
    page = st.radio("頁面", list(pages), horizontal=True, label_visibility="collapsed", key="current_page")

    _show_flash()

    try:
        pages[page]()
    except Exception as e:  # st.rerun() 等控制流程的例外不是 Exception 的子類別，不會被這裡攔截
        st.session_state.pop("_last_submit", None)  # 這次儲存失敗了，讓使用者可以馬上重試
        traceback.print_exc()  # 完整錯誤仍會寫進 Streamlit Cloud 的 Logs
        _show_friendly_error(e)


if __name__ == "__main__":
    main()
