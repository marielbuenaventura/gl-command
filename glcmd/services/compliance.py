"""
Module 5 — Risk, Audit & Compliance Vault.

Three IFRS engines and a control library:

* **IAS 16 / IAS 36** — PP&E register with straight-line *and* units-of-production
  depreciation (the method that matters for rigs and mining plant), plus
  impairment indicator assessment and loss recognition against recoverable
  amount.
* **IAS 2** — inventory measured at the lower of cost and net realisable value,
  where NRV = estimated selling price − costs to complete − costs to sell.
  Write-downs and permitted reversals (IAS 2.33) are both computed.
* **IAS 37** — provision roll-forward with discount unwind, restricted to
  present obligations; contingent items are disclosed, not provided.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

import pandas as pd

from .. import audit, config, db

# --------------------------------------------------------------- IAS 16/36
ASSET_CLASSES = [
    "Drill rigs", "Heavy plant & machinery", "Mining equipment",
    "Motor vehicles", "Buildings & infrastructure", "IT & office equipment",
    "Right-of-use assets", "Capital work in progress",
]

IMPAIRMENT_INDICATORS = [
    "Asset idle or under-utilised",
    "Commodity price decline",
    "Contract loss or non-renewal",
    "Physical damage or obsolescence",
    "Mine closure or care & maintenance",
    "Market capitalisation below net assets",
    "Adverse change in the regulatory environment",
]


def months_between(d1: date, d2: date) -> int:
    return max(0, (d2.year - d1.year) * 12 + (d2.month - d1.month))


def ppe_register(conn: sqlite3.Connection, as_at: date | None = None,
                 entity: str | None = None) -> pd.DataFrame:
    """Register with computed depreciation charge, NBV and impairment position."""
    as_at = as_at or date.today()
    sql = "SELECT * FROM ppe_register"
    params: list = []
    if entity and entity != "All":
        sql += " WHERE entity = ?"
        params.append(entity)
    df = db.query(conn, sql + " ORDER BY entity, asset_class, asset_no", params)
    if df.empty:
        return df

    def num(value, default: float = 0.0) -> float:
        """SQLite NULLs arrive as NaN through pandas — coerce them once, here."""
        if value is None or pd.isna(value):
            return default
        return float(value)

    charges, nbvs, statuses, exposure = [], [], [], []
    for r in df.itertuples():
        depreciable = num(r.cost) - num(r.residual_value)
        total_units = num(r.total_units)
        if r.method == "Units of production" and total_units > 0:
            rate = depreciable / total_units
            units_done = num(r.units_to_date)
            accum_expected = round(rate * units_done, 2)
            elapsed = max(months_between(date.fromisoformat(r.acquisition_date), as_at), 1)
            monthly = round(rate * (units_done / elapsed), 2)
        else:
            life = int(num(r.useful_life_m)) or 1
            monthly = round(depreciable / life, 2)
            elapsed = months_between(date.fromisoformat(r.acquisition_date), as_at)
            accum_expected = round(min(monthly * elapsed, depreciable), 2)
        if depreciable <= 0:
            monthly, accum_expected = 0.0, 0.0

        nbv = round(num(r.cost) - num(r.accum_dep) - num(r.accum_impairment), 2)
        charges.append(monthly)
        nbvs.append(nbv)

        rec = r.recoverable_amt
        if rec is not None and not pd.isna(rec) and float(rec) < nbv:
            statuses.append("Impairment required")
            exposure.append(round(nbv - float(rec), 2))
        elif r.indicator_notes:
            statuses.append("Indicator identified — test outstanding")
            exposure.append(0.0)
        else:
            statuses.append("No indicator")
            exposure.append(0.0)

        # expose the register-vs-ledger check as its own column
        df.loc[df["asset_no"] == r.asset_no, "accum_dep_expected"] = accum_expected

    df["monthly_charge"] = charges
    df["nbv"] = nbvs
    df["impairment_status"] = statuses
    df["impairment_exposure"] = exposure
    df["dep_variance"] = (df["accum_dep"] - df["accum_dep_expected"]).round(2)
    df["dep_check"] = df["dep_variance"].abs() < 1.0
    df["entity_name"] = df["entity"].map({e.code: e.name for e in config.ENTITIES})
    df["fully_depreciated"] = df["nbv"] <= df["residual_value"] + 0.01
    return df


def record_impairment(conn: sqlite3.Connection, actor: str, asset_no: str,
                      recoverable_amt: float, indicator: str, note: str) -> dict:
    row = db.fetch_one(conn, "SELECT * FROM ppe_register WHERE asset_no = ?", (asset_no,))
    if row is None:
        return {"ok": False, "message": "Asset not found."}
    nbv = round(row["cost"] - row["accum_dep"] - row["accum_impairment"], 2)
    loss = round(max(0.0, nbv - float(recoverable_amt)), 2)
    now = datetime.now().isoformat(timespec="seconds")
    with db.transaction(conn):
        conn.execute(
            "UPDATE ppe_register SET recoverable_amt = ?, accum_impairment = accum_impairment + ?,"
            " indicator_notes = ?, last_reviewed = ? WHERE asset_no = ?",
            (float(recoverable_amt), loss, f"{indicator}: {note}", now, asset_no),
        )
        audit.record(
            conn, actor, "ias36.impairment.record", "ppe_asset", asset_no,
            {"nbv_before": nbv, "recoverable_amount": float(recoverable_amt),
             "impairment_loss": loss, "indicator": indicator, "note": note},
            control_ref="GL-C06", commit=False,
        )
    if loss == 0:
        return {"ok": True, "message": f"Recoverable amount exceeds carrying amount — "
                                       f"no impairment for {asset_no}. Assessment logged."}
    return {"ok": True, "message": f"Impairment loss of {loss:,.2f} recognised on {asset_no}. "
                                   "Raise the corresponding journal in the GL workspace.",
            "loss": loss}


def run_depreciation(conn: sqlite3.Connection, actor: str, period: str,
                     entity: str | None = None) -> dict:
    """Post the monthly charge into the register and propose the journal."""
    reg = ppe_register(conn, entity=entity)
    if reg.empty:
        return {"ok": False, "message": "No assets in the register.", "lines": []}
    active = reg[(reg["status"] == "In use") & (~reg["fully_depreciated"])]
    if active.empty:
        return {"ok": False, "message": "No depreciable assets.", "lines": []}

    total = round(float(active["monthly_charge"].sum()), 2)
    with db.transaction(conn):
        for r in active.itertuples():
            headroom = round(r.cost - r.residual_value - r.accum_dep, 2)
            charge = min(r.monthly_charge, max(headroom, 0.0))
            conn.execute("UPDATE ppe_register SET accum_dep = accum_dep + ? WHERE asset_no = ?",
                         (charge, r.asset_no))
        audit.record(conn, actor, "ias16.depreciation.run", "ppe_register", period,
                     {"assets": len(active), "charge": total, "entity": entity or "All"},
                     control_ref="GL-C05", commit=False)

    by_class = active.groupby("asset_class", as_index=False)["monthly_charge"].sum()
    lines = [{"account_no": "7200", "debit": round(float(r.monthly_charge), 2), "credit": 0.0,
              "memo": f"Depreciation — {r.asset_class}"} for r in by_class.itertuples()]
    lines.append({"account_no": "1650", "debit": 0.0, "credit": total,
                  "memo": f"Accumulated depreciation {period}"})
    return {"ok": True, "message": f"Depreciation of {total:,.2f} recorded across "
                                   f"{len(active)} asset(s).", "lines": lines, "total": total}


# ------------------------------------------------------------------- IAS 2
def inventory_valuation(conn: sqlite3.Connection, period: str,
                        entity: str | None = None) -> pd.DataFrame:
    """Lower of cost and NRV, item by item."""
    sql = "SELECT * FROM inventory_valuation WHERE period = ?"
    params: list = [period]
    if entity and entity != "All":
        sql += " AND entity = ?"
        params.append(entity)
    df = db.query(conn, sql + " ORDER BY entity, category, sku", params)
    if df.empty:
        return df

    df["total_cost"] = (df["quantity"] * df["unit_cost"]).round(2)
    df["nrv_per_unit"] = (df["selling_price"] - df["cost_to_complete"] - df["cost_to_sell"]).round(4)
    df["total_nrv"] = (df["quantity"] * df["nrv_per_unit"]).round(2)
    df["carrying_value"] = df[["total_cost", "total_nrv"]].min(axis=1).round(2)
    df["required_writedown"] = (df["total_cost"] - df["carrying_value"]).round(2)
    df["provision_movement"] = (df["required_writedown"] - df["existing_provision"]).round(2)
    df["movement_type"] = df["provision_movement"].apply(
        lambda v: "Write-down (IAS 2.34)" if v > 0.01
        else ("Reversal (IAS 2.33)" if v < -0.01 else "No movement")
    )
    df["below_cost"] = df["total_nrv"] < df["total_cost"]
    df["slow_moving"] = df["ageing_days"] >= 365
    df["entity_name"] = df["entity"].map({e.code: e.name for e in config.ENTITIES})
    return df.sort_values("required_writedown", ascending=False).reset_index(drop=True)


def inventory_journal(conn: sqlite3.Connection, period: str, entity: str | None = None) -> dict:
    df = inventory_valuation(conn, period, entity)
    if df.empty:
        return {"lines": [], "net": 0.0}
    net = round(float(df["provision_movement"].sum()), 2)
    if abs(net) < 0.01:
        return {"lines": [], "net": 0.0}
    if net > 0:
        lines = [
            {"account_no": "7150", "debit": net, "credit": 0.0,
             "memo": f"Inventory written down to NRV — {period} (IAS 2.34)"},
            {"account_no": "1390", "debit": 0.0, "credit": net,
             "memo": "Provision for inventory obsolescence"},
        ]
    else:
        lines = [
            {"account_no": "1390", "debit": -net, "credit": 0.0,
             "memo": "Reversal of inventory write-down (IAS 2.33)"},
            {"account_no": "7150", "debit": 0.0, "credit": -net,
             "memo": f"Credit to cost of sales — {period}"},
        ]
    return {"lines": lines, "net": net}


def apply_inventory_provision(conn: sqlite3.Connection, actor: str, period: str,
                              entity: str | None = None) -> dict:
    """Roll the computed write-down into ``existing_provision`` once journalised."""
    df = inventory_valuation(conn, period, entity)
    if df.empty:
        return {"ok": False, "message": "No inventory loaded for this period."}
    moved = df[df["provision_movement"].abs() > 0.01]
    with db.transaction(conn):
        for r in moved.itertuples():
            conn.execute(
                "UPDATE inventory_valuation SET existing_provision = ? WHERE id = ?",
                (float(r.required_writedown), int(r.id)),
            )
        audit.record(conn, actor, "ias2.nrv.apply", "inventory_valuation", period,
                     {"items": len(moved), "net_movement": round(float(moved["provision_movement"].sum()), 2)},
                     control_ref="GL-C07", commit=False)
    return {"ok": True, "message": f"NRV provision updated on {len(moved)} item(s)."}


# ------------------------------------------------------------------ IAS 37
PROVISION_CATEGORIES = [
    "Site rehabilitation / environmental",
    "Mine closure",
    "Warranty",
    "Legal claim",
    "Restructuring",
    "Onerous contract",
    "Decommissioning",
]

RECOGNITION_BASES = [
    "Present obligation — probable outflow, reliably estimable (provide)",
    "Possible obligation — disclose as contingent liability",
    "Remote — no provision, no disclosure",
]


def provisions(conn: sqlite3.Connection, period: str, entity: str | None = None) -> pd.DataFrame:
    sql = "SELECT * FROM provisions WHERE period = ?"
    params: list = [period]
    if entity and entity != "All":
        sql += " AND entity = ?"
        params.append(entity)
    df = db.query(conn, sql + " ORDER BY entity, category", params)
    if df.empty:
        return df
    df["closing"] = (
        df["opening"] + df["additions"] - df["utilised"] - df["unused_reversed"]
        + df["unwind_discount"] + df["fx_movement"]
    ).round(2)
    df["provided"] = df["recognition_basis"].str.startswith("Present obligation")
    df["disclosure_only"] = ~df["provided"]
    df["evidence_missing"] = df["evidence"].isna() | (df["evidence"].astype(str).str.len() < 5)
    df["entity_name"] = df["entity"].map({e.code: e.name for e in config.ENTITIES})
    return df


def provision_rollforward(conn: sqlite3.Connection, period: str,
                          entity: str | None = None) -> pd.DataFrame:
    df = provisions(conn, period, entity)
    if df.empty:
        return df
    cols = ["opening", "additions", "utilised", "unused_reversed",
            "unwind_discount", "fx_movement", "closing"]
    out = df.groupby("category", as_index=False)[cols].sum().round(2)
    total = out[cols].sum().to_dict()
    total["category"] = "TOTAL"
    return pd.concat([out, pd.DataFrame([total])], ignore_index=True)


def upsert_provision(conn: sqlite3.Connection, actor: str, period: str, entity: str,
                     provision_ref: str, category: str, description: str,
                     opening: float, additions: float, utilised: float,
                     unused_reversed: float, unwind_discount: float, fx_movement: float,
                     discount_rate: float | None, expected_settle: str | None,
                     recognition_basis: str, evidence: str | None) -> dict:
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO provisions(period, entity, provision_ref, category, description,"
            " opening, additions, utilised, unused_reversed, unwind_discount, fx_movement,"
            " discount_rate, expected_settle, recognition_basis, evidence)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(period, entity, provision_ref) DO UPDATE SET"
            " category=excluded.category, description=excluded.description,"
            " opening=excluded.opening, additions=excluded.additions,"
            " utilised=excluded.utilised, unused_reversed=excluded.unused_reversed,"
            " unwind_discount=excluded.unwind_discount, fx_movement=excluded.fx_movement,"
            " discount_rate=excluded.discount_rate, expected_settle=excluded.expected_settle,"
            " recognition_basis=excluded.recognition_basis, evidence=excluded.evidence",
            (period, entity, provision_ref, category, description, float(opening),
             float(additions), float(utilised), float(unused_reversed),
             float(unwind_discount), float(fx_movement),
             discount_rate, expected_settle, recognition_basis, evidence),
        )
        audit.record(conn, actor, "ias37.provision.upsert", "provision",
                     f"{period}/{entity}/{provision_ref}",
                     {"category": category, "additions": additions, "utilised": utilised,
                      "basis": recognition_basis},
                     control_ref="GL-C08", commit=False)
    return {"ok": True, "message": f"Provision {provision_ref} recorded."}


# ------------------------------------------------------------------ controls
def controls(conn: sqlite3.Connection) -> pd.DataFrame:
    df = db.query(conn, "SELECT * FROM controls ORDER BY control_ref")
    if df.empty:
        return df
    today = date.today()

    def freshness(row):
        last = row["last_tested"]
        if last is None or pd.isna(last) or not str(last).strip():
            return "Never tested"
        age = (today - date.fromisoformat(str(last))).days
        limit = {"Monthly": 35, "Quarterly": 100, "Per entry": 35, "Annual": 380}.get(
            row["frequency"], 100
        )
        return "Current" if age <= limit else f"Overdue ({age}d)"

    df["freshness"] = df.apply(freshness, axis=1)
    return df


def test_control(conn: sqlite3.Connection, actor: str, control_ref: str,
                 result: str, evidence: str) -> dict:
    today = date.today().isoformat()
    with db.transaction(conn):
        conn.execute(
            "UPDATE controls SET last_tested = ?, test_result = ?, evidence = ?, owner = ?"
            " WHERE control_ref = ?",
            (today, result, evidence, actor, control_ref),
        )
        audit.record(conn, actor, "control.test", "control", control_ref,
                     {"result": result, "evidence": evidence}, control_ref=control_ref,
                     commit=False)
    return {"ok": True, "message": f"{control_ref} tested — {result}."}


def compliance_scorecard(conn: sqlite3.Connection, period: str) -> list[dict]:
    """One line per IFRS area with a pass/attention verdict and the reason."""
    out: list[dict] = []

    ppe = ppe_register(conn)
    if ppe.empty:
        out.append({"area": "IAS 16 — PP&E", "status": "No data",
                    "detail": "The fixed asset register is empty."})
    else:
        needs = ppe[ppe["impairment_status"] != "No indicator"]
        exposure = float(ppe["impairment_exposure"].sum())
        dep_breaks = int((~ppe["dep_check"]).sum())
        detail = (f"{len(ppe)} asset(s), NBV {ppe['nbv'].sum():,.0f}. "
                  f"{len(needs)} with impairment indicators; exposure {exposure:,.0f}. "
                  f"{dep_breaks} asset(s) where accumulated depreciation disagrees with the "
                  f"recomputed charge.")
        out.append({"area": "IAS 16 / IAS 36 — PP&E and impairment",
                    "status": "Attention" if (exposure > 0 or dep_breaks) else "Pass",
                    "detail": detail})

    inv = inventory_valuation(conn, period)
    if inv.empty:
        out.append({"area": "IAS 2 — Inventory", "status": "No data",
                    "detail": "No inventory loaded for this period."})
    else:
        wd = float(inv["required_writedown"].sum())
        mv = float(inv["provision_movement"].sum())
        out.append({
            "area": "IAS 2 — Inventory at lower of cost and NRV",
            "status": "Attention" if abs(mv) > 0.01 else "Pass",
            "detail": (f"{int(inv['below_cost'].sum())} of {len(inv)} line(s) below cost. "
                       f"Required provision {wd:,.0f}; movement to book this period {mv:,.0f}. "
                       f"{int(inv['slow_moving'].sum())} line(s) aged over a year."),
        })

    prov = provisions(conn, period)
    if prov.empty:
        out.append({"area": "IAS 37 — Provisions", "status": "No data",
                    "detail": "No provisions recorded for this period."})
    else:
        missing = int(prov["evidence_missing"].sum())
        no_unwind = prov[(prov["discount_rate"].notna()) & (prov["unwind_discount"] == 0)]
        out.append({
            "area": "IAS 37 — Provisions and contingencies",
            "status": "Attention" if (missing or len(no_unwind)) else "Pass",
            "detail": (f"Closing balance {prov['closing'].sum():,.0f} across {len(prov)} "
                       f"provision(s); {int(prov['disclosure_only'].sum())} contingent "
                       f"(disclosure only). {missing} without evidence on file; "
                       f"{len(no_unwind)} discounted provision(s) with no unwind recorded."),
        })

    ctrl = controls(conn)
    if not ctrl.empty:
        overdue = int(ctrl["freshness"].str.startswith("Overdue").sum())
        never = int((ctrl["freshness"] == "Never tested").sum())
        failed = int((ctrl["test_result"] == "Fail").sum())
        out.append({
            "area": "Control library",
            "status": "Attention" if (overdue or never or failed) else "Pass",
            "detail": (f"{len(ctrl)} control(s): {failed} failing, {overdue} overdue, "
                       f"{never} never tested."),
        })
    return out
