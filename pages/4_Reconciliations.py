"""Module 4 — Complex Reconciliations & Intercurrency Matching Hub."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from glcmd import config, db
from glcmd.services import fx, journals, recon
from glcmd.ui import components as ui

conn = ui.page(
    "Reconciliations & Intercurrency Matching",
    "⚖️",
    "Sub-ledger-to-GL matching on one side, and on the other the question that eats "
    "a close week: how much of the intercompany break is timing, how much is currency, "
    "and how much is a genuine error that needs a journal.",
)
ctx = ui.sidebar(conn, show_period=True)
actor, period, entity = ctx["actor"], ctx["period"], ctx["entity"]

summary = recon.summary(conn, period, entity)
ic = fx.decomposition(conn, period)

ui.tiles([
    ("Reconciliations reviewed", f"{summary['pct_complete']:.0f}%",
     f"{summary['reviewed']} reviewed · {summary['prepared']} awaiting review · "
     f"{summary['not_started']} not started",
     "good" if summary["pct_complete"] > 95 else "warning"),
    ("Out of tolerance", f"{summary['out_of_tolerance']}",
     f"Tolerance {recon.TOLERANCE:,.0f} {config.GROUP_CURRENCY}",
     "critical" if summary["out_of_tolerance"] else "good"),
    ("Aged items", f"{summary['aged_items']}",
     f"{ui.money(summary['aged_value'])} over {recon.AGEING_LIMIT_DAYS} days",
     "warning" if summary["aged_items"] else "good"),
    ("Intercompany break", ui.money(ic["gross"]),
     f"{ic['documents']} document(s) · {ic['matched']} clean",
     "critical" if abs(ic["mismatch"]) > 1 else "warning"),
])

ui.rule()

tab_recon, tab_detail, tab_ic, tab_reval = st.tabs(
    ["Reconciliation tracker", "Work a reconciliation", "Intercompany matching",
     "IAS 21 revaluation"]
)

# ==========================================================================
with tab_recon:
    c1, c2 = st.columns([3, 1])
    with c1:
        st.markdown("#### Tracker")
    with c2:
        if st.button("Run auto-match", width='stretch'):
            res = recon.auto_match(conn, period, actor)
            st.success(
                f"GL refreshed on {res['refreshed']} account(s); {res['matched']} "
                f"auto-advanced to Prepared; {res['differences']} left with a difference "
                "for a human to work."
            )
            st.rerun()

    ui.note(
        "Auto-match pulls the GL side straight from the loaded trial balance, nets off "
        "the open reconciling items, and advances anything inside tolerance. It never "
        "signs anything off — the independent review is always a person, and never the "
        "person who prepared it."
    )
    st.write("")

    status_filter = st.selectbox("Status", ["All"] + recon.RECON_STATUSES)
    df = recon.load(conn, period, entity, status_filter)

    if df.empty:
        st.info("No reconciliations scheduled for this period.")
    else:
        chart = (df.groupby("status", as_index=False)
                 .agg(count=("id", "count")).sort_values("count", ascending=False))
        left, right = st.columns([1, 1.4])
        with left:
            ui.bar_h(chart, "status", "count", title="By status", height=190,
                     tooltip=["status", "count"])
        with right:
            risk = (df.groupby(["risk_rating"], as_index=False)
                    .agg(open_items=("item_count", "sum"),
                         unexplained=("unexplained", lambda s: float(s.abs().sum()))))
            ui.bar_h(risk, "risk_rating", "unexplained",
                     title=f"Unexplained difference by risk rating ({config.GROUP_CURRENCY})",
                     height=190, tooltip=["risk_rating", "open_items", "unexplained"])

        display = df[["entity", "account_no", "account_name", "recon_type", "risk_rating",
                      "gl_balance", "subledger_balance", "open_items", "unexplained",
                      "within_tolerance", "status", "preparer", "reviewer"]].copy()
        for col in ("gl_balance", "subledger_balance", "open_items", "unexplained"):
            display[col] = display[col].map(lambda v: ui.money(v, 2))
        display["within_tolerance"] = display["within_tolerance"].map({True: "✓", False: "✕"})
        ui.dataframe(display.rename(columns={
            "entity": "Entity", "account_no": "Account", "account_name": "Description",
            "recon_type": "Type", "risk_rating": "Risk", "gl_balance": "GL",
            "subledger_balance": "Sub-ledger", "open_items": "Open items",
            "unexplained": "Unexplained", "within_tolerance": "In tolerance",
            "status": "Status", "preparer": "Preparer", "reviewer": "Reviewer",
        }), height=430)

# ==========================================================================
with tab_detail:
    df = recon.load(conn, period, entity)
    if df.empty:
        st.info("Nothing to work on.")
    else:
        options = df["id"].tolist()
        chosen = st.selectbox(
            "Reconciliation", options,
            format_func=lambda i: (
                f"{df.set_index('id').loc[i, 'entity']} · "
                f"{df.set_index('id').loc[i, 'account_no']} "
                f"{df.set_index('id').loc[i, 'account_name']} · "
                f"{df.set_index('id').loc[i, 'status']}"
            ),
        )
        row = df.set_index("id").loc[chosen]

        c1, c2 = st.columns([1.3, 1])
        with c1:
            st.markdown(f"### {row['account_no']} — {row['account_name']}")
            st.markdown(
                f"{ui.pill(row['status'], ui.status_tone(row['status']))} &nbsp;"
                f"{ui.pill(row['risk_rating'] + ' risk', 'critical' if row['risk_rating'] == 'High' else 'warning')}"
                f" &nbsp;<span style='color:{ui.MUTED};font-size:.82rem'>"
                f"{row['recon_type']} · {row['entity']} {row['entity_name']} · "
                f"{row['ifrs_standard'] or ''}</span>",
                unsafe_allow_html=True)

            bridge = pd.DataFrame([
                {"Line": "General ledger balance", "Amount": row["gl_balance"]},
                {"Line": "Less: sub-ledger / statement balance", "Amount": -row["subledger_balance"]},
                {"Line": "Less: open reconciling items", "Amount": -row["open_items"]},
                {"Line": "Unexplained difference", "Amount": row["unexplained"]},
            ])
            bridge["Amount"] = bridge["Amount"].map(lambda v: ui.money(v, 2))
            ui.dataframe(bridge)

            if row["within_tolerance"]:
                st.success(f"Unexplained difference of {ui.money(row['unexplained'], 2)} "
                           f"is inside the {recon.TOLERANCE:,.0f} tolerance.")
            else:
                st.error(f"Unexplained difference of {ui.money(row['unexplained'], 2)} "
                         f"exceeds tolerance. Record the reconciling item or post a journal "
                         "before this can be reviewed.")

        with c2:
            st.markdown("#### Update the sub-ledger balance")
            new_sub = st.number_input("Sub-ledger / statement balance",
                                      value=float(row["subledger_balance"]), step=1000.0,
                                      format="%.2f")
            if st.button("Save balance"):
                recon.set_subledger(conn, int(chosen), new_sub, actor)
                st.rerun()

            st.markdown("#### Sign-off")
            if st.button("Mark as prepared", width='stretch'):
                recon.prepare(conn, int(chosen), actor)
                st.rerun()
            if st.button("Independent review", width='stretch', type="primary"):
                res = recon.review(conn, int(chosen), actor)
                (st.success if res["ok"] else st.error)(res["message"])
                if res["ok"]:
                    st.rerun()
            st.caption(f"Prepared by {row['preparer'] or '—'} · "
                       f"reviewed by {row['reviewer'] or '—'}")

        ui.rule()
        st.markdown("#### Reconciling items")
        items = recon.items(conn, int(chosen))
        if items.empty:
            st.caption("No reconciling items recorded.")
        else:
            disp = items[["item_ref", "description", "amount", "item_date", "age_days",
                          "category", "status", "aged", "resolution"]].copy()
            disp["amount"] = disp["amount"].map(lambda v: ui.money(v, 2))
            disp["aged"] = disp["aged"].map({True: f"⚠ over {recon.AGEING_LIMIT_DAYS}d",
                                             False: ""})
            ui.dataframe(disp.rename(columns={
                "item_ref": "Ref", "description": "Description", "amount": "Amount",
                "item_date": "Date", "age_days": "Age (days)", "category": "Category",
                "status": "Status", "aged": "Ageing", "resolution": "Resolution"}))

            open_items = items[items["status"] == "Open"]
            if not open_items.empty:
                cc1, cc2 = st.columns([1, 2])
                target = cc1.selectbox("Clear item", open_items["id"].tolist(),
                                       format_func=lambda i: f"#{i}")
                resolution = cc2.text_input("Resolution",
                                            placeholder="How the item cleared")
                if st.button("Clear item"):
                    recon.resolve_item(conn, int(target), actor,
                                       resolution or "Cleared in the following period.")
                    st.rerun()

        with st.expander("Add a reconciling item"):
            with st.form("add_item"):
                d1, d2 = st.columns(2)
                desc = d1.text_input("Description")
                amount = d2.number_input("Amount", value=0.0, step=1000.0, format="%.2f")
                d3, d4 = st.columns(2)
                category = d3.selectbox("Category", recon.ITEM_CATEGORIES)
                item_date = d4.date_input("Item date")
                if st.form_submit_button("Add item"):
                    recon.add_item(conn, int(chosen), actor, desc, amount, category,
                                   item_date.isoformat())
                    st.rerun()

# ==========================================================================
with tab_ic:
    st.markdown("#### Intercompany decomposition")
    ui.note(
        "Every intercompany document is matched to its mirror. Where the two sides agree "
        "in the transaction currency, any remaining difference is translation and is "
        "treated under <b>IAS 21.23(a)</b> — retranslate the monetary item at the closing "
        "rate and take the exchange difference to profit or loss (IAS 21.28). Where the "
        "sides disagree in the transaction currency itself, that is not currency: it is a "
        "break that needs a journal."
    )
    st.write("")

    if ic["documents"] == 0:
        st.info("No intercompany documents loaded for this period.")
    else:
        ui.tiles([
            ("Gross break", ui.money(ic["gross"]), "Sum of both sides, group currency", None),
            ("Timing", ui.money(ic["timing"]),
             "One side booked, the counterparty has not", "warning"),
            ("FX revaluation", ui.money(ic["fx"]),
             "Translation only — IAS 21, to P&L", "serious"),
            ("True mismatch", ui.money(ic["mismatch"]),
             "Disagrees in transaction currency — needs a journal",
             "critical" if abs(ic["mismatch"]) > 1 else "good"),
        ])

        st.write("")
        decomp = pd.DataFrame([
            {"component": "Timing difference", "amount": ic["timing"]},
            {"component": "FX revaluation (IAS 21)", "amount": ic["fx"]},
            {"component": "True mismatch", "amount": ic["mismatch"]},
        ])
        ui.bar_diverging(decomp, "component", "amount",
                         title=f"What the break is made of ({config.GROUP_CURRENCY})",
                         height=170, tooltip=["component", "amount"])

        ui.rule()
        st.markdown("#### By entity pair")
        pairs = ic["by_pair"].copy()
        if not pairs.empty:
            pairs["pair"] = pairs["entity"] + " ↔ " + pairs["counterparty"]
            disp = pairs[["pair", "gross_group_break", "timing", "fx_revaluation",
                          "true_mismatch", "residual"]].copy()
            for col in disp.columns[1:]:
                disp[col] = disp[col].map(lambda v: ui.money(v, 2))
            ui.dataframe(disp.rename(columns={
                "pair": "Entity pair", "gross_group_break": "Gross break",
                "timing": "Timing", "fx_revaluation": "FX", "true_mismatch": "Mismatch",
                "residual": "Residual"}))

        ui.rule()
        st.markdown("#### Document-level match")
        m = fx.match(conn, period)
        filt = st.multiselect("Classification",
                              sorted(m["classification"].unique()),
                              default=[c for c in m["classification"].unique()
                                       if c != "Matched"])
        view = m[m["classification"].isin(filt)] if filt else m
        disp = view[["doc_ref", "entity", "counterparty", "currency", "amount_txn",
                     "counter_txn", "txn_break", "gross_group_break", "timing",
                     "fx_revaluation", "true_mismatch", "classification", "detail"]].copy()
        for col in ("amount_txn", "counter_txn", "txn_break", "gross_group_break",
                    "timing", "fx_revaluation", "true_mismatch"):
            disp[col] = disp[col].map(lambda v: ui.money(v, 2, dash_zero=True))
        ui.dataframe(disp.rename(columns={
            "doc_ref": "Document", "entity": "Entity", "counterparty": "Counterparty",
            "currency": "Ccy", "amount_txn": "Amount", "counter_txn": "Counter amount",
            "txn_break": "Txn break", "gross_group_break": "Group break",
            "timing": "Timing", "fx_revaluation": "FX", "true_mismatch": "Mismatch",
            "classification": "Classification", "detail": "Basis"}), height=380)

        st.download_button("Download the intercompany match (CSV)",
                           m.to_csv(index=False).encode(),
                           file_name=f"intercompany_match_{period}.csv", mime="text/csv")

    with st.expander("Add an intercompany document"):
        with st.form("add_ic"):
            c1, c2, c3 = st.columns(3)
            doc_ref = c1.text_input("Document reference", placeholder="IC-4432")
            ent = c2.selectbox("Entity", [e.code for e in config.ENTITIES],
                               format_func=lambda c: config.ENTITY_LABELS[c])
            cp = c3.selectbox("Counterparty", [e.code for e in config.ENTITIES],
                              index=1, format_func=lambda c: config.ENTITY_LABELS[c])
            c4, c5, c6 = st.columns(3)
            account = c4.selectbox("Account", ["1450", "2100"])
            ccy = c5.selectbox("Currency", sorted({e.functional_ccy for e in config.ENTITIES}))
            amount = c6.number_input("Amount (transaction currency)", value=0.0,
                                     step=1000.0, format="%.2f")
            c7, c8 = st.columns(2)
            hist = c7.number_input("Rate at booking (units per USD)", value=1.0,
                                   step=0.001, format="%.4f")
            desc = c8.text_input("Description")
            if st.form_submit_button("Add document"):
                if not doc_ref:
                    st.error("A document reference is required.")
                else:
                    fx.add_document(conn, actor, period, doc_ref, ent, cp, account,
                                    ccy, amount, hist, description=desc)
                    st.success(f"{doc_ref} added.")
                    st.rerun()

# ==========================================================================
with tab_reval:
    st.markdown("#### IAS 21 revaluation schedule")
    ui.note(
        "Monetary items denominated in a foreign currency are retranslated at the "
        "closing rate at each reporting date. The unrealised gain or loss is the "
        "difference between the carrying amount in the ledger and the amount at the "
        "closing rate, and goes to profit or loss in the period it arises."
    )
    st.write("")

    rate_table = fx.rates(conn, period)
    if rate_table.empty:
        st.warning(f"No FX rates loaded for {period}.")
    else:
        st.markdown("##### Group rate table")
        rt = rate_table.copy()
        rt["closing_rate"] = rt["closing_rate"].map(lambda v: f"{v:,.4f}")
        rt["average_rate"] = rt["average_rate"].map(lambda v: f"{v:,.4f}")
        ui.dataframe(rt.rename(columns={"period": "Period", "currency": "Currency",
                                        "closing_rate": "Closing", "average_rate": "Average"}))
        st.caption(f"Rates are units of the foreign currency per 1 {config.GROUP_CURRENCY}.")

    sched = fx.revaluation_schedule(conn, period)
    if sched.empty:
        st.info("Nothing to retranslate.")
    else:
        ui.rule()
        disp = sched.copy()
        for col in ("amount_txn", "carrying_group", "at_closing_rate", "fx_gain_loss"):
            disp[col] = disp[col].map(lambda v: ui.money(v, 2))
        disp["historical_rate"] = sched["historical_rate"].map(lambda v: f"{v:,.4f}")
        disp["closing_rate"] = sched["closing_rate"].map(lambda v: f"{v:,.4f}")
        ui.dataframe(disp.rename(columns={
            "doc_ref": "Document", "entity": "Entity", "counterparty": "Counterparty",
            "account_no": "Account", "currency": "Ccy", "amount_txn": "Amount",
            "historical_rate": "Booking rate", "closing_rate": "Closing rate",
            "carrying_group": "Carrying", "at_closing_rate": "At closing",
            "fx_gain_loss": "Gain / (loss)", "treatment": "Treatment"}), height=340)

        net = float(sched["fx_gain_loss"].sum())
        st.markdown(f"**Net unrealised exchange difference: {ui.money(net, 2)} "
                    f"{config.GROUP_CURRENCY}**")

        ui.rule()
        st.markdown("#### Proposed revaluation journal")
        target_entity = st.selectbox(
            "Entity", ["All"] + sorted(sched["entity"].unique()),
            format_func=lambda c: "All entities" if c == "All" else config.ENTITY_LABELS[c],
            key="reval_ent")
        proposal = fx.revaluation_journal(conn, period, target_entity)
        if not proposal["lines"]:
            st.info("No revaluation journal required for this selection.")
        else:
            pj = pd.DataFrame(proposal["lines"])
            pj["debit"] = pj["debit"].map(lambda v: ui.money(v, 2, dash_zero=True))
            pj["credit"] = pj["credit"].map(lambda v: ui.money(v, 2, dash_zero=True))
            ui.dataframe(pj.rename(columns={"account_no": "Account", "debit": "Debit",
                                            "credit": "Credit", "memo": "Memo"}))
            if st.button("Raise this as a draft journal", type="primary"):
                ref = journals.create(
                    conn, actor, period,
                    target_entity if target_entity != "All" else config.ENTITIES[0].code,
                    f"IAS 21 retranslation of intercompany monetary balances at the "
                    f"{period} closing rate",
                    "FX revaluation", "FX revaluation", config.GROUP_CURRENCY,
                    proposal["lines"],
                )
                st.success(f"{ref} created as a Draft in the journal workspace. "
                           "It still needs support and an independent review.")
