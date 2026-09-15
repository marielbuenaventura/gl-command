"""
Module 3 — Automated flux & variance analysis engine.

Two analyses run off the same trial-balance store:

* **PoP** — period over period (this period vs the immediately prior period).
* **BvA** — budget vs actual for the period.

Both apply the two-dimensional materiality gate in
:class:`config.MaterialityPolicy`.  Every flagged line requires root-cause
commentary before the period can be signed off; :func:`coverage` measures how
much of that obligation has been discharged.
"""

from __future__ import annotations

import io
import sqlite3
from datetime import datetime
from typing import Any

import pandas as pd

from .. import audit, config, db

REQUIRED_TB_COLUMNS = ["period", "entity", "account_no", "currency", "amount_local"]

DRIVER_CATEGORIES = [
    "Volume / activity",
    "Price / rate",
    "FX translation",
    "Timing / cut-off",
    "Accrual release",
    "One-off / non-recurring",
    "Reclassification",
    "Error — corrected",
    "Error — outstanding",
]


# --------------------------------------------------------------- utilities
def prior_period(period: str) -> str:
    y, m = (int(x) for x in period.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def available_periods(conn: sqlite3.Connection) -> list[str]:
    df = db.query(conn, "SELECT DISTINCT period FROM trial_balance ORDER BY period DESC")
    return df["period"].tolist() if not df.empty else []


def closing_rate(conn: sqlite3.Connection, period: str, currency: str) -> float:
    if currency == config.GROUP_CURRENCY:
        return 1.0
    row = db.fetch_one(
        conn, "SELECT closing_rate FROM fx_rates WHERE period = ? AND currency = ?",
        (period, currency),
    )
    if row is None:
        raise ValueError(f"No closing rate loaded for {currency} in {period}.")
    return float(row["closing_rate"])


# ------------------------------------------------------------- TB ingestion
def ingest_trial_balance(
    conn: sqlite3.Connection,
    actor: str,
    df: pd.DataFrame,
    source: str = "SAP FAGLL03",
) -> dict:
    """Validate and load a SAP trial-balance extract.

    Expected columns: ``period, entity, account_no, currency, amount_local``
    and optionally ``budget_local`` / ``budget_group``.  Group-currency amounts
    are derived from the period closing rate unless supplied.
    """
    df = df.copy()
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    missing = [c for c in REQUIRED_TB_COLUMNS if c not in df.columns]
    if missing:
        return {"ok": False, "message": f"Missing column(s): {', '.join(missing)}", "rows": 0}

    df["account_no"] = df["account_no"].astype(str).str.strip()
    df["entity"] = df["entity"].astype(str).str.strip()
    df["period"] = df["period"].astype(str).str.strip()
    df["currency"] = df["currency"].astype(str).str.strip().str.upper()
    df["amount_local"] = pd.to_numeric(df["amount_local"], errors="coerce")

    errors: list[str] = []
    if df["amount_local"].isna().any():
        errors.append(f"{int(df['amount_local'].isna().sum())} row(s) have a non-numeric amount.")

    known_accounts = set(db.query(conn, "SELECT account_no FROM accounts")["account_no"])
    unknown = sorted(set(df["account_no"]) - known_accounts)
    if unknown:
        errors.append(f"{len(unknown)} account(s) not in the chart of accounts "
                      f"(e.g. {', '.join(unknown[:5])}).")

    unknown_entities = sorted(set(df["entity"]) - set(config.ENTITY_BY_CODE))
    if unknown_entities:
        errors.append(f"Unknown entity code(s): {', '.join(unknown_entities)}.")

    if errors:
        return {"ok": False, "message": " ".join(errors), "rows": 0}

    # translate to group currency
    group_amounts = []
    for r in df.itertuples():
        if "amount_group" in df.columns and not pd.isna(getattr(r, "amount_group", None)):
            group_amounts.append(float(r.amount_group))
        else:
            rate = closing_rate(conn, r.period, r.currency)
            group_amounts.append(round(float(r.amount_local) / rate, 2))
    df["amount_group"] = group_amounts

    if "budget_group" not in df.columns:
        if "budget_local" in df.columns:
            df["budget_group"] = [
                round(float(b or 0) / closing_rate(conn, p, c), 2)
                for b, p, c in zip(df["budget_local"], df["period"], df["currency"])
            ]
        else:
            df["budget_group"] = 0.0

    # --- balance check per entity/period: a TB that does not sum to nil is not a TB
    check = df.groupby(["period", "entity"])["amount_group"].sum().round(2)
    out_of_balance = check[check.abs() > 1.0]

    now = datetime.now().isoformat(timespec="seconds")
    with db.transaction(conn):
        for r in df.itertuples():
            conn.execute(
                "INSERT INTO trial_balance(period, entity, account_no, currency,"
                " amount_local, amount_group, budget_group, source, loaded_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(period, entity, account_no) DO UPDATE SET"
                " amount_local = excluded.amount_local, amount_group = excluded.amount_group,"
                " budget_group = excluded.budget_group, source = excluded.source,"
                " loaded_at = excluded.loaded_at",
                (r.period, r.entity, r.account_no, r.currency, float(r.amount_local),
                 float(r.amount_group), float(r.budget_group), source, now),
            )
        audit.record(
            conn, actor, "flux.tb.ingest", "trial_balance",
            ",".join(sorted(df["period"].unique())),
            {"rows": len(df), "entities": sorted(df["entity"].unique()), "source": source,
             "out_of_balance": {k: float(v) for k, v in out_of_balance.items()}},
            control_ref="GL-C03", commit=False,
        )

    msg = f"Loaded {len(df)} row(s) from {source}."
    if not out_of_balance.empty:
        detail = "; ".join(f"{p}/{e}: {v:,.2f}" for (p, e), v in out_of_balance.items())
        msg += f" WARNING — trial balance does not net to nil for {detail}."
    return {"ok": True, "message": msg, "rows": len(df),
            "out_of_balance": out_of_balance.to_dict()}


def parse_upload(file_like: io.BytesIO | Any, filename: str) -> pd.DataFrame:
    if filename.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(file_like)
    return pd.read_csv(file_like)


# ------------------------------------------------------------ flux analysis
def _label_accounts(conn: sqlite3.Connection, df: pd.DataFrame) -> pd.DataFrame:
    acc = db.query(conn, "SELECT account_no, description, account_type, statement,"
                         " ifrs_standard, owner FROM accounts")
    return df.merge(acc, on="account_no", how="left")


def analyse(
    conn: sqlite3.Connection,
    period: str,
    basis: str = "PoP",
    entity: str | None = None,
    policy: config.MaterialityPolicy | None = None,
    statement: str | None = None,
) -> pd.DataFrame:
    """Return one row per account with the movement, the gate result and any commentary."""
    policy = policy or config.DEFAULT_MATERIALITY

    where = "WHERE period = ?"
    params: list[Any] = [period]
    if entity and entity != "All":
        where += " AND entity = ?"
        params.append(entity)

    current = db.query(
        conn,
        f"SELECT entity, account_no, amount_group AS current, budget_group AS budget"
        f" FROM trial_balance {where}",
        params,
    )
    if current.empty:
        return current

    if basis == "PoP":
        prior = period_comparative(conn, prior_period(period), entity)
        merged = current.merge(prior, on=["entity", "account_no"], how="outer")
        merged["current"] = merged["current"].fillna(0.0)
        merged["comparative"] = merged["comparative"].fillna(0.0)
        merged["budget"] = merged["budget"].fillna(0.0)
        merged["comparative_label"] = f"Prior period ({prior_period(period)})"
    elif basis == "BvA":
        merged = current.rename(columns={"budget": "comparative"}).copy()
        merged["comparative_label"] = "Budget"
    else:
        raise ValueError("basis must be 'PoP' or 'BvA'")

    merged["variance"] = (merged["current"] - merged["comparative"]).round(2)
    merged["variance_pct"] = [
        None if c == 0 else round(100.0 * v / abs(c), 1)
        for v, c in zip(merged["variance"], merged["comparative"])
    ]
    merged["flagged"] = [
        policy.breaches(p, v) for p, v in zip(merged["variance_pct"], merged["variance"])
    ]
    merged["gate"] = [
        _gate_reason(p, v, policy) for p, v in zip(merged["variance_pct"], merged["variance"])
    ]

    merged = _label_accounts(conn, merged)
    if statement and statement != "All":
        merged = merged[merged["statement"] == statement]

    commentary = db.query(
        conn,
        "SELECT entity, account_no, root_cause, driver_category, action, author, reviewed_by"
        " FROM flux_commentary WHERE period = ? AND basis = ?",
        (period, basis),
    )
    if not commentary.empty:
        merged = merged.merge(commentary, on=["entity", "account_no"], how="left")
    else:
        for c in ("root_cause", "driver_category", "action", "author", "reviewed_by"):
            merged[c] = None

    merged["explained"] = merged["root_cause"].notna() & (merged["root_cause"].astype(str) != "")
    merged["outstanding"] = merged["flagged"] & ~merged["explained"]
    merged["entity_name"] = merged["entity"].map(
        {e.code: e.name for e in config.ENTITIES}
    )
    merged["basis"] = basis
    merged["period"] = period

    return merged.sort_values("variance", key=lambda s: s.abs(), ascending=False).reset_index(drop=True)


def period_comparative(conn: sqlite3.Connection, period: str, entity: str | None) -> pd.DataFrame:
    where = "WHERE period = ?"
    params: list[Any] = [period]
    if entity and entity != "All":
        where += " AND entity = ?"
        params.append(entity)
    df = db.query(
        conn,
        f"SELECT entity, account_no, amount_group AS comparative FROM trial_balance {where}",
        params,
    )
    if df.empty:
        return pd.DataFrame(columns=["entity", "account_no", "comparative"])
    return df


def _gate_reason(pct: float | None, delta: float, policy: config.MaterialityPolicy) -> str:
    pct_hit = pct is not None and abs(pct) >= policy.pct_threshold
    abs_hit = abs(delta) >= policy.abs_threshold
    if policy.rule == "or":
        if pct_hit and abs_hit:
            return "Both thresholds breached"
        if pct_hit:
            return f"≥{policy.pct_threshold:g}% threshold"
        if abs_hit:
            return f"≥{policy.abs_threshold:,.0f} threshold"
        return "Within tolerance"
    if pct_hit and abs_hit:
        return f"≥{policy.pct_threshold:g}% and ≥{policy.abs_threshold:,.0f}"
    if pct_hit:
        return "% breached, value immaterial"
    if abs_hit:
        return "Value breached, % immaterial"
    return "Within tolerance"


# ---------------------------------------------------------------- commentary
def add_commentary(
    conn: sqlite3.Connection,
    actor: str,
    period: str,
    entity: str,
    account_no: str,
    basis: str,
    root_cause: str,
    driver_category: str,
    action: str | None = None,
    variance_amount: float | None = None,
    variance_pct: float | None = None,
) -> dict:
    if len(root_cause.strip()) < 20:
        return {"ok": False,
                "message": "Root-cause commentary must be substantive (at least 20 characters). "
                           "'Timing' on its own does not survive audit review."}
    now = datetime.now().isoformat(timespec="seconds")
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO flux_commentary(period, entity, account_no, basis, variance_amount,"
            " variance_pct, root_cause, driver_category, action, author, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(period, entity, account_no, basis) DO UPDATE SET"
            " root_cause = excluded.root_cause, driver_category = excluded.driver_category,"
            " action = excluded.action, author = excluded.author, created_at = excluded.created_at,"
            " variance_amount = excluded.variance_amount, variance_pct = excluded.variance_pct",
            (period, entity, account_no, basis, variance_amount, variance_pct,
             root_cause.strip(), driver_category, action, actor, now),
        )
        audit.record(
            conn, actor, "flux.commentary.add", "flux_commentary",
            f"{period}/{entity}/{account_no}/{basis}",
            {"driver": driver_category, "variance": variance_amount, "pct": variance_pct,
             "root_cause": root_cause.strip()[:300]},
            control_ref="GL-C03", commit=False,
        )
    return {"ok": True, "message": f"Commentary recorded for {account_no}."}


def review_commentary(conn: sqlite3.Connection, reviewer: str, period: str, entity: str,
                      account_no: str, basis: str) -> dict:
    row = db.fetch_one(
        conn,
        "SELECT author FROM flux_commentary WHERE period=? AND entity=? AND account_no=? AND basis=?",
        (period, entity, account_no, basis),
    )
    if row is None:
        return {"ok": False, "message": "No commentary to review."}
    if row["author"] == reviewer:
        return {"ok": False, "message": "Commentary must be reviewed by someone other than its author."}
    with db.transaction(conn):
        conn.execute(
            "UPDATE flux_commentary SET reviewed_by = ? WHERE period=? AND entity=?"
            " AND account_no=? AND basis=?",
            (reviewer, period, entity, account_no, basis),
        )
        audit.record(conn, reviewer, "flux.commentary.review", "flux_commentary",
                     f"{period}/{entity}/{account_no}/{basis}", {},
                     control_ref="GL-C03", commit=False)
    return {"ok": True, "message": "Commentary reviewed."}


# ------------------------------------------------------------------ summary
def coverage(conn: sqlite3.Connection, period: str, basis: str = "PoP",
             entity: str | None = None,
             policy: config.MaterialityPolicy | None = None) -> dict:
    df = analyse(conn, period, basis, entity, policy)
    if df.empty:
        return {"accounts": 0, "flagged": 0, "explained": 0, "coverage_pct": 100.0,
                "outstanding": 0, "gross_variance": 0.0, "largest": None}
    flagged = df[df["flagged"]]
    explained = int(flagged["explained"].sum())
    largest = None
    if not flagged.empty:
        top = flagged.iloc[0]
        largest = {"account_no": top["account_no"], "description": top.get("description"),
                   "variance": float(top["variance"]), "pct": top["variance_pct"]}
    return {
        "accounts": len(df),
        "flagged": len(flagged),
        "explained": explained,
        "outstanding": len(flagged) - explained,
        "coverage_pct": round(100 * explained / len(flagged), 1) if len(flagged) else 100.0,
        "gross_variance": float(flagged["variance"].abs().sum()),
        "largest": largest,
    }


def trend(conn: sqlite3.Connection, account_no: str, entity: str | None = None) -> pd.DataFrame:
    sql = ("SELECT period, entity, amount_group, budget_group FROM trial_balance"
           " WHERE account_no = ?")
    params: list[Any] = [account_no]
    if entity and entity != "All":
        sql += " AND entity = ?"
        params.append(entity)
    df = db.query(conn, sql + " ORDER BY period", params)
    if df.empty:
        return df
    return df.groupby("period", as_index=False)[["amount_group", "budget_group"]].sum()
