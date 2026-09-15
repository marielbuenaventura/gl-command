"""Module 2 — Financial Close & GL Operations."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from glcmd import config
from glcmd.services import close as close_svc
from glcmd.services import journals
from glcmd.ui import components as ui

conn = ui.page(
    "Financial Close & GL Operations",
    "📅",
    "The close as a working-day model, and a journal workspace where the dual "
    "sign-off is enforced by the system rather than promised by policy.",
)
ctx = ui.sidebar(conn, show_period=True)
actor, period, entity = ctx["actor"], ctx["period"], ctx["entity"]
role = ctx["role"]

prog = close_svc.progress(conn, period)
je_stats = journals.stats(conn, period)

ui.tiles([
    ("Close status", f"{prog['pct']:.0f}%",
     f"{prog['complete']} / {prog['total']} tasks · target "
     f"{prog['target_close']:%d %b}", "good" if prog["on_track"] else "warning"),
    ("Working days left",
     f"{prog['days_remaining']}" if prog["days_remaining"] >= 0
     else f"−{abs(prog['days_remaining'])}",
     f"WD+1 was {prog['close_start']:%d %b}",
     "critical" if prog["days_remaining"] < 0 else "good"),
    ("Blocked tasks", f"{prog['blocked']}", "Escalate before WD+2",
     "critical" if prog["blocked"] else "good"),
    ("Journals in review", f"{je_stats['pending']}",
     f"{je_stats['posted']} posted · {je_stats['rejected']} rejected",
     "warning" if je_stats["pending"] else "good"),
])

ui.rule()

tab_close, tab_je, tab_new, tab_gov = st.tabs(
    ["Close checklist", "Journal workspace", "Raise a journal", "Governance"]
)

# ==========================================================================
with tab_close:
    c1, c2 = st.columns([1, 1])
    with c1:
        wd = close_svc.by_workday(conn, period)
        ui.progress_bar(wd, "workday", "pct", title="Completion by working day", height=220)
    with c2:
        hist = close_svc.sla_history(conn)
        if not hist.empty:
            ui.bar_diverging(hist, "period", "days_variance",
                             title="Days against the 3-day target",
                             tooltip=["period", "target_close", "actual_close"],
                             height=220, label_fmt="+,.0f")
        else:
            st.caption("No completed closes yet — the SLA trend appears after the first sign-off.")

    if not prog["critical_path"].empty:
        st.markdown("#### Critical path — what is holding the close")
        cp = prog["critical_path"]
        ui.dataframe(
            cp[["workday", "stream", "task", "owner", "status", "due_date", "blocker"]].rename(
                columns={"workday": "WD", "stream": "Stream", "task": "Task",
                         "owner": "Owner", "status": "Status", "due_date": "Due",
                         "blocker": "Blocker"})
        )

    ui.rule()
    st.markdown("#### Checklist")
    tasks = close_svc.tasks(conn, period, entity)
    if tasks.empty:
        st.info("No checklist for this period yet.")
    else:
        stream_filter = st.multiselect("Filter by stream", config.CLOSE_STREAMS, [])
        view = tasks[tasks["stream"].isin(stream_filter)] if stream_filter else tasks

        for workday in config.CLOSE_WORKDAYS:
            block = view[view["workday"] == workday]
            if block.empty:
                continue
            done = int((block["status"] == "Complete").sum())
            st.markdown(f"**{workday}** &nbsp;<span style='color:{ui.MUTED};font-size:.82rem'>"
                        f"{done}/{len(block)} complete · due "
                        f"{block.iloc[0]['due_date']:%a %d %b}</span>",
                        unsafe_allow_html=True)
            for r in block.itertuples():
                cols = st.columns([0.06, 0.52, 0.16, 0.13, 0.13])
                mark = {"Complete": "✓", "Blocked": "✕", "In progress": "◐"}.get(r.status, "○")
                colour = {"Complete": ui.STATUS["good"], "Blocked": ui.STATUS["critical"],
                          "In progress": ui.STATUS["warning"]}.get(r.status, ui.MUTED)
                cols[0].markdown(f"<span style='color:{colour};font-size:1.1rem'>{mark}</span>",
                                 unsafe_allow_html=True)
                cols[1].markdown(
                    f"{r.task}  \n<span style='color:{ui.MUTED};font-size:.76rem'>"
                    f"{r.stream}" + (f" · {r.control_ref}" if r.control_ref else "")
                    + (f" · ⚠ {r.blocker}" if r.blocker else "") + "</span>",
                    unsafe_allow_html=True,
                )
                cols[2].markdown(
                    f"<span style='font-size:.78rem;color:{ui.INK_2}'>"
                    f"{config.USER_BY_NAME[r.owner].display_name if r.owner in config.USER_BY_NAME else r.owner}"
                    f"</span>", unsafe_allow_html=True,
                )
                new = cols[3].selectbox("s", close_svc.TASK_STATUSES,
                                        index=close_svc.TASK_STATUSES.index(r.status),
                                        key=f"ct_{r.id}", label_visibility="collapsed")
                if cols[4].button("Save", key=f"cb_{r.id}", width='stretch'):
                    blocker = None
                    if new == "Blocked":
                        blocker = "Flagged as blocked — add detail in the audit trail."
                    close_svc.set_task_status(conn, int(r.id), new, actor, blocker)
                    st.rerun()
            st.write("")

    ui.rule()
    st.markdown("#### Period sign-off")
    ui.note(
        "Sign-off is a hard gate. The period cannot be closed while any checklist task "
        "is open, any journal sits unposted, or any reconciliation is unreviewed — "
        "the three things an auditor will test first."
    )
    if st.button("Sign off period " + period, type="primary"):
        result = close_svc.sign_off_period(conn, period, actor)
        (st.success if result["ok"] else st.error)(result["message"])

# ==========================================================================
with tab_je:
    st.markdown("#### Journal entries")
    c1, c2 = st.columns([1, 3])
    status_filter = c1.selectbox("Status", ["All"] + config.JE_STATUSES)
    entries = journals.list_entries(conn, period, status_filter, entity)

    if entries.empty:
        st.info("No journal entries for this selection.")
    else:
        summary = entries[["je_ref", "entity", "description", "je_type", "risk_tag",
                           "total_debit", "status", "preparer", "reviewer",
                           "controller", "support_count"]].copy()
        summary["total_debit"] = summary["total_debit"].map(lambda v: ui.money(v, 2))
        ui.dataframe(summary.rename(columns={
            "je_ref": "Ref", "entity": "Entity", "description": "Description",
            "je_type": "Type", "risk_tag": "Risk tag", "total_debit": "Value",
            "status": "Status", "preparer": "Preparer", "reviewer": "Reviewer",
            "controller": "Controller", "support_count": "Docs",
        }), height=280)

        ui.rule()
        selected = st.selectbox("Open entry", entries["je_ref"].tolist())
        je = journals.get(conn, selected)

        head, side = st.columns([2, 1])
        with head:
            st.markdown(f"### {je['je_ref']}")
            st.markdown(
                f"{ui.pill(je['status'], ui.status_tone(je['status']))} &nbsp;"
                f"{ui.pill(je['risk_tag'], 'serious' if je['risk_tag'] in journals.JUDGEMENTAL_TAGS else 'good')}"
                f" &nbsp;<span style='color:{ui.MUTED};font-size:.82rem'>{je['je_type']} · "
                f"{je['framework']} · {je['entity']} · {je['period']}</span>",
                unsafe_allow_html=True,
            )
            st.write(je["description"])
            lines = journals.lines(conn, selected)
            disp = lines.copy()
            disp["debit"] = disp["debit"].map(lambda v: ui.money(v, 2, dash_zero=True))
            disp["credit"] = disp["credit"].map(lambda v: ui.money(v, 2, dash_zero=True))
            ui.dataframe(disp.rename(columns={
                "line_no": "#", "account_no": "Account", "account_name": "Description",
                "cost_center": "Cost centre", "debit": "Debit", "credit": "Credit",
                "memo": "Memo"}))
            st.caption(f"Debits {ui.money(je['total_debit'], 2)} · "
                       f"Credits {ui.money(je['total_credit'], 2)}")

            issues = journals.validate(conn, selected)
            if issues:
                for i in issues:
                    st.warning(i)
            else:
                st.success("All control checks pass.")

        with side:
            st.markdown("#### Sign-off trail")
            for label, user, ts in [
                ("Prepared", je["preparer"], je["prepared_at"]),
                ("Reviewed", je["reviewer"], je["reviewed_at"]),
                ("Controller", je["controller"], je["approved_at"]),
                ("Posted", "—" if not je["posted_at"] else "posted", je["posted_at"]),
            ]:
                if user and user != "—":
                    name = config.USER_BY_NAME[user].display_name if user in config.USER_BY_NAME else user
                    st.markdown(
                        f"<span style='color:{ui.STATUS['good']}'>✓</span> **{label}** — {name}"
                        f"<br><span style='color:{ui.MUTED};font-size:.75rem'>{ts or ''}</span>",
                        unsafe_allow_html=True)
                else:
                    st.markdown(f"<span style='color:{ui.MUTED}'>○ {label} — outstanding</span>",
                                unsafe_allow_html=True)

            st.write("")
            st.markdown("#### Document vault")
            docs = journals.verify_documents(conn, selected)
            if docs.empty:
                st.caption("No supporting documents attached.")
            else:
                for r in docs.itertuples():
                    tone = "good" if r.integrity == "Intact" else "critical"
                    st.markdown(
                        f"{ui.pill(r.integrity, tone)} {r.filename}"
                        f"<br><span style='color:{ui.MUTED};font-size:.72rem'>"
                        f"sha256 {r.sha256[:16]}… · {r.size_bytes:,} bytes</span>",
                        unsafe_allow_html=True)
            upload = st.file_uploader("Attach support", key=f"up_{selected}",
                                      label_visibility="collapsed")
            if upload is not None and st.button("Attach", key=f"at_{selected}"):
                res = journals.attach_document(conn, selected, actor, upload.name,
                                               upload.getvalue())
                (st.success if res["ok"] else st.error)(res["message"])
                st.rerun()

        ui.rule()
        st.markdown("#### Actions")
        a1, a2, a3, a4 = st.columns(4)

        with a1:
            if st.button("Submit for review", width='stretch',
                         disabled=je["status"] != "Draft"):
                res = journals.submit(conn, selected, actor)
                (st.success if res["ok"] else st.error)(res["message"])
                st.rerun()

        with a2:
            can_review = je["status"] == "Pending Review"
            if st.button("Review & approve", width='stretch', disabled=not can_review):
                res = journals.review(conn, selected, actor, "Approve")
                (st.success if res["ok"] else st.error)(res["message"])
                st.rerun()

        with a3:
            if st.button("Reject", width='stretch', disabled=not can_review):
                st.session_state["reject_target"] = selected

        with a4:
            if st.button("Post", width='stretch', type="primary",
                         disabled=je["status"] != "Approved"):
                res = journals.post(conn, selected, actor)
                (st.success if res["ok"] else st.error)(res["message"])
                st.rerun()

        if je["reviewer"] and not je["controller"] and \
                max(je["total_debit"], je["total_credit"]) >= config.JE_ESCALATION_THRESHOLD:
            st.warning(
                f"Value is at or above the {config.JE_ESCALATION_THRESHOLD:,.0f} escalation "
                "threshold — a third, Controller-level signature is required before posting."
            )
            if st.button("Controller approve", disabled=role != "Controller"):
                res = journals.controller_approve(conn, selected, actor)
                (st.success if res["ok"] else st.error)(res["message"])
                st.rerun()

        if st.session_state.get("reject_target") == selected:
            with st.form("reject_form"):
                reason = st.text_area("Reason for rejection",
                                      placeholder="What the preparer must change")
                if st.form_submit_button("Confirm rejection"):
                    res = journals.review(conn, selected, actor, "Reject", reason)
                    (st.success if res["ok"] else st.error)(res["message"])
                    st.session_state.pop("reject_target", None)
                    st.rerun()

        if je["status"] == "Posted":
            with st.expander("Reverse this entry"):
                ui.note("Posted entries are never edited. A reversal creates a new, "
                        "mirrored entry that points back at the original and goes "
                        "through the same dual sign-off.")
                reason = st.text_input("Reason for reversal", key=f"rv_{selected}")
                if st.button("Create reversal", key=f"rvb_{selected}"):
                    if not reason:
                        st.error("A reason is required.")
                    else:
                        res = journals.reverse(conn, selected, actor, period, reason)
                        (st.success if res["ok"] else st.error)(res["message"])
                        st.rerun()

# ==========================================================================
with tab_new:
    st.markdown("#### Raise a journal entry")
    ui.note(
        "The entry is validated before it can leave Draft: it must balance, every "
        "account must exist in the chart of accounts, the description must be "
        "substantive enough to stand as evidence, and a judgemental risk tag obliges "
        "you to attach supporting calculations."
    )
    st.write("")

    accounts = pd.read_sql_query(
        "SELECT account_no, description FROM accounts ORDER BY account_no", conn
    )
    account_options = accounts["account_no"].tolist()
    labels = dict(zip(accounts["account_no"], accounts["description"]))

    c1, c2, c3 = st.columns(3)
    je_entity = c1.selectbox("Entity", [e.code for e in config.ENTITIES],
                             format_func=lambda c: config.ENTITY_LABELS[c])
    je_type = c2.selectbox("Type", journals.JE_TYPES)
    risk_tag = c3.selectbox("Risk tag", config.JE_RISK_TAGS)
    c4, c5 = st.columns([3, 1])
    description = c4.text_input(
        "Description",
        placeholder="What, why and on what basis — at least 15 characters")
    framework = c5.selectbox("Framework", config.REPORTING_FRAMEWORKS)

    st.markdown("##### Lines")
    template = pd.DataFrame([
        {"account_no": account_options[0], "cost_center": f"CC-{je_entity}",
         "debit": 0.0, "credit": 0.0, "memo": ""},
        {"account_no": account_options[1], "cost_center": f"CC-{je_entity}",
         "debit": 0.0, "credit": 0.0, "memo": ""},
    ])
    edited = st.data_editor(
        template, num_rows="dynamic", width='stretch', key="je_lines",
        column_config={
            "account_no": st.column_config.SelectboxColumn(
                "Account", options=account_options, required=True),
            "cost_center": st.column_config.TextColumn("Cost centre"),
            "debit": st.column_config.NumberColumn("Debit", format="%.2f", min_value=0.0),
            "credit": st.column_config.NumberColumn("Credit", format="%.2f", min_value=0.0),
            "memo": st.column_config.TextColumn("Memo", width="large"),
        },
    )

    dr = float(pd.to_numeric(edited["debit"], errors="coerce").fillna(0).sum())
    cr = float(pd.to_numeric(edited["credit"], errors="coerce").fillna(0).sum())
    d1, d2, d3 = st.columns(3)
    d1.metric("Total debits", ui.money(dr, 2))
    d2.metric("Total credits", ui.money(cr, 2))
    d3.metric("Difference", ui.money(dr - cr, 2),
              delta="balanced" if abs(dr - cr) < 0.005 else "out of balance",
              delta_color="normal" if abs(dr - cr) < 0.005 else "inverse")

    if st.button("Create draft entry", type="primary"):
        rows = [r for r in edited.to_dict("records")
                if (r.get("debit") or 0) or (r.get("credit") or 0)]
        if len(rows) < 2:
            st.error("At least two lines carrying a value are required.")
        else:
            try:
                ref = journals.create(conn, actor, period, je_entity, description,
                                      je_type, risk_tag, config.GROUP_CURRENCY, rows,
                                      framework=framework)
                st.success(f"{ref} created as a Draft. Open it in the journal workspace "
                           "to attach support and submit it for review.")
            except ValueError as exc:
                st.error(str(exc))

# ==========================================================================
with tab_gov:
    st.markdown("#### Your review queue")
    queue = journals.review_queue(conn, actor)
    if queue.empty:
        st.success("Nothing awaiting your signature.")
    else:
        q = queue[["je_ref", "entity", "description", "risk_tag", "total_debit",
                   "preparer", "support_count"]].copy()
        q["total_debit"] = q["total_debit"].map(lambda v: ui.money(v, 2))
        ui.dataframe(q.rename(columns={
            "je_ref": "Ref", "entity": "Entity", "description": "Description",
            "risk_tag": "Risk tag", "total_debit": "Value", "preparer": "Preparer",
            "support_count": "Docs"}))

    ui.rule()
    st.markdown("#### Segregation of duties")
    ui.note(
        "Switch identity in the sidebar and try to review an entry you prepared. "
        "The system refuses and writes the attempt to the audit trail — an attempted "
        "self-review is itself evidence a reviewer will want to see."
    )
    sod = journals.sod_exceptions(conn)
    if sod.empty:
        st.success("No segregation-of-duties exceptions. Every approved or posted entry "
                   "carries two distinct signatures.")
    else:
        st.error(f"{len(sod)} exception(s) found.")
        ui.dataframe(sod)

    ui.rule()
    st.markdown("#### Entries by risk tag")
    entries_all = journals.list_entries(conn, period)
    if not entries_all.empty:
        by_tag = (entries_all.groupby("risk_tag", as_index=False)
                  .agg(entries=("je_ref", "count"), value=("total_debit", "sum")))
        ui.bar_h(by_tag, "risk_tag", "value", title="Value by risk tag",
                 tooltip=["risk_tag", "entries", "value"],
                 height=max(180, 30 * len(by_tag)))
