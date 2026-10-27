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
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import data_store
import urgency
import discord_notify

st.set_page_config(page_title="營造管理系統", page_icon="🏗️", layout="wide")

TZ = ZoneInfo("Asia/Taipei")


def _init_state():
    if "editing_id" not in st.session_state:
        st.session_state.editing_id = None
    if "operator" not in st.session_state:
        # 操作人姓名記在網址參數 ?user=xxx 裡：加書籤／下次從同一個網址進來就不用再選
        st.session_state.operator = str(st.query_params.get("user", "")).strip()


def _audit(action, target="", detail=""):
    """寫一筆操作紀錄（誰、做了什麼）。失敗不影響主要操作。"""
    data_store.log_action(st.session_state.get("operator", ""), action, target, detail)


def _render_operator_gate():
    """還沒選姓名就先請他選（不是登入密碼，只是用來記錄「誰操作的」）。回傳是否可以繼續。"""
    if st.session_state.operator:
        return True
    st.subheader("👤 請先選擇你的姓名")
    st.caption("系統會記錄每次新增、修改是誰操作的（這不是登入密碼）。選好後會記在網址裡，把網址加入書籤，下次就不用再選。")
    names = data_store.get_options("engineer")
    other_label = "➕ 其他（請在下方輸入）"
    with st.form("operator_form"):
        picked = st.selectbox("姓名", names + [other_label])
        other = st.text_input("其他姓名（上面選「其他」時才需要填）")
        go = st.form_submit_button("開始使用")
    if go:
        name = other.strip() if picked == other_label else picked
        if not name:
            st.error("請輸入姓名")
        else:
            st.session_state.operator = name
            st.query_params["user"] = name
            st.rerun()
    return False


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


def _send_discord_async(text, images=None):
    """在背景發送 Discord 訊息，不讓使用者等它；失敗時記錄起來，下次畫面更新時提醒。"""
    errors = st.session_state.setdefault("_notify_errors", [])
    op = st.session_state.get("operator")
    if op:
        text = f"{text}\n👤 操作人：{op}"
    try:
        webhook_url = discord_notify._get_webhook_url()  # 在主執行緒先取好，背景執行緒不碰 st
    except Exception as e:
        errors.append(str(e))
        return

    def _worker():
        try:
            discord_notify.send_discord_message(text, webhook_url=webhook_url, images=images)
        except Exception as e:  # 通知失敗不影響資料
            errors.append(str(e))

    threading.Thread(target=_worker, daemon=True).start()


def _select_with_add(label, category, key, current=None):
    """純下拉選單，選項存在 Google Sheets 的 Options 分頁。
    要新增新選項，請到上方「⚙️ 管理工項／類型選單」新增（不能在這個表單裡直接打新選項，
    因為 Streamlit 表單裡的元件不會即時刷新，體驗上會像打不進去）。
    current：編輯時傳入該事項原本的值，下拉選單會預設選在它；
    key 呼叫端要帶上事項 id，這樣每次開始編輯都是全新的欄位，不會殘留上一次的選擇。"""
    options = list(data_store.get_options(category))
    if current and current not in options:
        options.append(current)  # 原本的值後來被從選單刪掉了：仍然保留並維持選取，不要悄悄換成別的
    if not options:
        st.warning(f"目前沒有{label}選項，請先到上方「⚙️ 管理選單」新增。")
        return ""
    index = options.index(current) if current in options else 0
    return st.selectbox(label, options, index=index, key=key)


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
            _audit("刪除選項", f"{label}：{v}")
            st.rerun()
    with st.form(f"add_{category}_form", clear_on_submit=True):
        c1, c2 = st.columns([4, 1])
        new_val = c1.text_input(f"新增{label}", key=f"new_{category}_input", label_visibility="collapsed", placeholder=f"新增{label}")
        if c2.form_submit_button("新增"):
            if new_val.strip():
                with st.spinner("新增中…"):
                    data_store.add_option(category, new_val.strip())
                _audit("新增選項", f"{label}：{new_val.strip()}")
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
        if data_store.is_shareable(todo):
            lines.append("🔁 可共用（可與其他工項共用移工）")

        images = [(todo["task"], todo["image_url"])] if todo.get("image_url") else None
        _send_discord_async("\n".join(lines), images=images)
    except Exception:
        traceback.print_exc()  # 組訊息出錯也不能影響已經存好的資料


def _is_done(todo):
    return str(todo.get("completed", "")).strip().upper() == "TRUE"


def _fmt_range(start, end):
    return start if start == end else f"{start}~{end}"


def _overlapping_assignments(all_dispatches, todos_by_id, start_date, end_date, exclude_id=None):
    """
    找出每位移工在 [start_date, end_date] 這段期間「已經有的安排」。
    回傳 {移工: [{"todo": 事項, "ranges": [重疊的派工日期, ...]}, ...]}（同一事項只列一次）。
    已完成的事項、已經被刪除的事項（孤兒派工紀錄，例如你直接在試算表刪掉事項那一列）不算安排。
    """
    result = {}
    for d in all_dispatches:
        if exclude_id and d["id"] == exclude_id:
            continue
        t = todos_by_id.get(d["todo_id"])
        if t is None or _is_done(t):
            continue
        try:
            ds = date.fromisoformat(d["start_date"])
            de = date.fromisoformat(d["end_date"])
        except (ValueError, TypeError):
            continue
        if not (ds <= end_date and de >= start_date):  # 期間沒有重疊
            continue
        for w in data_store.split_workers(d.get("workers", "")):
            entries = result.setdefault(w, [])
            entry = next((x for x in entries if x["todo"]["id"] == t["id"]), None)
            if entry is None:
                entry = {"todo": t, "ranges": []}
                entries.append(entry)
            entry["ranges"].append(_fmt_range(d["start_date"], d["end_date"]))
    return result


def _describe_assignments(entries):
    """把一位移工的所有既有安排寫成一行：「事項」（工項／地點，日期，可共用或不可共用）。"""
    parts = []
    for x in entries:
        t = x["todo"]
        place = f"{t['work_item']}／{t['location']}" if t.get("location") else t["work_item"]
        share = "可共用" if data_store.is_shareable(t) else "不可共用"
        parts.append(f"「{t['task']}」（{place}，{'、'.join(x['ranges'])}，{share}）")
    return "；".join(parts)


def _block_reason(entries, picked_todo_id, new_shareable):
    """這位移工能不能指派給目前這筆事項？可以就回傳 None，不行就回傳原因。
    規則：① 同一筆事項不能重複安排同一位移工；
          ② 只有「兩筆都不可共用」的安排才算衝突；只要其中一筆標記了「可共用」，就可以疊在一起。
    第 ② 點刻意設計成「跟誰先排無關」：先排獨占的事項 X、再把可共用的 S 疊上去，
    和先排 S 再排 X，結果一樣；否則之後回頭編輯 X 時，會被已經合法疊上去的 S 判成衝突而存不了。"""
    if any(x["todo"]["id"] == picked_todo_id for x in entries):
        return "本事項這段期間已安排過這位移工"
    if not new_shareable:
        exclusive = [x["todo"]["task"] for x in entries if not data_store.is_shareable(x["todo"])]
        if exclusive:
            names = "、".join(f"「{n}」" for n in exclusive)
            return f"本事項與{names}都不可共用，不能重疊"
    return None


def _notify_discord_dispatch(todo, dispatch):
    """新增派工紀錄後，在背景發一則 Discord 通知。"""
    try:
        rng = _fmt_range(dispatch["start_date"], dispatch["end_date"])
        lines = [
            "👷 **新增派工**" + ("　🔁 可共用" if data_store.is_shareable(todo) else ""),
            f"{todo['task']}",
            f"工項：{todo['work_item']}／類型：{todo['type']}",
        ]
        if todo.get("location"):
            lines.append(f"地點：{todo['location']}")
        if todo.get("engineer"):
            lines.append(f"指派人：{todo['engineer']}")
        lines.append(f"派工日期：{rng}")
        lines.append(f"移工：{dispatch['workers']}")
        images = [(todo["task"], todo["image_url"])] if todo.get("image_url") else None
        _send_discord_async("\n".join(lines), images=images)
    except Exception:
        traceback.print_exc()


def _notify_discord_dispatch_update(todo, old, new_start, new_end, new_workers, partial):
    """修改派工紀錄（臨時換人、調整日期）後，在背景發一則 Discord 通知，讓大家知道有異動。"""
    try:
        old_rng = _fmt_range(old["start_date"], old["end_date"])
        new_rng = _fmt_range(str(new_start), str(new_end))
        lines = [
            "🔄 **派工異動**",
            f"{todo['task']}",
            f"工項：{todo['work_item']}／類型：{todo['type']}",
        ]
        if todo.get("location"):
            lines.append(f"地點：{todo['location']}")
        if partial:
            lines.append(f"原本（{old_rng}）：{old['workers']}")
            lines.append(f"其中 {new_rng} 改為：{'、'.join(new_workers)}（其餘日期維持原樣）")
        else:
            lines.append(f"原本：{old_rng}　{old['workers']}")
            lines.append(f"改為：{new_rng}　{'、'.join(new_workers)}")
        _send_discord_async("\n".join(lines))
    except Exception:
        traceback.print_exc()


def _fmt_absence(a):
    reason = f"（{a['reason']}）" if a.get("reason") else ""
    wd = data_store.parse_weekdays(a.get("weekdays"))
    weekly = "　每週" + "、".join(data_store.WEEKDAY_NAMES[i] for i in sorted(wd)) + "固定休" if wd else ""
    return f"{_fmt_range(a['start_date'], a['end_date'])}{weekly}{reason}"


def _affected_dispatches(workers, start, end, weekdays, all_dispatches, todos_by_id):
    """列出「已經排進去、但落在休假期間」的派工紀錄（只看未完成事項），提醒使用者去換人。"""
    lines = []
    workers = set(workers)
    for d in all_dispatches:
        t = todos_by_id.get(d["todo_id"])
        if t is None or _is_done(t):
            continue
        rule = {"start_date": start, "end_date": end, "weekdays": weekdays}
        if not data_store.absence_hits(rule, d["start_date"], d["end_date"]):
            continue
        hit = workers & set(data_store.split_workers(d.get("workers", "")))
        for w in sorted(hit):
            lines.append(f"{w}：「{t['task']}」{_fmt_range(d['start_date'], d['end_date'])}")
    return lines


def _notify_discord_absence(action, workers, start, end, reason, weekdays=""):
    try:
        lines = [f"🏖️ **移工休假／回國{action}**", f"移工：{'、'.join(workers)}",
                 f"日期：{_fmt_absence({'start_date': start, 'end_date': end, 'reason': '', 'weekdays': weekdays})}"]
        if reason:
            lines.append(f"原因：{reason}")
        lines.append("這段期間無法派工。")
        _send_discord_async("\n".join(lines))
    except Exception:
        traceback.print_exc()


def render_absence_manager():
    """移工預設都有上班；在這裡登記某位移工某段日期休息或回國，之後那段期間不能派工。可編輯、可刪除。"""
    absences = data_store.get_absences()
    today_s = datetime_now_taipei_date().isoformat()

    editing = None
    eid = st.session_state.get("editing_absence_id")
    if eid:
        editing = next((a for a in absences if a["id"] == eid), None)
        if editing is None:  # 已被別人刪除
            st.session_state.editing_absence_id = None
    ekey = editing["id"] if editing else "new"

    current = sorted([a for a in absences if a["end_date"] >= today_s], key=lambda a: (a["start_date"], a["worker"]))
    past = sorted([a for a in absences if a["end_date"] < today_s], key=lambda a: a["start_date"], reverse=True)

    with st.expander(f"🏖️ 移工休假／回國登記（目前有效 {len(current)} 筆）", expanded=bool(editing)):
        st.caption("移工預設都有上班。登記後，該移工在這段日期內（含頭尾）無法被派工；日後行程有變可以編輯或刪除。")
        worker_options = data_store.get_options("worker")
        today = datetime_now_taipei_date()

        with st.form("absence_form", clear_on_submit=not editing):
            if editing:
                opts = list(worker_options)
                if editing["worker"] not in opts:
                    opts.append(editing["worker"])
                picked_workers = [st.selectbox("移工", opts, index=opts.index(editing["worker"]), key=f"absence_worker_{ekey}")]
            else:
                picked_workers = st.multiselect("移工（可複選，同一段日期一次登記多位）", worker_options, key=f"absence_workers_{ekey}")
            c1, c2 = st.columns(2)
            a_start = c1.date_input("休假開始日期", value=_safe_date(editing["start_date"]) or today if editing else today, key=f"absence_start_{ekey}")
            a_end = c2.date_input("休假結束日期（含當天）", value=_safe_date(editing["end_date"]) or today if editing else today, key=f"absence_end_{ekey}")
            wd_names = data_store.WEEKDAY_NAMES
            wd_default = [wd_names[i] for i in sorted(data_store.parse_weekdays(editing.get("weekdays")))] if editing else []
            weekly_days = st.multiselect(
                "每週固定休（選填）", wd_names, default=wd_default, key=f"absence_weekly_{ekey}",
                help="不選＝上面日期區間內每一天都休。選了（例如「日」）＝只在這個日期區間內的每週日休。",
            )
            reason = st.text_input("原因（選填，例如：休假、回國、就醫）", value=editing.get("reason", "") if editing else "", key=f"absence_reason_{ekey}")
            if editing:
                b1, b2 = st.columns(2)
                absence_submit = b1.form_submit_button("更新休假", width="stretch")
                absence_cancel = b2.form_submit_button("取消編輯", width="stretch")
            else:
                absence_submit = st.form_submit_button("登記休假")
                absence_cancel = False

        if absence_cancel:
            st.session_state.editing_absence_id = None
            st.rerun()

        if absence_submit:
            if not picked_workers:
                st.error("請至少選一位移工")
            elif a_end < a_start:
                st.error("結束日期不能早於開始日期")
            elif _is_duplicate_submit(("absence", ekey, tuple(picked_workers), str(a_start), str(a_end), reason.strip(), tuple(weekly_days))):
                st.info("偵測到重複送出（10 秒內內容完全相同），已略過，沒有重複儲存。")
            else:
                wd_str = ",".join(str(wd_names.index(x)) for x in weekly_days)
                with st.spinner("儲存中，請稍候…"):
                    if editing:
                        data_store.update_absence(editing["id"], picked_workers[0], str(a_start), str(a_end), reason.strip(), wd_str)
                    else:
                        data_store.add_absences(picked_workers, str(a_start), str(a_end), reason.strip(), wd_str)
                todos_by_id = {t["id"]: t for t in data_store.get_todos()}
                affected = _affected_dispatches(picked_workers, str(a_start), str(a_end), wd_str, data_store.get_dispatches(), todos_by_id)
                _audit("更新休假" if editing else "登記休假", "、".join(picked_workers),
                       _fmt_absence({"start_date": str(a_start), "end_date": str(a_end), "reason": reason.strip(), "weekdays": wd_str}))
                msg = f"✅ 已{'更新' if editing else '登記'}休假：{'、'.join(picked_workers)}　{_fmt_range(str(a_start), str(a_end))}"
                if affected:
                    msg += "\n\n⚠️ 以下已排好的派工落在休假期間，請到下方「派工紀錄」編輯換人：\n" + "\n".join(f"・{x}" for x in affected)
                st.session_state.editing_absence_id = None
                _flash(msg)
                _notify_discord_absence("更新" if editing else "登記", picked_workers, str(a_start), str(a_end), reason.strip(), wd_str)
                st.rerun()

        st.markdown("**目前有效的休假紀錄**")
        if not current:
            st.caption("目前沒有任何休假登記，所有移工都可以派工。")
        for a in current:
            r1, r2, r3 = st.columns([5, 1, 1])
            r1.write(f"**{a['worker']}**　{_fmt_absence(a)}")
            if r2.button("✏️ 編輯", key=f"edit_absence_{a['id']}"):
                st.session_state.editing_absence_id = a["id"]
                st.rerun()
            if r3.button("刪除", key=f"del_absence_{a['id']}"):
                with st.spinner("刪除中…"):
                    data_store.delete_absence(a["id"])
                _audit("刪除休假", a["worker"], _fmt_absence(a))
                _flash(f"已刪除 {a['worker']} {_fmt_range(a['start_date'], a['end_date'])} 的休假登記")
                st.rerun()

        if past and st.toggle(f"顯示已結束的休假紀錄（{len(past)} 筆）", key="show_past_absences"):
            for a in past:
                st.caption(f"{a['worker']}　{_fmt_absence(a)}")


def _render_complete_form(t):
    """標記完成前，順手留下這次的經驗（可留白，直接完成）。有寫的話會自動存進「經驗分享區」。"""
    with st.form(f"complete_form_{t['id']}"):
        st.markdown(f"**完成「{t['task']}」** — 有什麼要留給大家的經驗嗎？（可留白）")
        details = st.text_area("須注意細節", key=f"cdetails_{t['id']}")
        mistakes = st.text_area("錯誤經驗／踩過的雷", key=f"cmistakes_{t['id']}")
        b1, b2 = st.columns(2)
        confirm = b1.form_submit_button("確認完成", width="stretch")
        cancel = b2.form_submit_button("取消", width="stretch")
    if cancel:
        st.session_state.completing_id = None
        st.rerun()
    if confirm:
        if _is_duplicate_submit(("complete", t["id"])):
            st.info("偵測到重複送出，已略過。")
            return
        with st.spinner("更新中…"):
            data_store.mark_completed(t["id"], True)
            wrote_exp = bool(details.strip() or mistakes.strip())
            if wrote_exp:
                data_store.add_experience(
                    t["work_item"], details.strip() or "－", mistakes.strip() or "－",
                    source=f"完成事項：{t['task']}",
                )
        _audit("標記完成", t["task"], "並留下經驗" if wrote_exp else "")
        st.session_state.completing_id = None
        _flash(f"✅ 已將「{t['task']}」標記完成（可到「📜 歷史記錄」查詢）" + ("，經驗已存進經驗分享區" if wrote_exp else ""))
        st.rerun()


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

        ekey = editing["id"] if editing else "new"
        work_item = _select_with_add(
            "工項", "work_item", f"work_item_select_{ekey}", current=editing["work_item"] if editing else None)
        type_ = _select_with_add(
            "類型", "type", f"type_select_{ekey}", current=editing["type"] if editing else None)
        engineer = _select_with_add(
            "指派人（工程師）", "engineer", f"engineer_select_{ekey}",
            current=(editing.get("engineer") or None) if editing else (
                st.session_state.operator if st.session_state.operator in data_store.get_options("engineer") else None))

        shareable = st.checkbox(
            "🔁 可共用（此工項可以與其他工項共用移工，例如只需 1 小時或半天就能完成）",
            value=data_store.is_shareable(editing) if editing else False,
            key=f"shareable_{st.session_state.editing_id or 'new'}",
            help="勾選後，在「移工指派」可以把已經有其他安排的移工也派給這個工項（會列出他目前的安排讓你判斷）。",
        )

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
            str(start_date), str(end_date), work_item, type_, engineer, shareable,
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
                        shareable="TRUE" if shareable else "FALSE",
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
                        engineer=engineer, shareable=shareable,
                    )

            st.session_state.editing_id = None
            _audit(f"{action}代辦事項", task.strip(), f"{work_item}／{type_}　指派人：{engineer or '－'}　{start_date}~{end_date}")
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
        title = f"{label} {t['task']}" + (" 🔁" if data_store.is_shareable(t) else "")

        with st.expander(title, expanded=(st.session_state.get("completing_id") == t["id"])):
            st.write(f"**期限：** {date_range}")
            st.write(f"**工項／類型：** {t['work_item']} ／ {t['type']}")
            if t.get("location"):
                st.write(f"**地點：** {t['location']}")
            if t.get("engineer"):
                st.write(f"**指派人：** {t['engineer']}")
            if data_store.is_shareable(t):
                st.write("**可共用：** 是（可與其他工項共用移工）")
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

            if st.session_state.get("completing_id") == t["id"]:
                _render_complete_form(t)
                continue
            c1, c2 = st.columns(2)
            if c1.button("標記完成", key=f"done_{t['id']}"):
                st.session_state.completing_id = t["id"]
                st.rerun()
            if c2.button("編輯", key=f"edit_{t['id']}"):
                st.session_state.editing_id = t["id"]
                st.rerun()


def _plan_copy(src_s, dst, all_dispatches, todos_by_id, absences):
    """規劃「把 src 那天的派工複製到 dst」。回傳 (要新增的紀錄, 略過的說明)。
    規則跟手動指派完全一樣：休假的不複製、與其他不可共用事項衝突的不複製、目標日不在事項區間內的不複製。"""
    dst_s = str(dst)
    dst_assign = _overlapping_assignments(all_dispatches, todos_by_id, dst, dst)
    dst_absent = data_store.absent_workers_on(absences, dst_s, dst_s)
    to_add, skipped = [], []
    seen = set()
    for d in sorted(all_dispatches, key=lambda x: x["start_date"]):
        t = todos_by_id.get(d["todo_id"])
        if t is None or _is_done(t) or not (d["start_date"] <= src_s <= d["end_date"]):
            continue
        if not (t["start_date"] <= dst_s <= t["end_date"]):
            skipped.append(f"「{t['task']}」：{dst_s} 不在事項期間（{_fmt_range(t['start_date'], t['end_date'])}）內")
            continue
        ok = []
        for w in data_store.split_workers(d.get("workers", "")):
            if (t["id"], w) in seen:
                continue
            seen.add((t["id"], w))
            if w in dst_absent:
                skipped.append(f"「{t['task']}」的 {w}：目標日休假／回國")
                continue
            reason = _block_reason(dst_assign.get(w, []), t["id"], data_store.is_shareable(t))
            if reason:
                skipped.append(f"「{t['task']}」的 {w}：{reason}")
                continue
            ok.append(w)
            dst_assign.setdefault(w, []).append({"todo": t, "ranges": [dst_s]})
        if ok:
            to_add.append((t, ok))
    return to_add, skipped


def render_copy_dispatch(todos_by_id, all_dispatches):
    today = datetime_now_taipei_date()
    with st.expander("📋 複製某天的派工到另一天（例如把今天的人力複製到明天）"):
        c1, c2 = st.columns(2)
        src = c1.date_input("來源日期（複製這天的派工）", value=today, key="copy_src")
        dst = c2.date_input("目標日期", value=today + timedelta(days=1), key="copy_dst")
        if src == dst:
            st.info("來源與目標是同一天，不需要複製。")
            return
        to_add, skipped = _plan_copy(str(src), dst, all_dispatches, todos_by_id, data_store.get_absences())
        if not to_add and not skipped:
            st.caption(f"{src} 這天沒有可以複製的派工。")
            return
        if to_add:
            st.markdown(f"**會複製到 {dst} 的派工：**")
            for t, ws in to_add:
                st.write(f"・{t['task']}：{'、'.join(ws)}")
        if skipped:
            st.warning("以下不會複製（請之後手動安排）：\n" + "\n".join(f"・{x}" for x in skipped))
        if to_add and st.button(f"確認複製到 {dst}", key="copy_confirm"):
            if _is_duplicate_submit(("copy", str(src), str(dst))):
                st.info("偵測到重複送出，已略過。")
                return
            with st.spinner("複製中，請稍候…"):
                data_store.add_dispatches([(t["id"], str(dst), str(dst), data_store.join_workers(ws)) for t, ws in to_add])
            _audit("複製派工", f"{src} → {dst}", f"{len(to_add)} 個事項")
            lines = [f"📋 **複製派工**　{src} → {dst}"] + [f"・{t['task']}：{'、'.join(ws)}" for t, ws in to_add]
            _send_discord_async("\n".join(lines))
            _flash(f"✅ 已複製 {len(to_add)} 個事項的派工到 {dst}" + (f"（另有 {len(skipped)} 項略過）" if skipped else ""))
            st.rerun()


def render_worker_week_tab():
    st.subheader("📆 移工週表")
    st.caption("橫軸是日期、縱軸是移工。空白＝可以派工；🏖️＝休假／回國；其餘是當天被派去的事項（有 🔁 代表同時做多件）。")
    today = datetime_now_taipei_date()
    start = st.date_input("起始日期（往後顯示 7 天）", value=today, key="week_start")
    days = [start + timedelta(days=i) for i in range(7)]
    day_s = [d.isoformat() for d in days]

    todos_by_id = {t["id"]: t for t in data_store.get_todos()}
    dispatches = data_store.get_dispatches()
    absences = data_store.get_absences()
    workers = data_store.get_options("worker")

    cell = {w: {d: [] for d in day_s} for w in workers}
    for d in dispatches:
        t = todos_by_id.get(d["todo_id"])
        if t is None or _is_done(t):
            continue
        for ds_ in day_s:
            if d["start_date"] <= ds_ <= d["end_date"]:
                for w in data_store.split_workers(d.get("workers", "")):
                    cell.setdefault(w, {x: [] for x in day_s})[ds_].append(t["task"])

    wd = data_store.WEEKDAY_NAMES
    rows = []
    for w, per_day in cell.items():
        row = {"移工": w}
        for d, ds_ in zip(days, day_s):
            tasks = per_day[ds_]
            off = bool(data_store.absent_workers_on([a for a in absences if a["worker"] == w], ds_, ds_))
            text = ""
            if off:
                text = "🏖️休" + ("（⚠️已排班：" + "、".join(tasks) + "）" if tasks else "")
            elif tasks:
                text = ("🔁 " if len(tasks) > 1 else "") + "、".join(tasks)
            row[f"{d.month}/{d.day}（{wd[d.weekday()]}）"] = text
        rows.append(row)
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def render_dispatch_assignment_tab():
    st.subheader("👷 移工指派")

    render_absence_manager()

    all_todos = data_store.get_todos()
    active_todos = [t for t in all_todos if not _is_done(t)]

    if not active_todos:
        st.info("目前沒有可指派的代辦事項，請先到「📋 代辦事項」分頁新增。")
        return

    all_dispatches = data_store.get_dispatches()
    todos_by_id = {t["id"]: t for t in all_todos}
    today = datetime_now_taipei_date()
    active_todos.sort(key=lambda t: t["start_date"])

    render_copy_dispatch(todos_by_id, all_dispatches)

    def _fmt(t):
        rng = _fmt_range(t["start_date"], t["end_date"])
        loc = f"／{t['location']}" if t.get("location") else ""
        return f"{t['task']}（{rng}{loc}）" + (" 🔁" if data_store.is_shareable(t) else "")

    # 選項用「事項 id」而不是「第幾筆」：多人同時使用時，別人新增或完成了事項，
    # 清單的順序會變，用順序當選項的話你選的事項會悄悄變成別的事項。
    ids = [t["id"] for t in active_todos]
    labels_by_id = {t["id"]: _fmt(t) for t in active_todos}

    # ---- 是否正在編輯某一筆派工紀錄
    editing_dispatch = None
    eid = st.session_state.get("editing_dispatch_id")
    if eid:
        editing_dispatch = next((d for d in all_dispatches if d["id"] == eid and d["todo_id"] in labels_by_id), None)
        if editing_dispatch is None:  # 已被別人刪除，或它所屬的事項已經完成
            st.session_state.editing_dispatch_id = None
    # 編輯用的欄位 key 都帶上這個值：每次開始編輯都是全新的欄位，不會殘留上一次的勾選或日期
    edit_key = editing_dispatch["id"] if editing_dispatch else "new"

    if editing_dispatch:
        picked_id = editing_dispatch["todo_id"]
        st.info(
            f"✏️ 正在編輯派工紀錄：{_fmt_range(editing_dispatch['start_date'], editing_dispatch['end_date'])}　"
            f"{editing_dispatch['workers']}"
        )
        st.selectbox(
            "代辦事項（編輯中不能更換）", ids, index=ids.index(picked_id),
            format_func=lambda i: labels_by_id[i], disabled=True, key=f"dispatch_todo_select_{edit_key}",
        )
    else:
        remembered = st.session_state.get("_dispatch_todo_id")
        picked_id = st.selectbox(
            "選擇代辦事項", ids,
            index=ids.index(remembered) if remembered in ids else 0,
            format_func=lambda i: labels_by_id[i],
            key="dispatch_todo_select_new",
        )
        st.session_state["_dispatch_todo_id"] = picked_id  # 切到別頁再回來時，還記得選到哪一筆
    todo = todos_by_id[picked_id]
    new_shareable = data_store.is_shareable(todo)

    todo_start = date.fromisoformat(todo["start_date"])
    todo_end = date.fromisoformat(todo["end_date"])
    st.caption(
        f"這筆事項整體區間：{todo_start} ～ {todo_end}　｜　"
        + ("🔁 可共用" if new_shareable else "不可共用")
    )

    # ---- 日期
    partial = False
    rec_s = rec_e = None
    if editing_dispatch:
        rec_s = _safe_date(editing_dispatch["start_date"]) or todo_start
        rec_e = _safe_date(editing_dispatch["end_date"]) or rec_s
        if rec_s < rec_e:
            mode = st.radio(
                "修改方式",
                ["整段一起改（可調整日期與移工）", "只改其中幾天（其餘日期維持原本的移工）"],
                key=f"dispatch_edit_mode_{edit_key}",
            )
            partial = mode.startswith("只改")

    # 日期欄位的 key 帶上事項 id 與 edit_key：每筆事項的可選範圍不同，換事項時要重新初始化，
    # 否則上一筆事項選的日期可能超出新事項的範圍而出錯。
    if partial:
        c1, c2 = st.columns(2)
        d_start = c1.date_input("要修改的開始日期", value=rec_s, min_value=rec_s, max_value=rec_e, key=f"dispatch_seg_start_{edit_key}")
        d_end = c2.date_input("要修改的結束日期", value=rec_e, min_value=rec_s, max_value=rec_e, key=f"dispatch_seg_end_{edit_key}")
    else:
        default_single = min(max(today, todo_start), todo_end)
        if editing_dispatch:
            init_s = min(max(rec_s, todo_start), todo_end)
            init_e = min(max(rec_e, todo_start), todo_end)
        else:
            init_s = init_e = default_single
        use_range = st.checkbox(
            "指定一段區間（不勾選的話，預設只有選的那一天生效）",
            value=bool(editing_dispatch and init_s != init_e),
            key=f"dispatch_use_range_{edit_key}",
        )
        if use_range:
            c1, c2 = st.columns(2)
            d_start = c1.date_input("派工開始日期", value=init_s if editing_dispatch else todo_start, min_value=todo_start, max_value=todo_end, key=f"dispatch_range_start_{picked_id}_{edit_key}")
            d_end = c2.date_input("派工結束日期", value=init_e if editing_dispatch else todo_end, min_value=todo_start, max_value=todo_end, key=f"dispatch_range_end_{picked_id}_{edit_key}")
        else:
            d_start = st.date_input("派工日期（只有這一天生效）", value=init_s, min_value=todo_start, max_value=todo_end, key=f"dispatch_single_date_{picked_id}_{edit_key}")
            d_end = d_start

    # ---- 每位移工在這段期間「已經有的安排」，以及能不能再派給這筆事項
    assignments = _overlapping_assignments(
        all_dispatches, todos_by_id, d_start, d_end,
        exclude_id=editing_dispatch["id"] if editing_dispatch else None,
    )
    blocked = {}
    for w, entries in assignments.items():
        reason = _block_reason(entries, picked_id, new_shareable)
        if reason:
            blocked[w] = reason

    worker_options = data_store.get_options("worker")
    if not worker_options:
        st.warning("目前沒有移工選項，請先到「📋 代辦事項」分頁打開「⚙️ 管理選單」新增。")
        return

    if new_shareable:
        st.caption("🔁 本事項已標記「可共用」：可以指派給已有其他安排的移工。下方會列出他目前的安排，請自己判斷適不適合一起做。")
    else:
        st.caption("本事項未標記「可共用」：期間內已被其他「不可共用」事項安排的移工不能選（會列出原因）；他若只有「可共用」的短工項，仍然可以選，下方會列出讓你判斷。若要讓同一位移工兼顧多個獨占工項，請到「📋 代辦事項」把其中一筆改成「可共用」。")

    default_workers = set(data_store.split_workers(editing_dispatch["workers"])) if editing_dispatch else set()

    # 這段派工日期內有登記休假／回國的移工（有任何一天重疊就不能派）
    absent = data_store.absent_workers_on(data_store.get_absences(), str(d_start), str(d_end))

    with st.form("dispatch_form", clear_on_submit=not editing_dispatch):
        st.markdown("**選擇移工（可複選）**")
        selected = []
        for w in worker_options:
            entries = assignments.get(w, [])
            reason = blocked.get(w)
            label = w
            disabled = False
            ab = absent.get(w)
            if ab:
                why = "；".join(_fmt_absence(a) for a in ab)
                if w in default_workers:
                    label = f"{w}　🏖️ 這段期間休假／回國（{why}），請取消勾選、換人或調整日期"
                else:
                    label = f"{w}　🏖️ 休假／回國，無法派工（{why}）"
                    disabled = True
            elif reason:
                if w in default_workers:
                    # 編輯模式：原本就在名單裡、但現在跟其他安排衝突的人，要能取消勾選（換人）
                    label = f"{w}　⚠️ 與其他安排衝突（{reason}），請取消勾選或調整日期｜已有安排：{_describe_assignments(entries)}"
                else:
                    label = f"{w}　⚠️ 無法選擇（{reason}）｜已有安排：{_describe_assignments(entries)}"
                    disabled = True
            elif entries:
                label = f"{w}　🔁 已有安排（可疊加，請確認適合）：{_describe_assignments(entries)}"
            checked = st.checkbox(label, value=(w in default_workers), disabled=disabled, key=f"dispatch_worker_{edit_key}_{w}")
            if checked:
                selected.append(w)

        if editing_dispatch:
            col_a, col_b = st.columns(2)
            submitted = col_a.form_submit_button("更新派工", width="stretch")
            cancel = col_b.form_submit_button("取消編輯", width="stretch")
        else:
            submitted = st.form_submit_button("指派")
            cancel = False

    if cancel:
        st.session_state.editing_dispatch_id = None
        st.rerun()

    if submitted:
        conflicts = [w for w in selected if w in blocked and w not in absent]
        absent_picked = [w for w in selected if w in absent]
        if d_end < d_start:
            st.error("結束日期不能早於開始日期")
        elif not selected:
            st.error("請至少選一位移工")
        elif absent_picked:
            st.error("這些移工在這段期間休假／回國，無法派工：" + "、".join(
                f"{w}（{'；'.join(_fmt_absence(a) for a in absent[w])}）" for w in absent_picked))
        elif conflicts:
            st.error("這些移工在這段期間與其他安排衝突：" + "、".join(f"{w}（{blocked[w]}）" for w in conflicts))
        elif _is_duplicate_submit((
            "dispatch", edit_key, picked_id, str(d_start), str(d_end), tuple(selected), partial,
        )):
            st.info("偵測到重複送出（10 秒內內容完全相同），已略過，沒有重複處理。")
        else:
            workers_str = data_store.join_workers(selected)
            if editing_dispatch:
                with st.spinner("更新中，請稍候…"):
                    if partial:
                        data_store.replace_dispatch_segment(editing_dispatch["id"], str(d_start), str(d_end), workers_str)
                    else:
                        data_store.update_dispatch(editing_dispatch["id"], str(d_start), str(d_end), workers_str)
                st.session_state.editing_dispatch_id = None
                _audit("更新派工" + ("（部分日期）" if partial else ""), todo["task"],
                       f"{_fmt_range(editing_dispatch['start_date'], editing_dispatch['end_date'])} {editing_dispatch['workers']} → {_fmt_range(str(d_start), str(d_end))} {workers_str}")
                _flash(f"✅ 已更新派工：{_fmt_range(str(d_start), str(d_end))}　{'、'.join(selected)}")
                _notify_discord_dispatch_update(todo, editing_dispatch, d_start, d_end, selected, partial)
            else:
                with st.spinner("指派中，請稍候…"):
                    record = data_store.add_dispatch(todo["id"], str(d_start), str(d_end), workers_str)
                _audit("新增派工", todo["task"], f"{_fmt_range(str(d_start), str(d_end))} {workers_str}")
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
        rng = _fmt_range(d["start_date"], d["end_date"])
        c1, c2, c3 = st.columns([5, 1, 1])
        c1.write(f"**{rng}**：{d['workers']}")
        if c2.button("✏️ 編輯", key=f"edit_dispatch_{d['id']}"):
            st.session_state.editing_dispatch_id = d["id"]
            st.rerun()
        if c3.button("刪除", key=f"del_dispatch_{d['id']}"):
            with st.spinner("刪除中…"):
                data_store.delete_dispatch(d["id"])
            _audit("刪除派工", todo["task"], f"{rng} {d['workers']}")
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

    day_absent = data_store.absent_workers_on(data_store.get_absences(), str(pick_date), str(pick_date))
    if day_absent:
        st.info("🏖️ 當天休假／回國：" + "；".join(
            f"{w}（{'；'.join(_fmt_absence(a) for a in ab)}）" for w, ab in sorted(day_absent.items())))
        scheduled_absent = sorted(w for w in total_workers if w in day_absent)
        if scheduled_absent:
            st.warning("⚠️ 以下移工當天已被排進派工、但登記為休假／回國，請到「👷 移工指派」換人：" + "、".join(scheduled_absent))

    c1, c2 = st.columns(2)
    c1.metric("當天派工項目數", len(merged_entries))
    c2.metric("涉及移工人數", len(total_workers))

    # 同一位移工當天同時出現在不只一個工項（共用），特別標出來方便一眼看到
    worker_tasks = {}
    for entry in merged_entries:
        for w in entry["workers"]:
            worker_tasks.setdefault(w, []).append(entry["todo"]["task"])
    shared_today = {w: ts for w, ts in worker_tasks.items() if len(ts) > 1}
    if shared_today:
        st.info("🔁 當天同時支援多個工項的移工：" + "；".join(
            f"{w}（{'、'.join(ts)}）" for w, ts in sorted(shared_today.items())
        ))

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
                st.markdown(f"{icon} **{t['task']}**{'　🔁 可共用' if data_store.is_shareable(t) else ''}{'　（已完成）' if completed else ''}")
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
    include_archive = st.toggle("包含已封存的舊事項（唯讀）", key="history_include_archive")
    archived_dispatches = []
    if include_archive:
        todos = todos + [dict(t, _archived=True) for t in data_store.get_archived_todos()]
        archived_dispatches = data_store.get_archived_dispatches()
    if not todos:
        st.info("目前沒有任何代辦事項紀錄。")
        _render_history_admin()
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
        _render_history_admin()
        return

    dispatches_by_todo = {k: list(v) for k, v in data_store.get_dispatches_by_todo().items()}  # 一次分好組
    for d in archived_dispatches:
        dispatches_by_todo.setdefault(d["todo_id"], []).append(d)

    rows = []
    for t in filtered:
        completed = str(t.get("completed", "")).strip().upper() == "TRUE"
        date_range = t["start_date"] if t["start_date"] == t["end_date"] else f"{t['start_date']}~{t['end_date']}"
        dispatches = dispatches_by_todo.get(t["id"], [])
        workers_set = {w for d in dispatches for w in data_store.split_workers(d.get("workers", ""))}
        rows.append({
            "狀態": "🗄️已封存" if t.get("_archived") else ("✅已完成" if completed else "🔸進行中"),
            "事項說明": t["task"],
            "工項": t["work_item"],
            "類型": t["type"],
            "地點": t.get("location") or "－",
            "指派人": t.get("engineer") or "－",
            "期限": date_range,
            "曾派工移工": "、".join(sorted(workers_set)) if workers_set else "－",
        })

    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    live = [t for t in filtered if not t.get("_archived")]
    with st.expander("✏️ 修正完成狀態（例如不小心標錯）"):
        if not live:
            st.caption("篩選結果裡沒有可以修正的事項（已封存的事項是唯讀的）。")
            _render_history_admin()
            return
        ids = [t["id"] for t in live]
        by_id = {t["id"]: t for t in live}

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
            _audit("取消完成" if target_completed else "標記完成", target["task"], "（歷史頁修正）")
            _flash(f"已將「{target['task']}」{'改回進行中' if target_completed else '標記為已完成'}")
            st.rerun()

    _render_history_admin()


def _render_history_admin():
    """封存舊事項、查看操作紀錄。"""
    today = datetime_now_taipei_date()
    with st.expander("🗄️ 封存舊事項（事項變多、App 變慢時使用）"):
        st.caption("把「已完成、且結束日期早於 N 個月前」的事項連同派工紀錄，搬到雲端的封存分頁。不會刪除資料，之後在上方打開「包含已封存」仍可查詢；主清單與每次讀取會變輕。")
        months = st.number_input("封存幾個月前完成的事項", min_value=1, max_value=36, value=3, step=1, key="archive_months")
        cutoff = (today - timedelta(days=30 * int(months))).isoformat()
        n_eligible = sum(
            1 for t in data_store.get_todos()
            if _is_done(t) and t.get("end_date") and t["end_date"] < cutoff
        )
        st.write(f"符合條件（結束日期早於 {cutoff}）：**{n_eligible}** 筆")
        if n_eligible and st.button(f"封存這 {n_eligible} 筆", key="archive_confirm"):
            if _is_duplicate_submit(("archive", cutoff)):
                st.info("偵測到重複送出，已略過。")
            else:
                with st.spinner("封存中，請稍候…"):
                    moved = data_store.archive_completed_before(cutoff)
                _audit("封存舊事項", f"早於 {cutoff}", f"{moved} 筆")
                _flash(f"✅ 已封存 {moved} 筆事項")
                st.rerun()

    if st.toggle("🧾 顯示操作紀錄（最近 200 筆）", key="show_audit_log"):
        log = data_store.get_audit_log()
        if not log:
            st.caption("目前還沒有操作紀錄。")
        else:
            st.dataframe(
                pd.DataFrame(log[-200:][::-1]).rename(columns={
                    "time": "時間", "operator": "操作人", "action": "動作", "target": "對象", "detail": "內容"}),
                width="stretch", hide_index=True,
            )


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
            _audit("發布經驗", work_item.strip())
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
                _audit("刪除經驗", e["work_item"])
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

    head_l, head_m, head_r = st.columns([5, 2, 1])
    head_l.title("🏗️ 營造管理系統")
    if st.session_state.operator:
        head_m.markdown(f"👤 **{st.session_state.operator}**")
        if head_m.button("切換身分", key="switch_operator"):
            st.session_state.operator = ""
            if "user" in st.query_params:
                del st.query_params["user"]
            st.rerun()
    if head_r.button("🔄 重新整理", help="從雲端重新讀取最新資料。如果你剛剛直接在 Google 試算表裡改了資料，按這個才會立刻看到。"):
        data_store.clear_all_caches()
        st.rerun()

    # 用「頁面切換」取代 st.tabs：st.tabs 每次互動都會把 5 個分頁的程式全部執行一遍，
    # 這裡只會執行你目前所在的那一頁，速度差很多。
    try:
        if not _render_operator_gate():
            return
    except Exception as e:
        st.session_state.pop("_last_submit", None)
        traceback.print_exc()
        _show_friendly_error(e)
        return

    pages = {
        "📋 代辦事項": render_todo_tab,
        "👷 移工指派": render_dispatch_assignment_tab,
        "📅 派工總表": render_dispatch_tab,
        "📆 移工週表": render_worker_week_tab,
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
