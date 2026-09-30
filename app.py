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
        if todo.get("workers"):
            lines.append(f"移工：{todo['workers']}")

        discord_notify.send_discord_message("\n".join(lines))
    except Exception as e:
        st.warning(f"代辦事項已儲存，但即時通知發送失敗（{e}），不影響資料，排程提醒仍會照常運作。")


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

        worker_options = data_store.get_options("worker")
        default_workers = data_store.split_workers(editing["workers"]) if editing else []
        default_workers = [w for w in default_workers if w in worker_options]
        workers_selected = st.multiselect(
            "移工名單（可複選）", worker_options, default=default_workers, key="workers_select"
        )
        if not worker_options:
            st.warning("目前沒有移工選項，請先到上方「⚙️ 管理選單」新增。")

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

            workers_str = data_store.join_workers(workers_selected)
            action = "更新" if editing else "新增"

            if editing:
                fields = dict(
                    start_date=str(start_date), end_date=str(end_date),
                    task=task, location=location,
                    work_item=work_item, type=type_,
                    engineer=engineer, workers=workers_str,
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
                    engineer=engineer, workers=workers_str,
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
            if t.get("workers"):
                st.write(f"**移工：** {t['workers']}")
            st.write(f"**目前分級：** {label}")
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

    tab1, tab2 = st.tabs(["📋 代辦事項", "📚 經驗分享區"])
    with tab1:
        render_todo_tab()
    with tab2:
        render_experience_tab()


if __name__ == "__main__":
    main()
