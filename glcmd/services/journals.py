"""
Module 2b — Journal entry workspace with enforced dual sign-off.

Control design (mirrors GL-C02):

* an entry must balance before it can leave Draft;
* the reviewer may not be the preparer — enforced in code, not by convention;
* entries above :data:`config.JE_ESCALATION_THRESHOLD` need a third,
  controller-level approval;
* an entry carrying a judgemental risk tag cannot be approved without at least
  one supporting document in the vault;
* posted entries are never edited — they are reversed, and the reversal is a new
  entry that points back at the original.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from .. import audit, config, db

JUDGEMENTAL_TAGS = {
    "Estimate / Judgement", "Top-side", "Management override risk",
    "Related party", "Non-recurring",
}

JE_TYPES = ["Standard", "Accrual", "Prepayment", "Reclassification",
            "Correction", "FX revaluation", "Provision", "Reversal"]


# ------------------------------------------------------------------ queries
def list_entries(
    conn: sqlite3.Connection,
    period: str | None = None,
    status: str | None = None,
    entity: str | None = None,
) -> pd.DataFrame:
    sql = "SELECT * FROM journal_entries WHERE 1=1"
    params: list[Any] = []
    if period and period != "All":
        sql += " AND period = ?"
        params.append(period)
    if status and status != "All":
        sql += " AND status = ?"
        params.append(status)
    if entity and entity != "All":
        sql += " AND entity = ?"
        params.append(entity)
    return db.query(conn, sql + " ORDER BY id DESC", params)


def get(conn: sqlite3.Connection, je_ref: str) -> sqlite3.Row | None:
    return db.fetch_one(conn, "SELECT * FROM journal_entries WHERE je_ref = ?", (je_ref,))


def lines(conn: sqlite3.Connection, je_ref: str) -> pd.DataFrame:
    return db.query(
        conn,
        "SELECT jl.line_no, jl.account_no, a.description AS account_name, jl.cost_center,"
        " jl.debit, jl.credit, jl.memo FROM journal_lines jl"
        " LEFT JOIN accounts a ON a.account_no = jl.account_no"
        " WHERE jl.je_ref = ? ORDER BY jl.line_no",
        (je_ref,),
    )


def documents(conn: sqlite3.Connection, je_ref: str) -> pd.DataFrame:
    return db.query(conn, "SELECT * FROM je_documents WHERE je_ref = ? ORDER BY id", (je_ref,))


def next_ref(conn: sqlite3.Connection, period: str) -> str:
    n = db.scalar(conn, "SELECT COUNT(*) FROM journal_entries WHERE period = ?", (period,))
    return f"JE-{period.replace('-', '')}-{int(n) + 1:04d}"


# ------------------------------------------------------------------ create
def create(
    conn: sqlite3.Connection,
    actor: str,
    period: str,
    entity: str,
    description: str,
    je_type: str,
    risk_tag: str,
    currency: str,
    entry_lines: Sequence[dict],
    framework: str = "IFRS",
    reversal_of: str | None = None,
) -> str:
    """Create a Draft entry. ``entry_lines`` = [{account_no, debit, credit, ...}]."""
    if not entry_lines:
        raise ValueError("A journal entry needs at least two lines.")
    total_dr = round(sum(float(l.get("debit") or 0) for l in entry_lines), 2)
    total_cr = round(sum(float(l.get("credit") or 0) for l in entry_lines), 2)
    if total_dr == 0 and total_cr == 0:
        raise ValueError("A journal entry cannot be for nil.")

    ref = next_ref(conn, period)
    now = datetime.now().isoformat(timespec="seconds")
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO journal_entries(je_ref, period, entity, description, je_type,"
            " risk_tag, framework, currency, total_debit, total_credit, status, preparer,"
            " prepared_at, reversal_of) VALUES (?,?,?,?,?,?,?,?,?,?, 'Draft', ?,?,?)",
            (ref, period, entity, description, je_type, risk_tag, framework, currency,
             total_dr, total_cr, actor, now, reversal_of),
        )
        for i, l in enumerate(entry_lines, start=1):
            conn.execute(
                "INSERT INTO journal_lines(je_ref, line_no, account_no, cost_center,"
                " debit, credit, memo) VALUES (?,?,?,?,?,?,?)",
                (ref, i, str(l["account_no"]), l.get("cost_center"),
                 float(l.get("debit") or 0), float(l.get("credit") or 0), l.get("memo")),
            )
        audit.record(
            conn, actor, "je.create", "journal_entry", ref,
            {"period": period, "entity": entity, "description": description,
             "type": je_type, "risk_tag": risk_tag, "dr": total_dr, "cr": total_cr,
             "lines": len(entry_lines), "reversal_of": reversal_of},
            control_ref="GL-C02", commit=False,
        )
    return ref


# ------------------------------------------------------------ control gates
def validate(conn: sqlite3.Connection, je_ref: str) -> list[str]:
    """Return a list of blocking issues. Empty list means the entry may proceed."""
    je = get(conn, je_ref)
    if je is None:
        return [f"{je_ref} does not exist."]
    issues: list[str] = []

    if round(je["total_debit"], 2) != round(je["total_credit"], 2):
        issues.append(
            f"Entry does not balance: debits {je['total_debit']:,.2f} vs "
            f"credits {je['total_credit']:,.2f}."
        )
    ln = lines(conn, je_ref)
    if len(ln) < 2:
        issues.append("Fewer than two lines.")
    if not ln.empty:
        both = ln[(ln["debit"] > 0) & (ln["credit"] > 0)]
        if not both.empty:
            issues.append(f"Line(s) {list(both['line_no'])} carry both a debit and a credit.")
        known = set(db.query(conn, "SELECT account_no FROM accounts")["account_no"])
        unknown = sorted(set(ln["account_no"]) - known)
        if unknown:
            issues.append(f"Account(s) not in the chart of accounts: {', '.join(unknown)}.")
    if not je["description"] or len(je["description"].strip()) < 15:
        issues.append("Description is too thin to stand as audit evidence (min 15 characters).")
    if je["risk_tag"] in JUDGEMENTAL_TAGS and je["support_count"] == 0:
        issues.append(
            f"Risk tag '{je['risk_tag']}' requires at least one supporting document."
        )
    return issues


def submit(conn: sqlite3.Connection, je_ref: str, actor: str) -> dict:
    je = get(conn, je_ref)
    if je is None:
        return {"ok": False, "message": "Entry not found."}
    if je["status"] != "Draft":
        return {"ok": False, "message": f"Only Draft entries can be submitted (currently {je['status']})."}
    if je["preparer"] != actor:
        return {"ok": False, "message": "Only the preparer can submit this entry for review."}
    issues = validate(conn, je_ref)
    if issues:
        return {"ok": False, "message": " ".join(issues)}

    with db.transaction(conn):
        conn.execute("UPDATE journal_entries SET status = 'Pending Review' WHERE je_ref = ?", (je_ref,))
        audit.record(conn, actor, "je.submit", "journal_entry", je_ref,
                     {"status": "Pending Review"}, control_ref="GL-C02", commit=False)
    return {"ok": True, "message": f"{je_ref} submitted for independent review."}


def review(conn: sqlite3.Connection, je_ref: str, reviewer: str, decision: str,
           note: str | None = None) -> dict:
    """Second signature. ``decision`` is 'Approve' or 'Reject'."""
    je = get(conn, je_ref)
    if je is None:
        return {"ok": False, "message": "Entry not found."}
    if je["status"] != "Pending Review":
        return {"ok": False, "message": f"Entry is {je['status']}, not awaiting review."}

    # --- the segregation-of-duties gate -------------------------------------
    if reviewer == je["preparer"]:
        audit.record(conn, reviewer, "je.review.blocked_sod", "journal_entry", je_ref,
                     {"reason": "Preparer attempted to self-review"}, control_ref="GL-C02")
        return {
            "ok": False,
            "message": "Segregation of duties: the preparer cannot review their own entry. "
                       "The attempt has been logged to the audit trail.",
        }

    now = datetime.now().isoformat(timespec="seconds")
    if decision == "Reject":
        with db.transaction(conn):
            conn.execute(
                "UPDATE journal_entries SET status = 'Rejected', reviewer = ?, reviewed_at = ?,"
                " rejection_note = ? WHERE je_ref = ?",
                (reviewer, now, note, je_ref),
            )
            audit.record(conn, reviewer, "je.reject", "journal_entry", je_ref,
                         {"note": note}, control_ref="GL-C02", commit=False)
        return {"ok": True, "message": f"{je_ref} rejected and returned to the preparer."}

    issues = validate(conn, je_ref)
    if issues:
        return {"ok": False, "message": "Cannot approve: " + " ".join(issues)}

    value = max(je["total_debit"], je["total_credit"])
    needs_controller = value >= config.JE_ESCALATION_THRESHOLD
    # A high-value entry stays in the review queue after the second signature so
    # the Controller can add the third; everything else goes straight to Approved.
    new_status = "Pending Review" if needs_controller else "Approved"

    with db.transaction(conn):
        conn.execute(
            "UPDATE journal_entries SET reviewer = ?, reviewed_at = ?, status = ? WHERE je_ref = ?",
            (reviewer, now, new_status, je_ref),
        )
        audit.record(
            conn, reviewer, "je.review.approve", "journal_entry", je_ref,
            {"value": value, "controller_required": needs_controller},
            control_ref="GL-C02", commit=False,
        )
    if needs_controller:
        return {
            "ok": True,
            "message": f"Reviewed. Value of {value:,.2f} is at or above the "
                       f"{config.JE_ESCALATION_THRESHOLD:,.0f} escalation threshold — "
                       "a Controller signature is still required.",
            "controller_required": True,
        }
    return {"ok": True, "message": f"{je_ref} approved and ready to post."}


def controller_approve(conn: sqlite3.Connection, je_ref: str, controller: str) -> dict:
    je = get(conn, je_ref)
    if je is None:
        return {"ok": False, "message": "Entry not found."}
    if not je["reviewer"]:
        return {"ok": False, "message": "The entry has not been independently reviewed yet."}
    if controller in (je["preparer"], je["reviewer"]):
        return {"ok": False, "message": "The controller signature must be a third, distinct person."}
    user = config.USER_BY_NAME.get(controller)
    if user and user.role != "Controller":
        return {"ok": False, "message": f"{user.display_name} does not hold the Controller role."}

    now = datetime.now().isoformat(timespec="seconds")
    with db.transaction(conn):
        conn.execute(
            "UPDATE journal_entries SET controller = ?, approved_at = ?, status = 'Approved'"
            " WHERE je_ref = ?",
            (controller, now, je_ref),
        )
        audit.record(conn, controller, "je.controller_approve", "journal_entry", je_ref,
                     {"value": max(je["total_debit"], je["total_credit"])},
                     control_ref="GL-C02", commit=False)
    return {"ok": True, "message": f"{je_ref} carries the third signature and is ready to post."}


def post(conn: sqlite3.Connection, je_ref: str, actor: str) -> dict:
    je = get(conn, je_ref)
    if je is None:
        return {"ok": False, "message": "Entry not found."}
    if je["status"] != "Approved":
        return {"ok": False, "message": f"Only Approved entries can be posted (currently {je['status']})."}
    value = max(je["total_debit"], je["total_credit"])
    if value >= config.JE_ESCALATION_THRESHOLD and not je["controller"]:
        return {"ok": False, "message": "Controller signature missing for a high-value entry."}

    period_row = db.fetch_one(conn, "SELECT status FROM close_periods WHERE period = ?", (je["period"],))
    if period_row and period_row["status"] == "Closed":
        return {"ok": False, "message": f"Period {je['period']} is closed. Post to the open period instead."}

    now = datetime.now().isoformat(timespec="seconds")
    with db.transaction(conn):
        conn.execute("UPDATE journal_entries SET status = 'Posted', posted_at = ? WHERE je_ref = ?",
                     (now, je_ref))
        audit.record(conn, actor, "je.post", "journal_entry", je_ref,
                     {"period": je["period"], "entity": je["entity"], "value": value,
                      "preparer": je["preparer"], "reviewer": je["reviewer"],
                      "controller": je["controller"]},
                     control_ref="GL-C02", commit=False)
    return {"ok": True, "message": f"{je_ref} posted."}


def reverse(conn: sqlite3.Connection, je_ref: str, actor: str, period: str,
            reason: str) -> dict:
    """Posted entries are corrected by reversal, never by edit."""
    je = get(conn, je_ref)
    if je is None:
        return {"ok": False, "message": "Entry not found."}
    if je["status"] != "Posted":
        return {"ok": False, "message": "Only posted entries can be reversed."}

    original = lines(conn, je_ref)
    flipped = [
        {"account_no": r.account_no, "cost_center": r.cost_center,
         "debit": r.credit, "credit": r.debit,
         "memo": f"Reversal of {je_ref}: {reason}"}
        for r in original.itertuples()
    ]
    new_ref = create(
        conn, actor, period, je["entity"],
        f"Reversal of {je_ref} — {reason}", "Reversal", je["risk_tag"],
        je["currency"], flipped, framework=je["framework"], reversal_of=je_ref,
    )
    with db.transaction(conn):
        conn.execute("UPDATE journal_entries SET status = 'Reversed' WHERE je_ref = ?", (je_ref,))
        audit.record(conn, actor, "je.reverse", "journal_entry", je_ref,
                     {"reversal_ref": new_ref, "reason": reason},
                     control_ref="GL-C02", commit=False)
    return {"ok": True, "message": f"{je_ref} reversed by {new_ref} (still in Draft).", "ref": new_ref}


# --------------------------------------------------------- document vault
def attach_document(
    conn: sqlite3.Connection,
    je_ref: str,
    actor: str,
    filename: str,
    content: bytes,
    doc_type: str = "Support",
) -> dict:
    """Store the file under data/vault/<je_ref>/ and fingerprint it with SHA-256."""
    je = get(conn, je_ref)
    if je is None:
        return {"ok": False, "message": "Entry not found."}
    if je["status"] in ("Posted", "Reversed"):
        return {"ok": False, "message": "Supporting documents cannot be added after posting."}

    digest = hashlib.sha256(content).hexdigest()
    folder = config.VAULT_DIR / je_ref
    folder.mkdir(parents=True, exist_ok=True)
    safe = Path(filename).name
    target = folder / f"{digest[:12]}_{safe}"
    target.write_bytes(content)

    now = datetime.now().isoformat(timespec="seconds")
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO je_documents(je_ref, filename, doc_type, sha256, size_bytes,"
            " stored_path, uploaded_by, uploaded_at) VALUES (?,?,?,?,?,?,?,?)",
            (je_ref, safe, doc_type, digest, len(content), str(target), actor, now),
        )
        conn.execute(
            "UPDATE journal_entries SET support_count = support_count + 1 WHERE je_ref = ?",
            (je_ref,),
        )
        audit.record(conn, actor, "je.document.attach", "journal_entry", je_ref,
                     {"filename": safe, "sha256": digest, "bytes": len(content)},
                     control_ref="GL-C02", commit=False)
    return {"ok": True, "message": f"Attached {safe} (sha256 {digest[:12]}…).", "sha256": digest}


def verify_documents(conn: sqlite3.Connection, je_ref: str) -> pd.DataFrame:
    """Re-hash every stored file and report whether it still matches."""
    docs = documents(conn, je_ref)
    if docs.empty:
        return docs
    results = []
    for r in docs.itertuples():
        p = Path(r.stored_path)
        if not p.exists():
            results.append("Missing")
            continue
        actual = hashlib.sha256(p.read_bytes()).hexdigest()
        results.append("Intact" if actual == r.sha256 else "ALTERED")
    docs = docs.copy()
    docs["integrity"] = results
    return docs


# ------------------------------------------------------------------ queues
def review_queue(conn: sqlite3.Connection, user: str) -> pd.DataFrame:
    """Entries this user is *permitted* to review (i.e. did not prepare)."""
    df = list_entries(conn, status="Pending Review")
    if df.empty:
        return df
    return df[df["preparer"] != user]


def sod_exceptions(conn: sqlite3.Connection) -> pd.DataFrame:
    """Any posted entry where the signatures collapse onto one person."""
    df = db.query(
        conn,
        "SELECT je_ref, period, entity, description, risk_tag, total_debit, preparer,"
        " reviewer, controller, status FROM journal_entries WHERE status IN ('Posted','Approved')",
    )
    if df.empty:
        return df
    bad = df[(df["reviewer"].isna()) | (df["reviewer"] == df["preparer"])]
    return bad


def stats(conn: sqlite3.Connection, period: str | None = None) -> dict:
    df = list_entries(conn, period=period)
    if df.empty:
        return {"total": 0, "pending": 0, "posted": 0, "rejected": 0,
                "judgemental": 0, "unsupported": 0, "value": 0.0}
    judgemental = df[df["risk_tag"].isin(JUDGEMENTAL_TAGS)]
    return {
        "total": len(df),
        "pending": int((df["status"] == "Pending Review").sum()),
        "draft": int((df["status"] == "Draft").sum()),
        "approved": int((df["status"] == "Approved").sum()),
        "posted": int((df["status"] == "Posted").sum()),
        "rejected": int((df["status"] == "Rejected").sum()),
        "judgemental": len(judgemental),
        "unsupported": int((judgemental["support_count"] == 0).sum()),
        "value": float(df["total_debit"].sum()),
    }
