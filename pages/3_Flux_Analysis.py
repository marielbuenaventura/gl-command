"""Module 3 — Automated Flux & Variance Analysis Engine."""

from __future__ import annotations

import io

import pandas as pd
import streamlit as st

from glcmd import config, db
from glcmd.services import flux
from glcmd.ui import components as ui

conn = ui.page(
    "Flux & Variance Analysis",
    "📈",
    "Load the SAP trial balance, and the engine does the rest: period-over-period "
    "and budget-versus-actual movements, a two-dimensional materiality gate, and a "
    "commentary obligation that will not let the period close half-explained.",
)
ctx = ui.sidebar(conn, show_period=True, show_materiality=True)
actor, period, entity = ctx["actor"], ctx["period"], ctx["entity"]
policy = ctx["policy"]

tab_analysis, tab_commentary, tab_trend, tab_ingest = st.tabs(
    ["Flux analysis", "Commentary", "Account trend", "Load data"]
)

# ==========================================================================
with tab_analysis:
    c1, c2 = st.columns([1, 1])
    basis = c1.radio("Basis", ["PoP", "BvA"], horizontal=True,
                     format_func=lambda b: "Period over period" if b == "PoP"
                     else "Budget vs actual")
    statement = c2.radio("Statement", ["All", "BS", "P&L"], horizontal=True,
                         format_func=lambda s: {"All": "All accounts",
                                                "BS": "Balance sheet",
                                                "P&L": "Profit & loss"}[s])

    df = flux.analyse(conn, period, basis, entity, policy, statement)
    cov = flux.coverage(conn, period, basis, entity, policy)

    if df.empty:
        st.warning(f"No trial balance loaded for {period}. Use the *Load data* tab.")
    else:

        ui.tiles([
            ("Accounts analysed", f"{cov['accounts']:,}",
             f"{period} · {'all entities' if entity == 'All' else entity}", None),
            ("Flagged", f"{cov['flagged']}",
             f"Gate: {policy.pct_threshold:g}% {policy.rule} "
             f"{policy.abs_threshold:,.0f} {config.GROUP_CURRENCY}",
             "warning" if cov["flagged"] else "good"),
            ("Commentary coverage", f"{cov['coverage_pct']:.0f}%",
             f"{cov['outstanding']} account(s) still unexplained",
             "critical" if cov["outstanding"] else "good"),
            ("Gross flagged movement", ui.money(cov["gross_variance"]),
             "Absolute sum across flagged accounts", None),
        ])

        ui.rule()
        flagged = df[df["flagged"]].copy()

        if flagged.empty:
            st.success("No account breaches the materiality gate on this basis. "
                       "Tighten the thresholds in the sidebar to widen the net.")
        else:
            chart_df = flagged.head(18).copy()
            chart_df["label"] = chart_df["account_no"] + " " + chart_df["description"].fillna("")
            chart_df["label"] = chart_df["label"].str.slice(0, 46)
            ui.bar_diverging(
                chart_df, "label", "variance",
                title=f"Largest movements — {'period over period' if basis == 'PoP' else 'budget vs actual'}",
                tooltip=["account_no", "description", "entity", "current", "comparative",
                         "variance", "variance_pct", "gate"],
                height=max(260, 26 * len(chart_df)),
            )
            st.caption("Blue is an increase against the comparative, red a decrease. "
                       "Sign follows the trial balance convention: debits positive, "
                       "credits negative — so a red revenue bar is revenue *up*.")

        ui.rule()
        st.markdown("#### Detail")
        only_flagged = st.checkbox("Show flagged accounts only", value=True)
        view = flagged if only_flagged else df

        display = view[["entity", "account_no", "description", "statement", "current",
                        "comparative", "variance", "variance_pct", "gate", "explained",
                        "driver_category", "root_cause"]].copy()
        for col in ("current", "comparative", "variance"):
            display[col] = display[col].map(lambda v: ui.money(v, 0))
        display["variance_pct"] = view["variance_pct"].map(lambda v: ui.pct(v))
        display["explained"] = display["explained"].map({True: "✓", False: "—"})

        ui.dataframe(display.rename(columns={
            "entity": "Entity", "account_no": "Account", "description": "Description",
            "statement": "Stmt", "current": period,
            "comparative": "Comparative", "variance": "Variance",
            "variance_pct": "%", "gate": "Gate result", "explained": "Explained",
            "driver_category": "Driver", "root_cause": "Root cause",
        }), height=440)

        csv = view.to_csv(index=False).encode()
        st.download_button(f"Download the {basis} flux pack (CSV)", csv,
                           file_name=f"flux_{period}_{basis}.csv", mime="text/csv")

# ==========================================================================
with tab_commentary:
    st.markdown("#### Root-cause commentary")
    ui.note(
        "Every flagged account needs a substantive explanation before the period can "
        "be signed off. The engine rejects one-word answers — a reviewer cannot test "
        "'timing', only a statement of what moved, by how much and why."
    )
    st.write("")

    basis_c = st.radio("Basis", ["PoP", "BvA"], horizontal=True, key="comm_basis",
                       format_func=lambda b: "Period over period" if b == "PoP"
                       else "Budget vs actual")
    df_c = flux.analyse(conn, period, basis_c, entity, policy)
    if df_c.empty:
        st.warning("No data loaded for this period.")
    else:

        outstanding = df_c[df_c["outstanding"]]
        done = df_c[df_c["flagged"] & df_c["explained"]]

        st.markdown(f"**{len(outstanding)} outstanding · {len(done)} explained**")
        st.write("")

        if outstanding.empty:
            st.success("Every flagged account carries commentary. The flux pack is complete.")
        else:
            for r in outstanding.head(25).itertuples():
                with st.container(border=True):
                    c1, c2 = st.columns([1.4, 2])
                    with c1:
                        st.markdown(
                            f"**{r.account_no} — {r.description}**  \n"
                            f"<span style='color:{ui.MUTED};font-size:.8rem'>"
                            f"{r.entity} {r.entity_name} · {r.statement}</span>",
                            unsafe_allow_html=True)
                        st.markdown(
                            f"<span style='font-size:.85rem'>"
                            f"{period}: <b>{ui.money(r.current)}</b><br>"
                            f"Comparative: {ui.money(r.comparative)}<br>"
                            f"Movement: <b>{ui.money(r.variance)}</b> ({ui.pct(r.variance_pct)})<br>"
                            f"<span style='color:{ui.MUTED}'>{r.gate}</span></span>",
                            unsafe_allow_html=True)
                    with c2:
                        driver = st.selectbox("Driver", flux.DRIVER_CATEGORIES,
                                              key=f"dv_{r.entity}_{r.account_no}_{basis_c}")
                        text = st.text_area(
                            "Root cause", key=f"rc_{r.entity}_{r.account_no}_{basis_c}",
                            placeholder="What moved, by how much, why, and what it means for "
                                        "the following period. At least 20 characters.",
                            height=96)
                        action = st.text_input("Action", key=f"ac_{r.entity}_{r.account_no}_{basis_c}",
                                               placeholder="Optional — follow-up required")
                        if st.button("Record commentary",
                                     key=f"bt_{r.entity}_{r.account_no}_{basis_c}"):
                            res = flux.add_commentary(
                                conn, actor, period, r.entity, r.account_no, basis_c,
                                text, driver, action, float(r.variance), r.variance_pct)
                            (st.success if res["ok"] else st.error)(res["message"])
                            if res["ok"]:
                                st.rerun()

        if not done.empty:
            ui.rule()
            st.markdown("#### Recorded commentary")
            for r in done.itertuples():
                reviewed = bool(r.reviewed_by)
                with st.expander(
                    f"{r.account_no} — {r.description}  ·  {ui.money(r.variance)} "
                    f"({ui.pct(r.variance_pct)})" + ("  ·  reviewed" if reviewed else "")
                ):
                    st.markdown(f"**Driver:** {r.driver_category}")
                    st.write(r.root_cause)
                    if r.action:
                        st.markdown(f"**Action:** {r.action}")
                    st.caption(f"Author: {r.author}"
                               + (f" · Reviewed by: {r.reviewed_by}" if reviewed else
                                  " · not yet independently reviewed"))
                    if not reviewed:
                        if st.button("Review this commentary",
                                     key=f"rv_{r.entity}_{r.account_no}_{basis_c}"):
                            res = flux.review_commentary(conn, actor, period, r.entity,
                                                         r.account_no, basis_c)
                            (st.success if res["ok"] else st.error)(res["message"])
                            if res["ok"]:
                                st.rerun()

# ==========================================================================
with tab_trend:
    st.markdown("#### Account trend")
    accounts = db.query(conn, "SELECT account_no, description FROM accounts ORDER BY account_no")
    choice = st.selectbox(
        "Account", accounts["account_no"].tolist(),
        format_func=lambda a: f"{a} — {accounts.set_index('account_no').loc[a, 'description']}",
    )
    t = flux.trend(conn, choice, entity)
    if t.empty:
        st.info("No history for this account.")
    else:
        long = t.melt(id_vars="period", value_vars=["amount_group", "budget_group"],
                      var_name="series", value_name="amount")
        long["series"] = long["series"].map({"amount_group": "Actual",
                                             "budget_group": "Budget"})
        ui.line_series(long, "period", "amount", "series",
                       title=f"{choice} — actual against budget "
                             f"({config.GROUP_CURRENCY}, group)",
                       y_title=None, height=300)
        ui.dataframe(t.rename(columns={"period": "Period", "amount_group": "Actual",
                                       "budget_group": "Budget"}))

# ==========================================================================
with tab_ingest:
    st.markdown("#### Load a SAP trial balance")
    ui.note(
        "Expected columns: <code>period, entity, account_no, currency, amount_local</code>. "
        "Optional: <code>budget_local</code> or <code>budget_group</code>, and "
        "<code>amount_group</code> to override the translation. Group-currency amounts "
        "are otherwise derived from the period closing rate. The loader refuses unknown "
        "accounts or entities and warns when the trial balance does not net to nil."
    )
    st.write("")

    template = pd.DataFrame({
        "period": [period, period],
        "entity": ["1100", "1100"],
        "account_no": ["1010", "6010"],
        "currency": ["USD", "USD"],
        "amount_local": [4_200_000.00, -14_800_000.00],
        "budget_local": [4_050_000.00, -15_200_000.00],
    })
    st.download_button(
        "Download the import template (CSV)",
        template.to_csv(index=False).encode(),
        file_name="trial_balance_template.csv", mime="text/csv",
    )

    upload = st.file_uploader("Trial balance file", type=["csv", "xlsx", "xls"])
    source = st.text_input("Source system / transaction", value="SAP FAGLL03")
    if upload is not None:
        try:
            raw = flux.parse_upload(io.BytesIO(upload.getvalue()), upload.name)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not read the file: {exc}")
        else:
            st.markdown("##### Preview")
            ui.dataframe(raw.head(20))
            st.caption(f"{len(raw):,} row(s) in the file.")
            if st.button("Validate and load", type="primary"):
                res = flux.ingest_trial_balance(conn, actor, raw, source)
                (st.success if res["ok"] else st.error)(res["message"])
                if res["ok"]:
                    st.rerun()

    ui.rule()
    st.markdown("#### What is currently loaded")
    loaded = db.query(
        conn,
        "SELECT period, entity, COUNT(*) AS accounts, SUM(amount_group) AS net_group,"
        " MAX(source) AS source, MAX(loaded_at) AS loaded_at FROM trial_balance"
        " GROUP BY period, entity ORDER BY period DESC, entity",
    )
    if loaded.empty:
        st.info("No trial balance loaded yet.")
    else:
        loaded["balanced"] = loaded["net_group"].abs() < 1.0
        loaded["net_group"] = loaded["net_group"].map(lambda v: ui.money(v, 2))
        loaded["balanced"] = loaded["balanced"].map({True: "✓ nets to nil",
                                                     False: "✕ out of balance"})
        ui.dataframe(loaded.rename(columns={
            "period": "Period", "entity": "Entity", "accounts": "Accounts",
            "net_group": f"Net ({config.GROUP_CURRENCY})", "balanced": "Integrity check",
            "source": "Source", "loaded_at": "Loaded"}))
