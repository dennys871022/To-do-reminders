"""
app.py
-------
營造管理系統 - Streamlit 版本

【這版改版重點】
拿掉「緊急程度」手動下拉選單。緊急程度現在完全由 urgency.py 依「結束日期」
自動判定（3 天內=非常緊急／本週內=緊急／本月內=一般），清單畫面上會即時顯示
目前的分級標籤，不需要使用者自己選、也不會選錯。
"""

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


def _select_with_add(label, category, key):
    """純下拉選單，選項存在 Google Sheets 的 Options 分頁。
    要新增新選項，請到上方「⚙️ 管理工項／類型選單」新增（不能在這個表單裡直接打新選項，
    因為 Streamlit 表單裡的元件不會即時刷新，體驗上會像打不進去）。"""
    options = data_store.get_options(category)
    if not options:
        st.warning(f"目前沒有{label}選項，請先到上方「⚙️ 管理工項／類型選單」新增。")
        return ""
    return st.selectbox(label, options, key=key)


def render_option_manager():
    """管理工項／類型／工程師／移工選項：新增、刪除都在這裡做（表單外，才能即時刷新）。"""
    with st.expander("⚙️ 管理選單（工項／類型／工程師／移工）", expanded=False):
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
            data_store.delete_option(category, v)
            st.rerun()
    with st.form(f"add_{category}_form", clear_on_submit=True):
        c1, c2 = st.columns([4, 1])
        new_val = c1.text_input(f"新增{label}", key=f"new_{category}_input", label_visibility="collapsed", placeholder=f"新增{label}")
        if c2.form_submit_button("新增"):
            if new_val.strip():
                data_store.add_option(category, new_val.strip())
                st.rerun()
    st.divider()


def _notify_discord_instant(todo, action):
    """新增或編輯代辦事項後，立即發一則 Discord 通知（跟排程的每日彙整是獨立的兩條路）。
    推播失敗只顯示小提醒，不會影響代辦事項本身已經存檔成功這件事。"""
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

        discord_notify.send_discord_message("\n".join(lines))
    except Exception as e:
        st.warning(f"代辦事項已儲存，但即時通知發送失敗（{e}），不影響資料，排程提醒仍會照常運作。")


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
    """新增派工紀錄後，立即發一則 Discord 通知。"""
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
        discord_notify.send_discord_message("\n".join(lines))
    except Exception as e:
        st.warning(f"派工已儲存，但即時通知發送失敗（{e}），不影響資料。")


def render_todo_tab():
    render_option_manager()

    st.subheader("新增 / 編輯代辦事項")

    editing = None
    if st.session_state.editing_id:
        todos = data_store.get_todos()
        editing = next((t for t in todos if t["id"] == st.session_state.editing_id), None)

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
        submitted = col_a.form_submit_button(submit_label, use_container_width=True)
        cancel = False
        if editing:
            cancel = col_b.form_submit_button("取消編輯", use_container_width=True)

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
        else:
            image_url = None
            if image_file is not None:
                with st.spinner("圖片上傳中..."):
                    image_url = data_store.upload_image(
                        image_file.getvalue(), image_file.name, image_file.type
                    )

            action = "更新" if editing else "新增"

            if editing:
                fields = dict(
                    start_date=str(start_date), end_date=str(end_date),
                    task=task, location=location,
                    work_item=work_item, type=type_,
                    engineer=engineer,
                    last_reminder_at="",  # 內容更新後重新起算提醒週期
                )
                if image_url is not None:
                    fields["image_url"] = image_url
                data_store.update_todo(editing["id"], **fields)
                st.session_state.editing_id = None
                st.success("已更新事項")
                saved_todo = {**editing, **fields}
            else:
                saved_todo = data_store.add_todo(
                    str(start_date), str(end_date), task, location,
                    work_item, type_, image_url=image_url or "",
                    engineer=engineer,
                )
                st.success("已新增事項")

            _notify_discord_instant(saved_todo, action)
            st.rerun()

    st.divider()
    st.subheader("代辦事項清單")

    todos = data_store.get_todos()
    if not todos:
        st.info("目前沒有代辦事項。")
        return

    today = datetime_now_taipei_date()

    # 依緊急程度排序：非常緊急 > 緊急 > 一般 > 尚未進入提醒範圍
    def _sort_key(t):
        tier = urgency.classify_tier(t["end_date"], today)
        if tier in urgency.TIER_ORDER:
            return urgency.TIER_ORDER.index(tier)
        return len(urgency.TIER_ORDER)

    todos_sorted = sorted(todos, key=_sort_key)

    for t in todos_sorted:
        completed = str(t.get("completed", "")).strip().upper() == "TRUE"
        label = urgency.tier_label(t["end_date"], today)
        date_range = t["start_date"] if t["start_date"] == t["end_date"] else f"{t['start_date']} ~ {t['end_date']}"
        title = f"{label} {'~~' if completed else ''}{t['task']}{'~~' if completed else ''}"

        with st.expander(title):
            st.write(f"**期限：** {date_range}")
            st.write(f"**工項／類型：** {t['work_item']} ／ {t['type']}")
            if t.get("location"):
                st.write(f"**地點：** {t['location']}")
            if t.get("engineer"):
                st.write(f"**指派人：** {t['engineer']}")
            st.write(f"**目前分級：** {label}")

            dispatches = sorted(data_store.get_dispatches_for_todo(t["id"]), key=lambda d: d["start_date"])
            if dispatches:
                st.write("**派工紀錄：**")
                for d in dispatches:
                    rng = d["start_date"] if d["start_date"] == d["end_date"] else f"{d['start_date']}~{d['end_date']}"
                    st.caption(f"　{rng}：{d['workers']}")
            else:
                st.caption("尚未安排移工，請到「👷 移工指派」分頁指派。")

            if t.get("image_url"):
                st.image(t["image_url"], caption="圖說", width=300)

            c1, c2, c3 = st.columns(3)
            if c1.button("標記完成" if not completed else "取消完成", key=f"done_{t['id']}"):
                data_store.mark_completed(t["id"], not completed)
                st.rerun()
            if c2.button("編輯", key=f"edit_{t['id']}"):
                st.session_state.editing_id = t["id"]
                st.rerun()
            if c3.button("刪除", key=f"del_{t['id']}"):
                data_store.delete_todo(t["id"])
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

    labels = [_fmt(t) for t in active_todos]
    picked_idx = st.selectbox(
        "選擇代辦事項", range(len(active_todos)), format_func=lambda i: labels[i], key="dispatch_todo_select"
    )
    todo = active_todos[picked_idx]

    todo_start = date.fromisoformat(todo["start_date"])
    todo_end = date.fromisoformat(todo["end_date"])
    st.caption(f"這筆事項整體區間：{todo_start} ～ {todo_end}")

    use_range = st.checkbox("指定一段區間（不勾選的話，預設只有選的那一天生效）", key="dispatch_use_range")

    default_single = min(max(today, todo_start), todo_end)
    if use_range:
        c1, c2 = st.columns(2)
        d_start = c1.date_input("派工開始日期", value=todo_start, min_value=todo_start, max_value=todo_end, key="dispatch_range_start")
        d_end = c2.date_input("派工結束日期", value=todo_end, min_value=todo_start, max_value=todo_end, key="dispatch_range_end")
    else:
        d_start = st.date_input("派工日期（只有這一天生效）", value=default_single, min_value=todo_start, max_value=todo_end, key="dispatch_single_date")
        d_end = d_start

    completed_ids = {t["id"] for t in all_todos if str(t.get("completed", "")).strip().upper() == "TRUE"}
    all_dispatches = data_store.get_dispatches()
    todos_by_id = {t["id"]: t for t in all_todos}
    busy = _compute_busy_workers_dispatch(all_dispatches, d_start, d_end, completed_todo_ids=completed_ids)

    worker_options = data_store.get_options("worker")
    if not worker_options:
        st.warning("目前沒有移工選項，請先到「📋 代辦事項」分頁的「⚙️ 管理選單」新增。")
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
        else:
            record = data_store.add_dispatch(todo["id"], str(d_start), str(d_end), data_store.join_workers(selected))
            st.success("已指派")
            _notify_discord_dispatch(todo, record)
            st.rerun()

    st.divider()
    st.subheader(f"「{todo['task']}」目前的派工紀錄")

    dispatches_for_todo = sorted(data_store.get_dispatches_for_todo(todo["id"]), key=lambda d: d["start_date"])
    if not dispatches_for_todo:
        st.info("這筆事項目前還沒有任何派工紀錄。")
        return

    for d in dispatches_for_todo:
        rng = d["start_date"] if d["start_date"] == d["end_date"] else f"{d['start_date']}~{d['end_date']}"
        c1, c2 = st.columns([5, 1])
        c1.write(f"**{rng}**：{d['workers']}")
        if c2.button("刪除", key=f"del_dispatch_{d['id']}"):
            data_store.delete_dispatch(d["id"])
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

    today = datetime_now_taipei_date()

    def _sort_key(d):
        t = todos_by_id.get(d["todo_id"])
        if not t:
            return len(urgency.TIER_ORDER)
        tier = urgency.classify_tier(t["end_date"], today)
        return urgency.TIER_ORDER.index(tier) if tier in urgency.TIER_ORDER else len(urgency.TIER_ORDER)

    matched_dispatches.sort(key=_sort_key)

    rows = []
    total_workers = set()
    dispatched_todo_ids = set()
    for d in matched_dispatches:
        t = todos_by_id.get(d["todo_id"])
        if not t:
            continue  # 事項已被刪除，這筆派工紀錄是孤兒資料，略過不顯示
        dispatched_todo_ids.add(t["id"])
        completed = str(t.get("completed", "")).strip().upper() == "TRUE"
        tier = urgency.classify_tier(t["end_date"], today)
        workers_list = data_store.split_workers(d.get("workers", ""))
        total_workers.update(workers_list)
        rows.append({
            "分級": urgency.TIER_ICONS.get(tier, "⚪") if tier else "⚪",
            "指派人": t.get("engineer") or "－",
            "事項說明": t["task"],
            "工項": t["work_item"],
            "類型": t["type"],
            "地點": t.get("location") or "－",
            "移工名單": d.get("workers") or "－",
            "狀態": "已完成" if completed else "進行中",
        })

    c1, c2 = st.columns(2)
    c1.metric("當天派工項目數", len(rows))
    c2.metric("涉及移工人數", len(total_workers))

    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info(f"{pick_date} 目前沒有任何派工紀錄。")

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
        else:
            data_store.add_experience(work_item, details, mistakes)
            st.success("已發布經驗")
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
                data_store.delete_experience(e["id"])
                st.rerun()


def datetime_now_taipei_date():
    import datetime as _dt
    return _dt.datetime.now(TZ).date()


def main():
    _init_state()
    st.title("🏗️ 營造管理系統")

    tab1, tab2, tab3, tab4 = st.tabs(["📋 代辦事項", "👷 移工指派", "📅 派工總表", "📚 經驗分享區"])
    with tab1:
        render_todo_tab()
    with tab2:
        render_dispatch_assignment_tab()
    with tab3:
        render_dispatch_tab()
    with tab4:
        render_experience_tab()


if __name__ == "__main__":
    main()
