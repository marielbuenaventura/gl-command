"""Module 1 — Timeline & Milestone Operating Engine."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from glcmd import config
from glcmd.services import roadmap
from glcmd.ui import components as ui

conn = ui.page(
    "Timeline & Milestone Operating Engine",
    "🗺️",
    "The two-year plan as an operating system rather than a document. Each phase "
    "carries a gate: it is not passed until every milestone in it is complete, and "
    "the engine will tell you when you are running ahead of a gate you have not earned.",
)
ctx = ui.sidebar(conn, show_period=False)
actor = ctx["actor"]

start = roadmap.start_date(conn)
elapsed = (date.today() - start).days
phases = roadmap.phase_summary(conn)
all_ms = roadmap.load(conn)

current_phase = phases[phases["gate"] != "Passed"]
current_name = current_phase.iloc[0]["phase_name"] if not current_phase.empty else "Complete"

ui.tiles([
    ("Day in role", f"{elapsed}", f"Started {start:%d %b %Y}", None),
    ("Current phase", current_name,
     current_phase.iloc[0]["window"] if not current_phase.empty else "All gates passed", None),
    ("Milestones complete", f"{int(all_ms['status'].eq('Complete').sum())} / {len(all_ms)}"
     if not all_ms.empty else "0 / 0",
     f"{all_ms['progress_pct'].mean():.0f}% weighted progress" if not all_ms.empty else "", None),
    ("Overdue", f"{int(all_ms['overdue'].sum())}" if not all_ms.empty else "0",
     f"{int(all_ms['status'].eq('Blocked').sum())} blocked" if not all_ms.empty else "",
     "critical" if not all_ms.empty and all_ms["overdue"].sum() else "good"),
])

ui.rule()

tab_now, tab_phases, tab_all, tab_admin = st.tabs(
    ["Focus", "Phase gates", "Full roadmap", "Plan settings"]
)

# --------------------------------------------------------------------------
with tab_now:
    st.markdown("#### The next fourteen days")
    focus = roadmap.focus_list(conn, horizon_days=14)
    if focus.empty:
        st.success("Nothing due in the next fortnight. Pull work forward from the next phase.")
    else:
        for urgency, tone in [("Overdue", "critical"), ("Due this week", "warning"),
                              ("Upcoming", "good")]:
            block = focus[focus["urgency"] == urgency]
            if block.empty:
                continue
            st.markdown(f"{ui.pill(f'{urgency} — {len(block)}', tone)}",
                        unsafe_allow_html=True)
            for r in block.itertuples():
                with st.container(border=True):
                    c1, c2 = st.columns([3, 1.1])
                    with c1:
                        st.markdown(
                            f"**{r.objective}**  \n"
                            f"<span style='color:{ui.MUTED};font-size:.8rem'>"
                            f"{config.PHASE_LABELS[r.phase]} · {r.workstream} · "
                            f"due {r.due_date:%d %b %Y}</span>  \n"
                            f"<span style='color:{ui.INK_2};font-size:.85rem'>"
                            f"Success measure: {r.success_measure}</span>",
                            unsafe_allow_html=True,
                        )
                    with c2:
                        new_status = st.selectbox(
                            "Status", roadmap.STATUSES,
                            index=roadmap.STATUSES.index(r.status),
                            key=f"st_{r.id}", label_visibility="collapsed",
                        )
                        new_pct = st.slider("Progress", 0, 100, int(r.progress_pct), 5,
                                            key=f"pc_{r.id}", label_visibility="collapsed")
                        if st.button("Save", key=f"sv_{r.id}", width='stretch'):
                            roadmap.update_milestone(conn, int(r.id), actor,
                                                     status=new_status, progress_pct=new_pct)
                            st.rerun()
            st.write("")

# --------------------------------------------------------------------------
with tab_phases:
    st.markdown("#### Phase gates")
    ui.note(
        "A gate is <b>Passed</b> only when every milestone inside it is complete. "
        "A phase showing progress while an earlier gate is still open is flagged — "
        "in a pioneer role the sequencing is the control: you cannot credibly automate "
        "a process you have not yet standardised."
    )
    st.write("")
    ui.progress_bar(phases, "phase_name", "progress_pct", height=250)

    display = phases.copy()
    display["Gate"] = display["gate"]
    display["Sequencing"] = display["sequence_warning"].map(
        {True: "⚠ running ahead of an open gate", False: ""}
    )
    ui.dataframe(
        display[["phase_name", "window", "milestones", "complete", "overdue",
                 "blocked", "progress_pct", "Gate", "Sequencing"]].rename(columns={
            "phase_name": "Phase", "window": "Window", "milestones": "Milestones",
            "complete": "Complete", "overdue": "Overdue", "blocked": "Blocked",
            "progress_pct": "Progress %",
        })
    )

    st.write("")
    for r in phases.itertuples():
        tone = {"Passed": "good", "Open": "warning", "Blocked": "critical"}[r.gate]
        with st.expander(f"{r.phase_name} — {r.window}  ·  {r.gate}", expanded=False):
            st.markdown(f"{ui.pill(r.gate, tone)} &nbsp; {r.purpose}", unsafe_allow_html=True)
            sub = all_ms[all_ms["phase"] == r.phase]
            ui.dataframe(
                sub[["workstream", "objective", "success_measure", "due_date",
                     "status", "progress_pct", "owner"]].rename(columns={
                    "workstream": "Workstream", "objective": "Objective",
                    "success_measure": "Success measure", "due_date": "Due",
                    "status": "Status", "progress_pct": "%", "owner": "Owner",
                })
            )

# --------------------------------------------------------------------------
with tab_all:
    st.markdown("#### Full roadmap")
    c1, c2, c3 = st.columns(3)
    f_phase = c1.selectbox("Phase", ["All"] + [p[0] for p in config.PHASES],
                           format_func=lambda c: "All phases" if c == "All"
                           else config.PHASE_LABELS[c])
    f_status = c2.selectbox("Status", ["All"] + roadmap.STATUSES)
    f_stream = c3.selectbox("Workstream", ["All"] + sorted(all_ms["workstream"].unique())
                            if not all_ms.empty else ["All"])

    view = all_ms.copy()
    if f_phase != "All":
        view = view[view["phase"] == f_phase]
    if f_status != "All":
        view = view[view["status"] == f_status]
    if f_stream != "All":
        view = view[view["workstream"] == f_stream]

    st.caption(f"{len(view)} milestone(s)")
    ui.dataframe(
        view[["phase_name", "workstream", "objective", "success_measure", "due_date",
              "days_to_due", "status", "progress_pct", "owner", "evidence"]].rename(columns={
            "phase_name": "Phase", "workstream": "Workstream", "objective": "Objective",
            "success_measure": "Success measure", "due_date": "Due",
            "days_to_due": "Days", "status": "Status", "progress_pct": "%",
            "owner": "Owner", "evidence": "Evidence",
        }),
        height=520,
    )

    if not view.empty:
        st.markdown("#### Progress by workstream")
        ws = (view.groupby("workstream", as_index=False)["progress_pct"].mean()
              .round(1).sort_values("progress_pct", ascending=False))
        ui.bar_h(ws, "workstream", "progress_pct", height=max(200, 26 * len(ws)),
                 tooltip=["workstream", "progress_pct"], label_fmt=".0f")

# --------------------------------------------------------------------------
with tab_admin:
    st.markdown("#### Plan settings")
    new_start = st.date_input("Start date in role", value=start)
    if st.button("Update start date"):
        roadmap.set_start_date(conn, new_start, actor)
        st.success("Start date updated — every milestone due date has moved with it.")
        st.rerun()

    ui.rule()
    st.markdown("#### Add a milestone")
    with st.form("add_ms"):
        c1, c2 = st.columns(2)
        phase = c1.selectbox("Phase", [p[0] for p in config.PHASES],
                             format_func=lambda c: config.PHASE_LABELS[c])
        workstream = c2.text_input("Workstream", placeholder="e.g. Automation")
        objective = st.text_input("Objective", placeholder="What will be true when this is done")
        measure = st.text_input("Success measure",
                                placeholder="How you will evidence it to a reviewer")
        c3, c4 = st.columns(2)
        due_day = c3.number_input("Due (days from start)", 1, 800, 90)
        owner = c4.selectbox("Owner", [u.username for u in config.USERS],
                             format_func=lambda u: config.USER_LABELS[u])
        if st.form_submit_button("Add milestone"):
            if not (objective and workstream):
                st.error("An objective and a workstream are required.")
            else:
                roadmap.add_milestone(conn, actor, phase, workstream, objective,
                                      measure, int(due_day), owner)
                st.success("Milestone added.")
                st.rerun()
