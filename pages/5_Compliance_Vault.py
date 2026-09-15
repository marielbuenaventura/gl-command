"""Module 5 — Risk, Audit & Compliance Vault (IFRS & GAAP)."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from glcmd import config
from glcmd.services import compliance, journals
from glcmd.ui import components as ui

conn = ui.page(
    "Risk, Audit & Compliance Vault",
    "🛡️",
    "The standards that actually bite in a drilling and mining-services group: "
    "IAS 16 and IAS 36 on a rig fleet, IAS 2 on consumables that go obsolete, and "
    "IAS 37 on rehabilitation obligations that outlive the contract.",
)
ctx = ui.sidebar(conn, show_period=True)
actor, period, entity = ctx["actor"], ctx["period"], ctx["entity"]

scorecard = compliance.compliance_scorecard(conn, period)
attention = sum(1 for s in scorecard if s["status"] == "Attention")

st.markdown("#### Scorecard")
cols = st.columns(len(scorecard))
for col, item in zip(cols, scorecard):
    tone = {"Pass": "good", "Attention": "warning", "No data": "serious"}[item["status"]]
    with col:
        ui.tile(item["area"], item["status"], item["detail"], tone)

ui.rule()

tab_ppe, tab_inv, tab_prov, tab_ctrl = st.tabs(
    ["IAS 16 / 36 — PP&E", "IAS 2 — Inventory", "IAS 37 — Provisions", "Control library"]
)

# ==========================================================================
with tab_ppe:
    reg = compliance.ppe_register(conn, entity=entity)
    if reg.empty:
        st.info("The fixed asset register is empty.")
    else:
        ui.tiles([
            ("Assets", f"{len(reg)}",
             f"Cost {ui.money(float(reg['cost'].sum()))}", None),
            ("Net book value", ui.money(float(reg["nbv"].sum())),
             f"Accumulated depreciation {ui.money(float(reg['accum_dep'].sum()))}", None),
            ("Monthly charge", ui.money(float(reg["monthly_charge"].sum())),
             "Straight line and units of production", None),
            ("Impairment exposure", ui.money(float(reg["impairment_exposure"].sum())),
             f"{int((reg['impairment_status'] != 'No indicator').sum())} asset(s) with indicators",
             "critical" if reg["impairment_exposure"].sum() > 0 else "good"),
        ])

        st.write("")
        c1, c2 = st.columns([1, 1])
        with c1:
            by_class = (reg.groupby("asset_class", as_index=False)["nbv"].sum()
                        .sort_values("nbv", ascending=False))
            ui.bar_h(by_class, "asset_class", "nbv",
                     title=f"Net book value by asset class ({config.GROUP_CURRENCY})",
                     height=max(200, 30 * len(by_class)))
        with c2:
            by_cgu = (reg.groupby("cgu", as_index=False)
                      .agg(nbv=("nbv", "sum"), exposure=("impairment_exposure", "sum"))
                      .sort_values("nbv", ascending=False))
            ui.bar_h(by_cgu, "cgu", "nbv",
                     title="Net book value by cash-generating unit",
                     tooltip=["cgu", "nbv", "exposure"],
                     height=max(200, 30 * len(by_cgu)))

        ui.rule()
        st.markdown("#### Depreciation")
        ui.note(
            "The register recomputes the charge from cost, residual value and either the "
            "useful life or the units consumed, then compares it with the accumulated "
            "depreciation actually carried. A difference is the first thing an auditor "
            "tests on a fixed asset register — it usually means a life change was never "
            "applied, or a disposal was never processed."
        )
        st.write("")
        breaks = reg[~reg["dep_check"]]
        if breaks.empty:
            st.success("Accumulated depreciation agrees with the recomputed charge on "
                       "every asset.")
        else:
            st.warning(f"{len(breaks)} asset(s) where accumulated depreciation does not "
                       "agree with the recomputed position.")

        if st.button("Run the monthly depreciation charge"):
            res = compliance.run_depreciation(conn, actor, period, entity)
            if res["ok"]:
                st.success(res["message"])
                st.session_state["dep_lines"] = res["lines"]
            else:
                st.error(res["message"])

        if st.session_state.get("dep_lines"):
            pj = pd.DataFrame(st.session_state["dep_lines"])
            pj["debit"] = pj["debit"].map(lambda v: ui.money(v, 2, dash_zero=True))
            pj["credit"] = pj["credit"].map(lambda v: ui.money(v, 2, dash_zero=True))
            st.markdown("##### Proposed journal")
            ui.dataframe(pj.rename(columns={"account_no": "Account", "debit": "Debit",
                                            "credit": "Credit", "memo": "Memo"}))
            if st.button("Raise as a draft journal", key="dep_je"):
                ref = journals.create(
                    conn, actor, period,
                    entity if entity != "All" else config.ENTITIES[1].code,
                    f"Monthly depreciation charge for {period} per the fixed asset register",
                    "Standard", "Routine", config.GROUP_CURRENCY,
                    st.session_state["dep_lines"])
                st.success(f"{ref} created as a Draft.")
                st.session_state.pop("dep_lines", None)

        ui.rule()
        st.markdown("#### Register")
        disp = reg[["asset_no", "description", "entity", "asset_class", "cgu", "method",
                    "cost", "accum_dep", "accum_impairment", "nbv", "monthly_charge",
                    "status", "impairment_status", "impairment_exposure"]].copy()
        for col in ("cost", "accum_dep", "accum_impairment", "nbv", "monthly_charge",
                    "impairment_exposure"):
            disp[col] = disp[col].map(lambda v: ui.money(v, 0, dash_zero=(col == "impairment_exposure")))
        ui.dataframe(disp.rename(columns={
            "asset_no": "Asset", "description": "Description", "entity": "Entity",
            "asset_class": "Class", "cgu": "CGU", "method": "Method", "cost": "Cost",
            "accum_dep": "Acc. dep.", "accum_impairment": "Acc. impair.", "nbv": "NBV",
            "monthly_charge": "Monthly", "status": "Status",
            "impairment_status": "IAS 36", "impairment_exposure": "Exposure"}), height=430)

        ui.rule()
        st.markdown("#### Impairment assessment (IAS 36)")
        flagged = reg[reg["impairment_status"] != "No indicator"]
        if not flagged.empty:
            for r in flagged.itertuples():
                tone = "critical" if r.impairment_exposure > 0 else "warning"
                st.markdown(
                    f"{ui.pill(r.impairment_status, tone)} **{r.asset_no} — {r.description}**  \n"
                    f"<span style='color:{ui.INK_2};font-size:.86rem'>"
                    f"NBV {ui.money(r.nbv)} · recoverable amount "
                    f"{ui.money(r.recoverable_amt) if pd.notna(r.recoverable_amt) else 'not yet determined'}"
                    + (f" · loss to recognise {ui.money(r.impairment_exposure)}"
                       if r.impairment_exposure > 0 else "")
                    + f"<br>{r.indicator_notes or ''}</span>",
                    unsafe_allow_html=True)
                st.write("")

        with st.expander("Record an impairment assessment"):
            with st.form("imp_form"):
                asset = st.selectbox("Asset", reg["asset_no"].tolist(),
                                     format_func=lambda a: f"{a} — "
                                     f"{reg.set_index('asset_no').loc[a, 'description']}")
                nbv_now = float(reg.set_index("asset_no").loc[asset, "nbv"])
                st.caption(f"Current net book value: {ui.money(nbv_now, 2)}")
                indicator = st.selectbox("Indicator", compliance.IMPAIRMENT_INDICATORS)
                recoverable = st.number_input(
                    "Recoverable amount (higher of value in use and fair value less costs "
                    "of disposal)", value=nbv_now, step=10_000.0, format="%.2f")
                note_text = st.text_area("Basis of the assessment",
                                         placeholder="How the recoverable amount was "
                                                     "determined, and the key assumptions")
                if st.form_submit_button("Record assessment"):
                    res = compliance.record_impairment(conn, actor, asset, recoverable,
                                                       indicator, note_text)
                    (st.success if res["ok"] else st.error)(res["message"])
                    if res["ok"]:
                        st.rerun()

# ==========================================================================
with tab_inv:
    inv = compliance.inventory_valuation(conn, period, entity)
    if inv.empty:
        st.info(f"No inventory loaded for {period}.")
    else:
        ui.tiles([
            ("Inventory at cost", ui.money(float(inv["total_cost"].sum())),
             f"{len(inv)} line(s)", None),
            ("Carrying value", ui.money(float(inv["carrying_value"].sum())),
             "Lower of cost and NRV", None),
            ("Required provision", ui.money(float(inv["required_writedown"].sum())),
             f"{int(inv['below_cost'].sum())} line(s) below cost",
             "warning" if inv["required_writedown"].sum() else "good"),
            ("Movement to book", ui.money(float(inv["provision_movement"].sum())),
             "Against the provision already carried",
             "critical" if abs(inv["provision_movement"].sum()) > 0.01 else "good"),
        ])

        ui.note(
            "NRV is the estimated selling price in the ordinary course of business less "
            "the estimated costs of completion and the estimated costs necessary to make "
            "the sale. Where a line has no selling price — consumables used on the rig "
            "rather than sold — cost is the carrying value and the ageing profile is the "
            "obsolescence signal instead."
        )
        st.write("")

        c1, c2 = st.columns([1, 1])
        with c1:
            by_cat = (inv.groupby("category", as_index=False)
                      .agg(cost=("total_cost", "sum"), carrying=("carrying_value", "sum")))
            long = by_cat.melt(id_vars="category", value_vars=["cost", "carrying"],
                               var_name="measure", value_name="amount")
            long["measure"] = long["measure"].map({"cost": "At cost",
                                                   "carrying": "Carrying value"})
            ui.bar_grouped(long, "category", "amount", "measure",
                           title=f"Cost against carrying value ({config.GROUP_CURRENCY})",
                           height=260)
        with c2:
            wd = inv[inv["required_writedown"] > 0].copy()
            if wd.empty:
                st.caption("No line requires a write-down.")
            else:
                wd["label"] = wd["sku"] + " — " + wd["description"].str.slice(0, 32)
                ui.bar_h(wd, "label", "required_writedown",
                         title="Required write-down by line",
                         colour=ui.STATUS["critical"],
                         tooltip=["sku", "description", "total_cost", "total_nrv",
                                  "required_writedown"],
                         height=max(200, 30 * len(wd)))

        ui.rule()
        disp = inv[["entity", "sku", "description", "category", "quantity", "unit_cost",
                    "total_cost", "nrv_per_unit", "total_nrv", "carrying_value",
                    "required_writedown", "existing_provision", "provision_movement",
                    "movement_type", "ageing_days"]].copy()
        for col in ("total_cost", "total_nrv", "carrying_value", "required_writedown",
                    "existing_provision", "provision_movement"):
            disp[col] = disp[col].map(lambda v: ui.money(v, 0, dash_zero=True))
        disp["unit_cost"] = inv["unit_cost"].map(lambda v: f"{v:,.2f}")
        disp["nrv_per_unit"] = inv["nrv_per_unit"].map(lambda v: f"{v:,.2f}")
        ui.dataframe(disp.rename(columns={
            "entity": "Entity", "sku": "SKU", "description": "Description",
            "category": "Category", "quantity": "Qty", "unit_cost": "Unit cost",
            "total_cost": "Cost", "nrv_per_unit": "NRV/unit", "total_nrv": "NRV",
            "carrying_value": "Carrying", "required_writedown": "Required prov.",
            "existing_provision": "Existing prov.", "provision_movement": "Movement",
            "movement_type": "Treatment", "ageing_days": "Age (days)"}), height=430)

        ui.rule()
        st.markdown("#### Proposed journal")
        proposal = compliance.inventory_journal(conn, period, entity)
        if not proposal["lines"]:
            st.success("No provision movement required this period.")
        else:
            pj = pd.DataFrame(proposal["lines"])
            pj["debit"] = pj["debit"].map(lambda v: ui.money(v, 2, dash_zero=True))
            pj["credit"] = pj["credit"].map(lambda v: ui.money(v, 2, dash_zero=True))
            ui.dataframe(pj.rename(columns={"account_no": "Account", "debit": "Debit",
                                            "credit": "Credit", "memo": "Memo"}))
            c1, c2 = st.columns(2)
            if c1.button("Raise as a draft journal", key="inv_je"):
                ref = journals.create(
                    conn, actor, period,
                    entity if entity != "All" else config.ENTITIES[3].code,
                    f"Inventory measured at the lower of cost and net realisable value "
                    f"for {period} under IAS 2",
                    "Provision", "Estimate / Judgement", config.GROUP_CURRENCY,
                    proposal["lines"])
                st.success(f"{ref} created as a Draft. Attach the NRV workings before "
                           "submitting it — the judgemental risk tag requires support.")
            if c2.button("Apply the provision to the register", key="inv_apply"):
                res = compliance.apply_inventory_provision(conn, actor, period, entity)
                (st.success if res["ok"] else st.error)(res["message"])
                st.rerun()

# ==========================================================================
with tab_prov:
    prov = compliance.provisions(conn, period, entity)
    if prov.empty:
        st.info(f"No provisions recorded for {period}.")
    else:
        provided = prov[prov["provided"]]
        ui.tiles([
            ("Closing balance", ui.money(float(provided["closing"].sum())),
             f"{len(provided)} recognised provision(s)", None),
            ("Additions", ui.money(float(prov["additions"].sum())),
             f"Utilised {ui.money(float(prov['utilised'].sum()))}", None),
            ("Discount unwind", ui.money(float(prov["unwind_discount"].sum())),
             "To finance costs", None),
            ("Contingent — disclose only", f"{int(prov['disclosure_only'].sum())}",
             "Possible obligations, not provided", "serious"),
        ])

        ui.note(
            "A provision is recognised only where there is a present obligation from a "
            "past event, an outflow is probable, and the amount can be reliably "
            "estimated. Items marked <i>possible</i> are disclosed as contingent "
            "liabilities and carry no balance — the engine keeps them in the schedule so "
            "the disclosure is never forgotten."
        )
        st.write("")

        st.markdown("#### Roll-forward")
        roll = compliance.provision_rollforward(conn, period, entity)
        disp = roll.copy()
        for col in disp.columns[1:]:
            disp[col] = disp[col].map(lambda v: ui.money(v, 0, dash_zero=True))
        ui.dataframe(disp.rename(columns={
            "category": "Category", "opening": "Opening", "additions": "Additions",
            "utilised": "Utilised", "unused_reversed": "Unused reversed",
            "unwind_discount": "Unwind", "fx_movement": "FX", "closing": "Closing"}))

        st.write("")
        chart = roll[roll["category"] != "TOTAL"].copy()
        ui.bar_h(chart, "category", "closing",
                 title=f"Closing provision by category ({config.GROUP_CURRENCY})",
                 tooltip=["category", "opening", "additions", "utilised", "closing"],
                 height=max(200, 32 * len(chart)))

        ui.rule()
        st.markdown("#### Detail")
        for r in prov.itertuples():
            tone = "good" if r.provided else "serious"
            label = "Provided" if r.provided else "Contingent — disclosure only"
            with st.expander(f"{r.provision_ref} · {r.category} · {r.entity} — "
                             f"{ui.money(r.closing)}"):
                st.markdown(f"{ui.pill(label, tone)}", unsafe_allow_html=True)
                st.write(r.description)
                bridge = pd.DataFrame([
                    {"Movement": "Opening", "Amount": r.opening},
                    {"Movement": "Additions", "Amount": r.additions},
                    {"Movement": "Utilised", "Amount": -r.utilised},
                    {"Movement": "Unused amounts reversed", "Amount": -r.unused_reversed},
                    {"Movement": "Unwinding of discount", "Amount": r.unwind_discount},
                    {"Movement": "Foreign exchange", "Amount": r.fx_movement},
                    {"Movement": "Closing", "Amount": r.closing},
                ])
                bridge["Amount"] = bridge["Amount"].map(lambda v: ui.money(v, 0, dash_zero=True))
                ui.dataframe(bridge)
                st.caption(
                    f"Recognition basis: {r.recognition_basis}"
                    + (f" · discount rate {r.discount_rate:.2%}"
                       if pd.notna(r.discount_rate) else "")
                    + (f" · expected settlement {r.expected_settle}"
                       if r.expected_settle else "")
                )
                if r.evidence_missing:
                    st.error("No evidence on file. A provision without documented "
                             "support will not survive audit — attach the basis of "
                             "estimate before period end.")
                else:
                    st.success(f"Evidence: {r.evidence}")

        with st.expander("Record or update a provision"):
            with st.form("prov_form"):
                c1, c2, c3 = st.columns(3)
                p_entity = c1.selectbox("Entity", [e.code for e in config.ENTITIES],
                                        format_func=lambda c: config.ENTITY_LABELS[c])
                p_ref = c2.text_input("Reference", placeholder="PRV-REH-009")
                p_cat = c3.selectbox("Category", compliance.PROVISION_CATEGORIES)
                p_desc = st.text_input("Description")
                c4, c5, c6, c7 = st.columns(4)
                opening = c4.number_input("Opening", value=0.0, step=10_000.0, format="%.2f")
                additions = c5.number_input("Additions", value=0.0, step=10_000.0, format="%.2f")
                utilised = c6.number_input("Utilised", value=0.0, step=10_000.0, format="%.2f")
                unused = c7.number_input("Unused reversed", value=0.0, step=10_000.0,
                                         format="%.2f")
                c8, c9, c10 = st.columns(3)
                unwind = c8.number_input("Unwinding of discount", value=0.0, step=1_000.0,
                                         format="%.2f")
                fx_mv = c9.number_input("FX movement", value=0.0, step=1_000.0, format="%.2f")
                rate = c10.number_input("Discount rate", value=0.04, step=0.001,
                                        format="%.4f")
                c11, c12 = st.columns(2)
                settle = c11.text_input("Expected settlement", placeholder="2031-12-31")
                basis = c12.selectbox("Recognition basis", compliance.RECOGNITION_BASES)
                evidence = st.text_area("Evidence",
                                        placeholder="Permit, study, counsel opinion, "
                                                    "board approval — what supports the estimate")
                if st.form_submit_button("Save provision"):
                    if not p_ref:
                        st.error("A reference is required.")
                    else:
                        res = compliance.upsert_provision(
                            conn, actor, period, p_entity, p_ref, p_cat, p_desc,
                            opening, additions, utilised, unused, unwind, fx_mv,
                            rate, settle, basis, evidence)
                        st.success(res["message"])
                        st.rerun()

# ==========================================================================
with tab_ctrl:
    st.markdown("#### Control library")
    ui.note(
        "Every control here is referenced from the module that performs it, so testing "
        "a control means pointing at the evidence the system already produced rather "
        "than assembling a file at year end."
    )
    st.write("")

    ctrl = compliance.controls(conn)
    if ctrl.empty:
        st.info("No controls defined.")
    else:
        overdue = ctrl[ctrl["freshness"].str.startswith("Overdue")
                       | (ctrl["freshness"] == "Never tested")]
        failed = ctrl[ctrl["test_result"] == "Fail"]
        ui.tiles([
            ("Controls", f"{len(ctrl)}", "Across GL and ITGC", None),
            ("Current", f"{int((ctrl['freshness'] == 'Current').sum())}",
             "Tested within the frequency window", "good"),
            ("Overdue or untested", f"{len(overdue)}", "Refresh before period end",
             "warning" if len(overdue) else "good"),
            ("Failing", f"{len(failed)}", "Remediation plan required",
             "critical" if len(failed) else "good"),
        ])
        st.write("")

        disp = ctrl[["control_ref", "name", "standard", "frequency", "assertion",
                     "owner", "last_tested", "test_result", "freshness", "evidence"]]
        ui.dataframe(disp.rename(columns={
            "control_ref": "Ref", "name": "Control", "standard": "Standard",
            "frequency": "Frequency", "assertion": "Assertion", "owner": "Owner",
            "last_tested": "Last tested", "test_result": "Result",
            "freshness": "Status", "evidence": "Evidence"}), height=430)

        with st.expander("Record a control test"):
            with st.form("ctrl_form"):
                ref = st.selectbox("Control", ctrl["control_ref"].tolist(),
                                   format_func=lambda r: f"{r} — "
                                   f"{ctrl.set_index('control_ref').loc[r, 'name']}")
                result = st.radio("Result", ["Pass", "Fail", "Pass with exception"],
                                  horizontal=True)
                evidence = st.text_area("Evidence",
                                        placeholder="Population, sample, what was "
                                                    "inspected and where it is filed")
                if st.form_submit_button("Record test"):
                    res = compliance.test_control(conn, actor, ref, result, evidence)
                    st.success(res["message"])
                    st.rerun()
