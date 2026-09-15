"""
GL-Command — command centre.

Entry point. Run with:  streamlit run app.py
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from glcmd import audit, config, db
from glcmd.services import close as close_svc
from glcmd.services import compliance, flux, fx, journals, recon, roadmap
from glcmd.ui import components as ui

conn = ui.page(
    "Command Centre",
    "🧭",
    "One screen for the four questions that decide the day: is the close on track, "
    "is anything sitting on my signature, does the balance sheet hold, and am I on "
    "plan against the two-year roadmap.",
)
ctx = ui.sidebar(conn, show_period=True)
actor, period, entity = ctx["actor"], ctx["period"], ctx["entity"]

# --------------------------------------------------------------------------
# Headline state
# --------------------------------------------------------------------------
prog = close_svc.progress(conn, period)
je_stats = journals.stats(conn, period)
recon_sum = recon.summary(conn, period, entity)
flux_cov = flux.coverage(conn, period, "PoP", entity)
phases = roadmap.phase_summary(conn)
ic = fx.decomposition(conn, period)

close_tone = "good" if prog["on_track"] else ("critical" if prog["blocked"] else "warning")
days_left = prog["days_remaining"]
days_text = (f"{days_left} day(s) to target" if days_left >= 0
             else f"{abs(days_left)} day(s) past target")

ui.tiles([
    ("Close progress", f"{prog['pct']:.0f}%",
     f"{prog['complete']} of {prog['total']} tasks · {days_text}", close_tone),
    ("On my signature", f"{je_stats['pending']}",
     f"{je_stats['draft']} draft · {je_stats['approved']} ready to post",
     "warning" if je_stats["pending"] else "good"),
    ("Reconciliations reviewed", f"{recon_sum['pct_complete']:.0f}%",
     f"{recon_sum['out_of_tolerance']} out of tolerance · "
     f"{recon_sum['high_risk_open']} high-risk open",
     "critical" if recon_sum["out_of_tolerance"] else "good"),
    ("Flux commentary", f"{flux_cov['coverage_pct']:.0f}%",
     f"{flux_cov['outstanding']} of {flux_cov['flagged']} flagged accounts unexplained",
     "warning" if flux_cov["outstanding"] else "good"),
])

st.write("")

ui.tiles([
    ("Intercompany break", ui.money(ic["gross"]),
     f"of which FX {ui.money(ic['fx'])} · timing {ui.money(ic['timing'])}",
     "critical" if abs(ic["mismatch"]) > 1 else "warning"),
    ("Roadmap progress",
     f"{phases['progress_pct'].mean():.0f}%" if not phases.empty else "—",
     f"{int(phases['overdue'].sum())} milestone(s) overdue" if not phases.empty else "",
     "warning" if not phases.empty and phases["overdue"].sum() else "good"),
    ("Journals posted", f"{je_stats['posted']}",
     f"{je_stats['judgemental']} judgemental · {je_stats['unsupported']} without support",
     "critical" if je_stats["unsupported"] else "good"),
    ("Aged reconciling items", f"{recon_sum['aged_items']}",
     f"{ui.money(recon_sum['aged_value'])} over {recon.AGEING_LIMIT_DAYS} days",
     "warning" if recon_sum["aged_items"] else "good"),
])

ui.rule()

# --------------------------------------------------------------------------
# What needs me today
# --------------------------------------------------------------------------
left, right = st.columns([1.15, 1])

with left:
    st.markdown("#### What needs you today")

    actions: list[dict] = []

    queue = journals.review_queue(conn, actor)
    if not queue.empty:
        actions.append({
            "Priority": "High",
            "Item": f"{len(queue)} journal(s) awaiting your independent review",
            "Detail": ", ".join(queue["je_ref"].head(4)),
            "Where": "Close & GL Operations",
        })

    blocked = prog["critical_path"]
    if not blocked.empty:
        overdue_tasks = blocked[blocked["due_date"].apply(lambda d: d < date.today())]
        if not overdue_tasks.empty:
            actions.append({
                "Priority": "High",
                "Item": f"{len(overdue_tasks)} close task(s) past their working day",
                "Detail": overdue_tasks.iloc[0]["task"][:70],
                "Where": "Close & GL Operations",
            })

    if flux_cov["outstanding"]:
        largest = flux_cov["largest"]
        actions.append({
            "Priority": "High" if flux_cov["outstanding"] > 3 else "Medium",
            "Item": f"{flux_cov['outstanding']} flagged account(s) without root-cause commentary",
            "Detail": (f"Largest: {largest['account_no']} {ui.money(largest['variance'])}"
                       if largest else ""),
            "Where": "Flux Analysis",
        })

    if recon_sum["out_of_tolerance"]:
        actions.append({
            "Priority": "High",
            "Item": f"{recon_sum['out_of_tolerance']} reconciliation(s) outside tolerance",
            "Detail": "Unexplained difference above the 1,000 threshold",
            "Where": "Reconciliations",
        })

    if abs(ic["mismatch"]) > 1:
        actions.append({
            "Priority": "High",
            "Item": "Intercompany true mismatch requires a journal, not an explanation",
            "Detail": f"{ui.money(ic['mismatch'])} disagrees in transaction currency",
            "Where": "Reconciliations → Intercompany",
        })

    ppe = compliance.ppe_register(conn, entity=entity)
    if not ppe.empty:
        exposure = float(ppe["impairment_exposure"].sum())
        if exposure > 0:
            asset = ppe[ppe["impairment_exposure"] > 0].iloc[0]
            actions.append({
                "Priority": "High",
                "Item": "Impairment loss to recognise under IAS 36",
                "Detail": f"{asset['asset_no']} {asset['description'][:40]} — "
                          f"{ui.money(exposure)}",
                "Where": "Compliance Vault",
            })
        indicators = ppe[ppe["impairment_status"] == "Indicator identified — test outstanding"]
        if not indicators.empty:
            actions.append({
                "Priority": "Medium",
                "Item": f"{len(indicators)} asset(s) with an impairment indicator not yet tested",
                "Detail": indicators.iloc[0]["description"][:60],
                "Where": "Compliance Vault",
            })

    inv = compliance.inventory_valuation(conn, period, entity)
    if not inv.empty:
        movement = float(inv["provision_movement"].sum())
        if abs(movement) > 0.01:
            actions.append({
                "Priority": "Medium",
                "Item": "Inventory NRV provision movement to book under IAS 2",
                "Detail": f"{ui.money(movement)} across "
                          f"{int((inv['provision_movement'].abs() > 0.01).sum())} line(s)",
                "Where": "Compliance Vault",
            })

    focus = roadmap.focus_list(conn, horizon_days=7)
    if not focus.empty:
        od = focus[focus["urgency"] == "Overdue"]
        if not od.empty:
            actions.append({
                "Priority": "Medium",
                "Item": f"{len(od)} roadmap milestone(s) overdue",
                "Detail": od.iloc[0]["objective"][:70],
                "Where": "Roadmap",
            })

    sod = journals.sod_exceptions(conn)
    if not sod.empty:
        actions.append({
            "Priority": "High",
            "Item": f"{len(sod)} entry(ies) carry a segregation-of-duties exception",
            "Detail": ", ".join(sod["je_ref"].head(3)),
            "Where": "Audit Trail",
        })

    if actions:
        order = {"High": 0, "Medium": 1, "Low": 2}
        adf = pd.DataFrame(actions).sort_values("Priority", key=lambda s: s.map(order))
        ui.dataframe(adf, height=min(60 + 36 * len(adf), 430))
    else:
        st.success("Nothing outstanding for this period. The close is clean.")

with right:
    st.markdown("#### Two-year roadmap")
    if not phases.empty:
        ui.progress_bar(phases, "phase_name", "progress_pct", height=230)
        current = phases[phases["gate"] != "Passed"]
        if not current.empty:
            row = current.iloc[0]
            st.markdown(
                f"**Current phase — {row['phase_name']} ({row['window']})**  \n"
                f"<span style='color:{ui.INK_2};font-size:.87rem'>{row['purpose']}<br>"
                f"{row['complete']} of {row['milestones']} milestones complete"
                + (f" · {row['overdue']} overdue" if row["overdue"] else "")
                + (f" · {row['blocked']} blocked" if row["blocked"] else "")
                + "</span>",
                unsafe_allow_html=True,
            )
        else:
            st.success("Every roadmap phase gate has been passed.")

ui.rule()

# --------------------------------------------------------------------------
# Close trajectory + compliance scorecard
# --------------------------------------------------------------------------
c1, c2 = st.columns([1, 1])

with c1:
    st.markdown("#### Close by working day")
    wd = close_svc.by_workday(conn, period)
    if not wd.empty:
        ui.progress_bar(wd, "workday", "pct", height=210)
        st.caption(
            f"Target is WD+{config.CLOSE_SLA_DAYS} ({prog['target_close']:%d %b}). "
            f"Expected completion by today is {prog.get('expected_pct', 0):.0f}%; "
            f"actual is {prog['pct']:.0f}%."
        )
    hist = close_svc.sla_history(conn)
    if not hist.empty:
        st.markdown("#### Close SLA — days against target")
        ui.bar_diverging(hist, "period", "days_variance",
                         tooltip=["period", "target_close", "actual_close", "days_variance"],
                         height=150, label_fmt="+,.0f")
        st.caption("Negative is early, positive is late. Blue bars sit inside the "
                   f"{config.CLOSE_SLA_DAYS}-day SLA.")

with c2:
    st.markdown("#### IFRS compliance scorecard")
    for item in compliance.compliance_scorecard(conn, period):
        tone = {"Pass": "good", "Attention": "warning", "No data": "serious"}[item["status"]]
        st.markdown(
            f"{ui.pill(item['status'], tone)} &nbsp;**{item['area']}**  \n"
            f"<span style='color:{ui.INK_2};font-size:.85rem'>{item['detail']}</span>",
            unsafe_allow_html=True,
        )
        st.write("")

ui.rule()

# --------------------------------------------------------------------------
# Footer
# --------------------------------------------------------------------------
integrity = audit.verify_chain(conn)
seeded = db.get_setting(conn, "seeded_at")
st.caption(
    f"{config.APP_NAME} {config.APP_VERSION} · group currency {config.GROUP_CURRENCY} · "
    f"frameworks {', '.join(config.REPORTING_FRAMEWORKS)} · "
    f"{integrity['checked']:,} audit entries "
    f"({'chain verified' if integrity['valid'] else 'CHAIN BROKEN'}) · "
    f"demo data loaded {seeded or 'n/a'}"
)
