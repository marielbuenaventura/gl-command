"""
Module 4a — Balance sheet, bank and sub-ledger reconciliations.

The tracker holds the GL balance and the sub-ledger/statement balance, plus the
open reconciling items.  The unexplained difference is computed, never typed:

    unexplained = GL − sub-ledger − Σ(open reconciling items)

A reconciliation is only "Reviewed" when the unexplained amount is inside
tolerance *and* a second person has signed it (GL-C01).
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

import pandas as pd

from .. import audit, config, db

RECON_STATUSES = ["Not started", "In progress", "Prepared", "Reviewed", "On hold"]
RECON_TYPES = ["Balance sheet", "Bank", "Sub-ledger"]
ITEM_CATEGORIES = ["Timing", "In transit", "Error", "Unidentified", "Disputed"]
RISK_RATINGS = ["Low", "Medium", "High"]

#: An account is considered reconciled when the unexplained residual is within
#: this tolerance (group currency). Anything above needs a note or a journal.
TOLERANCE = 1_000.0

#: Reconciling items older than this are a reportable ageing exception (GL-C09).
AGEING_LIMIT_DAYS = 30


def ensure_schedule(conn: sqlite3.Connection, period: str, actor: str = "system") -> int:
    """Create a reconciliation shell for every recon-required account/entity."""
    accounts = db.query(
        conn, "SELECT account_no, description, ifrs_standard FROM accounts WHERE recon_required = 1"
    )
    if accounts.empty:
        return 0
    created = 0
    with db.transaction(conn):
        for e in config.ENTITIES:
            for a in accounts.itertuples():
                exists = db.fetch_one(
                    conn,
                    "SELECT id FROM reconciliations WHERE period=? AND entity=? AND account_no=?",
                    (period, e.code, a.account_no),
                )
                if exists:
                    continue
                gl = db.scalar(
                    conn,
                    "SELECT amount_group FROM trial_balance WHERE period=? AND entity=? AND account_no=?",
                    (period, e.code, a.account_no), default=None,
                )
                if gl is None:
                    continue  # no balance on this account for this entity
                rtype = "Bank" if a.account_no.startswith("10") else "Balance sheet"
                risk = "High" if abs(float(gl)) > config.PERFORMANCE_MATERIALITY else "Medium"
                conn.execute(
                    "INSERT INTO reconciliations(period, entity, account_no, recon_type,"
                    " gl_balance, subledger_balance, status, risk_rating)"
                    " VALUES (?,?,?,?,?,0,'Not started',?)",
                    (period, e.code, a.account_no, rtype, float(gl), risk),
                )
                created += 1
        if created:
            audit.record(conn, actor, "recon.schedule.create", "reconciliation", period,
                         {"created": created}, control_ref="GL-C01", commit=False)
    return created


def load(conn: sqlite3.Connection, period: str, entity: str | None = None,
         status: str | None = None) -> pd.DataFrame:
    sql = ("SELECT r.*, a.description AS account_name, a.ifrs_standard FROM reconciliations r"
           " LEFT JOIN accounts a ON a.account_no = r.account_no WHERE r.period = ?")
    params: list = [period]
    if entity and entity != "All":
        sql += " AND r.entity = ?"
        params.append(entity)
    if status and status != "All":
        sql += " AND r.status = ?"
        params.append(status)
    df = db.query(conn, sql + " ORDER BY r.entity, r.account_no", params)
    if df.empty:
        return df

    items = db.query(
        conn,
        "SELECT recon_id, SUM(amount) AS open_items, COUNT(*) AS item_count"
        " FROM recon_items WHERE status = 'Open' GROUP BY recon_id",
    )
    df = df.merge(items, left_on="id", right_on="recon_id", how="left")
    df["open_items"] = df["open_items"].fillna(0.0)
    df["item_count"] = df["item_count"].fillna(0).astype(int)
    df["unexplained"] = (df["gl_balance"] - df["subledger_balance"] - df["open_items"]).round(2)
    df["within_tolerance"] = df["unexplained"].abs() <= TOLERANCE
    df["entity_name"] = df["entity"].map({e.code: e.name for e in config.ENTITIES})
    return df


def auto_match(conn: sqlite3.Connection, period: str, actor: str) -> dict:
    """Pull the GL side straight from the trial balance for every reconciliation.

    This is the sub-ledger-to-GL match: where the sub-ledger figure has been
    loaded and equals the GL within tolerance, the recon is auto-advanced to
    'Prepared' with a system note. Differences are left for a human.
    """
    recs = db.query(conn, "SELECT * FROM reconciliations WHERE period = ?", (period,))
    if recs.empty:
        return {"matched": 0, "refreshed": 0, "differences": 0}

    matched = refreshed = differences = 0
    now = datetime.now().isoformat(timespec="seconds")
    with db.transaction(conn):
        for r in recs.itertuples():
            gl = db.scalar(
                conn,
                "SELECT amount_group FROM trial_balance WHERE period=? AND entity=? AND account_no=?",
                (period, r.entity, r.account_no), default=None,
            )
            if gl is not None and round(float(gl), 2) != round(r.gl_balance, 2):
                conn.execute("UPDATE reconciliations SET gl_balance = ? WHERE id = ?",
                             (float(gl), r.id))
                refreshed += 1
            gl_val = float(gl) if gl is not None else r.gl_balance

            open_items = db.scalar(
                conn, "SELECT COALESCE(SUM(amount),0) FROM recon_items WHERE recon_id=? AND status='Open'",
                (r.id,),
            )
            residual = round(gl_val - r.subledger_balance - float(open_items), 2)
            if r.subledger_balance == 0 and gl_val != 0:
                continue  # sub-ledger not yet loaded — nothing to match against
            if abs(residual) <= TOLERANCE:
                if r.status in ("Not started", "In progress"):
                    conn.execute(
                        "UPDATE reconciliations SET status='Prepared', preparer=?, prepared_at=?,"
                        " notes=COALESCE(notes,'') || ? WHERE id = ?",
                        (actor, now,
                         f"[auto-match {now}] sub-ledger agrees to GL within tolerance "
                         f"(residual {residual:,.2f}). ", r.id),
                    )
                    matched += 1
            else:
                differences += 1
        audit.record(conn, actor, "recon.automatch", "reconciliation", period,
                     {"matched": matched, "refreshed": refreshed, "differences": differences},
                     control_ref="GL-C01", commit=False)
    return {"matched": matched, "refreshed": refreshed, "differences": differences}


def set_subledger(conn: sqlite3.Connection, recon_id: int, amount: float, actor: str) -> None:
    with db.transaction(conn):
        conn.execute("UPDATE reconciliations SET subledger_balance = ? WHERE id = ?",
                     (float(amount), recon_id))
        audit.record(conn, actor, "recon.subledger.set", "reconciliation", recon_id,
                     {"subledger_balance": float(amount)}, control_ref="GL-C01", commit=False)


def add_item(conn: sqlite3.Connection, recon_id: int, actor: str, description: str,
             amount: float, category: str, item_date: str | None = None,
             item_ref: str | None = None) -> int:
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO recon_items(recon_id, item_ref, description, amount, item_date, category)"
            " VALUES (?,?,?,?,?,?)",
            (recon_id, item_ref, description, float(amount),
             item_date or date.today().isoformat(), category),
        )
        new_id = int(cur.lastrowid)
        audit.record(conn, actor, "recon.item.add", "reconciliation", recon_id,
                     {"item_id": new_id, "amount": float(amount), "category": category,
                      "description": description},
                     control_ref="GL-C01", commit=False)
    return new_id


def resolve_item(conn: sqlite3.Connection, item_id: int, actor: str, resolution: str) -> None:
    with db.transaction(conn):
        conn.execute("UPDATE recon_items SET status='Cleared', resolution=? WHERE id = ?",
                     (resolution, item_id))
        audit.record(conn, actor, "recon.item.clear", "recon_item", item_id,
                     {"resolution": resolution}, control_ref="GL-C01", commit=False)


def items(conn: sqlite3.Connection, recon_id: int) -> pd.DataFrame:
    df = db.query(conn, "SELECT * FROM recon_items WHERE recon_id = ? ORDER BY item_date", (recon_id,))
    if df.empty:
        return df
    today = date.today()
    df["age_days"] = df["item_date"].apply(
        lambda d: (today - date.fromisoformat(str(d))).days if d else 0
    )
    df["aged"] = (df["age_days"] > AGEING_LIMIT_DAYS) & (df["status"] == "Open")
    return df


def prepare(conn: sqlite3.Connection, recon_id: int, actor: str, notes: str | None = None) -> dict:
    now = datetime.now().isoformat(timespec="seconds")
    with db.transaction(conn):
        conn.execute(
            "UPDATE reconciliations SET status='Prepared', preparer=?, prepared_at=?,"
            " notes=COALESCE(?, notes) WHERE id=?",
            (actor, now, notes, recon_id),
        )
        audit.record(conn, actor, "recon.prepare", "reconciliation", recon_id,
                     {"notes": notes}, control_ref="GL-C01", commit=False)
    return {"ok": True, "message": "Reconciliation marked as prepared."}


def review(conn: sqlite3.Connection, recon_id: int, reviewer: str) -> dict:
    """Independent review — the GL-C01 second signature."""
    row = db.fetch_one(conn, "SELECT * FROM reconciliations WHERE id = ?", (recon_id,))
    if row is None:
        return {"ok": False, "message": "Reconciliation not found."}
    if row["status"] != "Prepared":
        return {"ok": False, "message": f"Status is {row['status']} — it must be Prepared first."}
    if row["preparer"] == reviewer:
        audit.record(conn, reviewer, "recon.review.blocked_sod", "reconciliation", recon_id,
                     {"reason": "Preparer attempted self-review"}, control_ref="GL-C01")
        return {"ok": False,
                "message": "Segregation of duties: the preparer cannot review their own "
                           "reconciliation. The attempt has been logged."}

    open_items = db.scalar(
        conn, "SELECT COALESCE(SUM(amount),0) FROM recon_items WHERE recon_id=? AND status='Open'",
        (recon_id,),
    )
    residual = round(row["gl_balance"] - row["subledger_balance"] - float(open_items), 2)
    if abs(residual) > TOLERANCE:
        return {"ok": False,
                "message": f"Unexplained difference of {residual:,.2f} exceeds the "
                           f"{TOLERANCE:,.0f} tolerance. Post a journal or record the item first."}

    now = datetime.now().isoformat(timespec="seconds")
    with db.transaction(conn):
        conn.execute("UPDATE reconciliations SET status='Reviewed', reviewer=?, reviewed_at=?"
                     " WHERE id=?", (reviewer, now, recon_id))
        audit.record(conn, reviewer, "recon.review", "reconciliation", recon_id,
                     {"residual": residual, "preparer": row["preparer"]},
                     control_ref="GL-C01", commit=False)
    return {"ok": True, "message": "Reconciliation reviewed and signed off."}


def summary(conn: sqlite3.Connection, period: str, entity: str | None = None) -> dict:
    df = load(conn, period, entity)
    if df.empty:
        return {"total": 0, "reviewed": 0, "prepared": 0, "not_started": 0,
                "out_of_tolerance": 0, "pct_complete": 0.0, "high_risk_open": 0,
                "aged_items": 0, "aged_value": 0.0}

    aged = db.query(
        conn,
        "SELECT ri.*, r.period FROM recon_items ri JOIN reconciliations r ON r.id = ri.recon_id"
        " WHERE r.period = ? AND ri.status = 'Open'",
        (period,),
    )
    aged_count, aged_value = 0, 0.0
    if not aged.empty:
        today = date.today()
        aged["age"] = aged["item_date"].apply(
            lambda d: (today - date.fromisoformat(str(d))).days if d else 0
        )
        old = aged[aged["age"] > AGEING_LIMIT_DAYS]
        aged_count, aged_value = len(old), float(old["amount"].abs().sum())

    return {
        "total": len(df),
        "reviewed": int((df["status"] == "Reviewed").sum()),
        "prepared": int((df["status"] == "Prepared").sum()),
        "not_started": int((df["status"] == "Not started").sum()),
        "out_of_tolerance": int((~df["within_tolerance"]).sum()),
        "pct_complete": round(100 * (df["status"] == "Reviewed").sum() / len(df), 1),
        "high_risk_open": int(((df["risk_rating"] == "High") & (df["status"] != "Reviewed")).sum()),
        "aged_items": aged_count,
        "aged_value": aged_value,
    }
