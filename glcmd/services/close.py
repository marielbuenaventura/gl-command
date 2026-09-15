"""
Module 2a — Financial close calendar and checklist.

The engine is built around the 3-working-day close.  Every task is stamped with
the working day it belongs to (WD-2 pre-close through WD+3 sign-off), so slippage
is visible as *which day broke*, not merely "we were late".
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

import pandas as pd

from .. import audit, config, db

TASK_STATUSES = ["Not started", "In progress", "Complete", "Blocked", "N/A"]

WORKDAY_OFFSET = {"WD-2": -2, "WD-1": -1, "WD+1": 0, "WD+2": 1, "WD+3": 2}


# ----------------------------------------------------------------- calendar
def add_business_days(start: date, n: int) -> date:
    """Add ``n`` business days (Mon–Fri), ignoring public holidays."""
    step = 1 if n >= 0 else -1
    remaining = abs(n)
    cur = start
    while remaining:
        cur += timedelta(days=step)
        if cur.weekday() < 5:
            remaining -= 1
    return cur


def first_business_day(year: int, month: int) -> date:
    d = date(year, month, 1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def period_type(period: str) -> str:
    month = int(period.split("-")[1])
    if month == 12:
        return "Year"
    if month in (3, 6, 9):
        return "Quarter"
    return "Month"


def ensure_period(conn: sqlite3.Connection, period: str) -> sqlite3.Row:
    """Create the close period with WD+1 / WD+3 dates if it does not exist."""
    row = db.fetch_one(conn, "SELECT * FROM close_periods WHERE period = ?", (period,))
    if row:
        return row
    year, month = (int(x) for x in period.split("-"))
    nxt_y, nxt_m = (year + 1, 1) if month == 12 else (year, month + 1)
    wd1 = first_business_day(nxt_y, nxt_m)
    target = add_business_days(wd1, config.CLOSE_SLA_DAYS - 1)
    db.execute(
        conn,
        "INSERT INTO close_periods(period, period_type, close_start, target_close, status)"
        " VALUES (?,?,?,?, 'Open')",
        (period, period_type(period), wd1.isoformat(), target.isoformat()),
    )
    return db.fetch_one(conn, "SELECT * FROM close_periods WHERE period = ?", (period,))


def periods(conn: sqlite3.Connection) -> pd.DataFrame:
    return db.query(conn, "SELECT * FROM close_periods ORDER BY period DESC")


def workday_dates(conn: sqlite3.Connection, period: str) -> dict[str, date]:
    row = ensure_period(conn, period)
    wd1 = date.fromisoformat(row["close_start"])
    return {wd: add_business_days(wd1, off) for wd, off in WORKDAY_OFFSET.items()}


# -------------------------------------------------------------------- tasks
def tasks(conn: sqlite3.Connection, period: str, entity: str | None = None) -> pd.DataFrame:
    sql = "SELECT * FROM close_tasks WHERE period = ?"
    params: list = [period]
    if entity and entity != "All":
        sql += " AND (entity = ? OR entity IS NULL OR entity = 'All')"
        params.append(entity)
    df = db.query(conn, sql + " ORDER BY id", params)
    if df.empty:
        return df
    dates = workday_dates(conn, period)
    df["due_date"] = df["workday"].map(lambda w: dates.get(w))
    order = {w: i for i, w in enumerate(config.CLOSE_WORKDAYS)}
    df["_o"] = df["workday"].map(order).fillna(99)
    return df.sort_values(["_o", "stream", "id"]).drop(columns=["_o"])


def set_task_status(
    conn: sqlite3.Connection,
    task_id: int,
    status: str,
    actor: str,
    blocker: str | None = None,
) -> None:
    if status not in TASK_STATUSES:
        raise ValueError(f"Unknown status '{status}'")
    before = db.fetch_one(conn, "SELECT * FROM close_tasks WHERE id = ?", (task_id,))
    if before is None:
        raise ValueError(f"Close task {task_id} not found")

    completed_at = datetime.now().isoformat(timespec="seconds") if status == "Complete" else None
    with db.transaction(conn):
        conn.execute(
            "UPDATE close_tasks SET status = ?, completed_at = ?, blocker = ? WHERE id = ?",
            (status, completed_at, blocker, task_id),
        )
        audit.record(
            conn, actor, "close.task.status", "close_task", task_id,
            {
                "period": before["period"], "task": before["task"],
                "workday": before["workday"],
                "from": before["status"], "to": status, "blocker": blocker,
            },
            control_ref=before["control_ref"], commit=False,
        )


def progress(conn: sqlite3.Connection, period: str) -> dict:
    """Headline close metrics: completion, on-track flag, and the critical path."""
    df = tasks(conn, period)
    row = ensure_period(conn, period)
    target = date.fromisoformat(row["target_close"])
    wd1 = date.fromisoformat(row["close_start"])
    today = date.today()

    if df.empty:
        return {
            "period": period, "total": 0, "complete": 0, "pct": 0.0, "blocked": 0,
            "overdue": 0, "target_close": target, "close_start": wd1,
            "days_remaining": (target - today).days, "status": row["status"],
            "on_track": True, "critical_path": pd.DataFrame(),
        }

    live = df[df["status"] != "N/A"]
    total = len(live)
    complete = int((live["status"] == "Complete").sum())
    blocked = int((live["status"] == "Blocked").sum())
    overdue = int(
        ((live["due_date"].apply(lambda d: d < today)) & (live["status"] != "Complete")).sum()
    )
    pct = round(100 * complete / total, 1) if total else 0.0

    # Expected completion by now, based on how far through the close window we are.
    elapsed = sum(1 for wd in config.CLOSE_WORKDAYS if workday_dates(conn, period)[wd] <= today)
    expected_pct = round(100 * elapsed / len(config.CLOSE_WORKDAYS), 1)

    critical = live[(live["status"] != "Complete")].copy()
    critical = critical.sort_values("due_date").head(10)

    return {
        "period": period,
        "total": total,
        "complete": complete,
        "pct": pct,
        "expected_pct": expected_pct,
        "blocked": blocked,
        "overdue": overdue,
        "target_close": target,
        "close_start": wd1,
        "days_remaining": (target - today).days,
        "status": row["status"],
        "on_track": pct >= expected_pct and blocked == 0,
        "critical_path": critical,
    }


def by_workday(conn: sqlite3.Connection, period: str) -> pd.DataFrame:
    df = tasks(conn, period)
    if df.empty:
        return df
    g = (
        df.groupby("workday")
        .agg(tasks=("id", "count"),
             complete=("status", lambda s: int((s == "Complete").sum())),
             blocked=("status", lambda s: int((s == "Blocked").sum())))
        .reindex(config.CLOSE_WORKDAYS)
        .fillna(0)
        .astype(int)
        .reset_index()
    )
    g["pct"] = (100 * g["complete"] / g["tasks"].replace(0, pd.NA)).fillna(0).round(1)
    return g


def sign_off_period(conn: sqlite3.Connection, period: str, actor: str) -> dict:
    """Hard gate: a period cannot be closed with open or blocked tasks."""
    prog = progress(conn, period)
    open_tasks = prog["total"] - prog["complete"]
    if open_tasks > 0:
        return {
            "ok": False,
            "message": f"{open_tasks} task(s) still open — the period cannot be signed off.",
        }
    unposted = db.scalar(
        conn,
        "SELECT COUNT(*) FROM journal_entries WHERE period = ? AND status NOT IN"
        " ('Posted','Rejected','Reversed')",
        (period,),
    )
    if unposted:
        return {"ok": False, "message": f"{unposted} journal entry(ies) not yet posted or rejected."}

    unreviewed = db.scalar(
        conn,
        "SELECT COUNT(*) FROM reconciliations WHERE period = ? AND status != 'Reviewed'",
        (period,),
    )
    if unreviewed:
        return {"ok": False, "message": f"{unreviewed} reconciliation(s) not yet independently reviewed."}

    actual = date.today().isoformat()
    with db.transaction(conn):
        conn.execute(
            "UPDATE close_periods SET status = 'Closed', actual_close = ? WHERE period = ?",
            (actual, period),
        )
        audit.record(conn, actor, "close.period.signoff", "close_period", period,
                     {"actual_close": actual, "tasks": prog["total"]},
                     control_ref="GL-C11", commit=False)
    target = prog["target_close"]
    days_late = (date.fromisoformat(actual) - target).days
    return {
        "ok": True,
        "message": f"Period {period} signed off on {actual}"
                   + (f" ({days_late} day(s) after target)." if days_late > 0 else " — within SLA."),
    }


def sla_history(conn: sqlite3.Connection) -> pd.DataFrame:
    """Actual vs target close, period over period — the metric leadership sees."""
    df = periods(conn)
    if df.empty:
        return df
    df = df[df["actual_close"].notna()].copy()
    if df.empty:
        return df
    df["days_variance"] = df.apply(
        lambda r: (date.fromisoformat(r["actual_close"])
                   - date.fromisoformat(r["target_close"])).days,
        axis=1,
    )
    df["within_sla"] = df["days_variance"] <= 0
    return df.sort_values("period")
