"""
Module 1 — Timeline & Milestone Operating Engine.

The roadmap is stored as dated milestones so the app can answer the only two
questions that matter on any given morning: *what is due now* and *which phase
gate am I not yet entitled to pass through*.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from .. import audit, config, db

STATUSES = ["Not started", "In progress", "Blocked", "Complete", "Deferred"]

#: A phase gate cannot be declared passed while any *critical* milestone in the
#: phase is open. These are the milestones an incoming senior GL accountant
#: genuinely cannot skip.
GATE_RULE_MIN_PROGRESS = 100


def start_date(conn: sqlite3.Connection) -> date:
    raw = db.get_setting(conn, "start_date")
    if raw:
        return date.fromisoformat(raw)
    today = date.today()
    db.set_setting(conn, "start_date", today.isoformat())
    return today


def set_start_date(conn: sqlite3.Connection, value: date, actor: str) -> None:
    db.set_setting(conn, "start_date", value.isoformat())
    audit.record(conn, actor, "roadmap.start_date.set", "roadmap", "start_date",
                 {"start_date": value.isoformat()})


def load(conn: sqlite3.Connection, phase: str | None = None) -> pd.DataFrame:
    sql = "SELECT * FROM milestones"
    params: list[Any] = []
    if phase:
        sql += " WHERE phase = ?"
        params.append(phase)
    sql += " ORDER BY due_day, id"
    df = db.query(conn, sql, params)
    if df.empty:
        return df
    start = start_date(conn)
    df["due_date"] = df["due_day"].apply(lambda d: start + timedelta(days=int(d)))
    today = date.today()
    df["days_to_due"] = df["due_date"].apply(lambda d: (d - today).days)
    df["phase_name"] = df["phase"].map(config.PHASE_LABELS)
    df["overdue"] = (df["days_to_due"] < 0) & (df["status"] != "Complete")
    return df


def update_milestone(
    conn: sqlite3.Connection,
    milestone_id: int,
    actor: str,
    status: str | None = None,
    progress_pct: int | None = None,
    evidence: str | None = None,
    notes: str | None = None,
    owner: str | None = None,
) -> None:
    before = db.fetch_one(conn, "SELECT * FROM milestones WHERE id = ?", (milestone_id,))
    if before is None:
        raise ValueError(f"Milestone {milestone_id} not found")

    fields: dict[str, Any] = {}
    if status is not None:
        if status not in STATUSES:
            raise ValueError(f"Unknown status '{status}'")
        fields["status"] = status
        if status == "Complete":
            fields["progress_pct"] = 100
    if progress_pct is not None:
        fields["progress_pct"] = max(0, min(100, int(progress_pct)))
        if fields["progress_pct"] == 100 and fields.get("status") != "Complete":
            fields.setdefault("status", "Complete")
    if evidence is not None:
        fields["evidence"] = evidence
    if notes is not None:
        fields["notes"] = notes
    if owner is not None:
        fields["owner"] = owner
    if not fields:
        return

    fields["updated_at"] = datetime.now().isoformat(timespec="seconds")
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with db.transaction(conn):
        conn.execute(
            f"UPDATE milestones SET {assignments} WHERE id = ?",
            (*fields.values(), milestone_id),
        )
        audit.record(
            conn, actor, "roadmap.milestone.update", "milestone", milestone_id,
            {
                "objective": before["objective"],
                "from": {"status": before["status"], "progress_pct": before["progress_pct"]},
                "to": {k: v for k, v in fields.items() if k != "updated_at"},
            },
            commit=False,
        )


def add_milestone(
    conn: sqlite3.Connection,
    actor: str,
    phase: str,
    workstream: str,
    objective: str,
    success_measure: str,
    due_day: int,
    owner: str | None = None,
) -> int:
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO milestones(phase, workstream, objective, success_measure,"
            " due_day, owner, updated_at) VALUES (?,?,?,?,?,?,?)",
            (phase, workstream, objective, success_measure, int(due_day), owner,
             datetime.now().isoformat(timespec="seconds")),
        )
        new_id = int(cur.lastrowid)
        audit.record(conn, actor, "roadmap.milestone.create", "milestone", new_id,
                     {"phase": phase, "objective": objective, "due_day": due_day},
                     commit=False)
    return new_id


def phase_summary(conn: sqlite3.Connection) -> pd.DataFrame:
    """One row per phase with completion, overdue count and gate status."""
    df = load(conn)
    rows = []
    prior_gate_open = False
    for code, name, window, purpose in config.PHASES:
        sub = df[df["phase"] == code] if not df.empty else df
        total = len(sub)
        complete = int((sub["status"] == "Complete").sum()) if total else 0
        overdue = int(sub["overdue"].sum()) if total else 0
        blocked = int((sub["status"] == "Blocked").sum()) if total else 0
        pct = round(sub["progress_pct"].mean(), 1) if total else 0.0
        gate_passed = total > 0 and complete == total
        rows.append(
            {
                "phase": code,
                "phase_name": name,
                "window": window,
                "purpose": purpose,
                "milestones": total,
                "complete": complete,
                "overdue": overdue,
                "blocked": blocked,
                "progress_pct": pct,
                "gate": "Passed" if gate_passed else ("Blocked" if blocked else "Open"),
                # you should not be running ahead while an earlier gate is open
                "sequence_warning": prior_gate_open and pct > 0,
            }
        )
        if not gate_passed:
            prior_gate_open = True
    return pd.DataFrame(rows)


def focus_list(conn: sqlite3.Connection, horizon_days: int = 14) -> pd.DataFrame:
    """What is actually on the plate: overdue first, then the next fortnight."""
    df = load(conn)
    if df.empty:
        return df
    live = df[df["status"].isin(["Not started", "In progress", "Blocked"])].copy()
    live = live[live["days_to_due"] <= horizon_days]
    live["urgency"] = live["days_to_due"].apply(
        lambda d: "Overdue" if d < 0 else ("Due this week" if d <= 7 else "Upcoming")
    )
    order = {"Overdue": 0, "Due this week": 1, "Upcoming": 2}
    live["_o"] = live["urgency"].map(order)
    return live.sort_values(["_o", "days_to_due"]).drop(columns=["_o"])
