"""
Module 4b — Cross-border intercompany matching and IAS 21 decomposition.

The question this module exists to answer is the one that eats a close week:
*the intercompany accounts do not agree — how much of that is a real break, how
much is timing, and how much is just currency?*

Every intercompany document is held once per side.  For a period the engine
produces, for each entity pair:

``gross difference = timing difference + FX revaluation + true mismatch``

where

* **timing difference** — a document booked by one side and not (yet) the other;
* **FX revaluation** — both sides agree in transaction currency, but the
  carrying amounts differ because they were translated at different rates.
  Under IAS 21.23(a) monetary items are retranslated at the closing rate, and
  the resulting exchange difference goes to profit or loss (IAS 21.28);
* **true mismatch** — the sides disagree in the transaction currency itself.
  That is an invoicing or coding error and needs a journal, not an explanation.

Rate convention throughout: ``rate`` = units of transaction currency per 1 USD,
so ``group_amount = transaction_amount / rate``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pandas as pd

from .. import audit, config, db

MATCH_TOLERANCE = 1.0  # transaction currency


def rates(conn: sqlite3.Connection, period: str) -> pd.DataFrame:
    return db.query(conn, "SELECT * FROM fx_rates WHERE period = ? ORDER BY currency", (period,))


def closing_rate(conn: sqlite3.Connection, period: str, currency: str) -> float:
    if currency == config.GROUP_CURRENCY:
        return 1.0
    row = db.fetch_one(
        conn, "SELECT closing_rate FROM fx_rates WHERE period=? AND currency=?", (period, currency)
    )
    if row is None:
        raise ValueError(f"No closing rate for {currency} in {period} — load the group rate table.")
    return float(row["closing_rate"])


def set_rate(conn: sqlite3.Connection, actor: str, period: str, currency: str,
             closing: float, average: float) -> None:
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO fx_rates(period, currency, closing_rate, average_rate) VALUES (?,?,?,?)"
            " ON CONFLICT(period, currency) DO UPDATE SET closing_rate = excluded.closing_rate,"
            " average_rate = excluded.average_rate",
            (period, currency.upper(), float(closing), float(average)),
        )
        audit.record(conn, actor, "fx.rate.set", "fx_rate", f"{period}/{currency}",
                     {"closing": closing, "average": average},
                     control_ref="GL-C10", commit=False)


def documents(conn: sqlite3.Connection, period: str) -> pd.DataFrame:
    return db.query(conn, "SELECT * FROM intercompany WHERE period = ? ORDER BY doc_ref, entity",
                    (period,))


# ------------------------------------------------------------------ matching
def match(conn: sqlite3.Connection, period: str) -> pd.DataFrame:
    """Document-level match. One row per document with its classification."""
    docs = documents(conn, period)
    if docs.empty:
        return docs

    rows = []
    for doc_ref, grp in docs.groupby("doc_ref"):
        if len(grp) == 1:
            r = grp.iloc[0]
            rate = closing_rate(conn, period, r["currency"])
            reval = round(r["amount_txn"] / rate - r["booked_group"], 2)
            rows.append({
                "doc_ref": doc_ref,
                "entity": r["entity"],
                "counterparty": r["counterparty"],
                "currency": r["currency"],
                "amount_txn": float(r["amount_txn"]),
                "counter_txn": 0.0,
                "txn_break": float(r["amount_txn"]),
                "booked_group": float(r["booked_group"]),
                "counter_group": 0.0,
                "gross_group_break": float(r["booked_group"]),
                "classification": "Timing difference",
                "timing": float(r["booked_group"]),
                "fx_revaluation": reval,
                "true_mismatch": 0.0,
                "detail": "Recorded by one side only — not yet booked by the counterparty.",
                "posting_date": r["posting_date"],
                "description": r["description"],
            })
            continue

        if len(grp) > 2:
            rows.append({
                "doc_ref": doc_ref, "entity": ", ".join(sorted(grp["entity"])),
                "counterparty": "-", "currency": grp.iloc[0]["currency"],
                "amount_txn": float(grp["amount_txn"].sum()), "counter_txn": 0.0,
                "txn_break": float(grp["amount_txn"].sum()),
                "booked_group": float(grp["booked_group"].sum()), "counter_group": 0.0,
                "gross_group_break": float(grp["booked_group"].sum()),
                "classification": "Requires investigation",
                "timing": 0.0, "fx_revaluation": 0.0,
                "true_mismatch": float(grp["booked_group"].sum()),
                "detail": f"{len(grp)} postings carry this document reference.",
                "posting_date": grp.iloc[0]["posting_date"],
                "description": grp.iloc[0]["description"],
            })
            continue

        a, b = grp.iloc[0], grp.iloc[1]
        # transaction-currency break: mirror postings should net to nil
        txn_break = round(float(a["amount_txn"]) + float(b["amount_txn"]), 2)
        gross_group = round(float(a["booked_group"]) + float(b["booked_group"]), 2)

        if a["currency"] != b["currency"]:
            # Cross-currency pair: compare both legs in group currency at closing rate.
            ra = closing_rate(conn, period, a["currency"])
            rb = closing_rate(conn, period, b["currency"])
            economic = round(a["amount_txn"] / ra + b["amount_txn"] / rb, 2)
            fx_part = round(gross_group - economic, 2)
            classification = "FX revaluation" if abs(economic) <= MATCH_TOLERANCE else "True mismatch"
            rows.append({
                "doc_ref": doc_ref, "entity": a["entity"], "counterparty": b["entity"],
                "currency": f"{a['currency']}/{b['currency']}",
                "amount_txn": float(a["amount_txn"]), "counter_txn": float(b["amount_txn"]),
                "txn_break": economic,
                "booked_group": float(a["booked_group"]), "counter_group": float(b["booked_group"]),
                "gross_group_break": gross_group,
                "classification": classification,
                "timing": 0.0,
                "fx_revaluation": fx_part,
                "true_mismatch": economic if classification == "True mismatch" else 0.0,
                "detail": "Legs are denominated in different currencies; compared at closing rate.",
                "posting_date": a["posting_date"], "description": a["description"],
            })
            continue

        if abs(txn_break) <= MATCH_TOLERANCE:
            # Same amount in transaction currency — any group difference is pure FX.
            classification = "Matched" if abs(gross_group) <= MATCH_TOLERANCE else "FX revaluation"
            rows.append({
                "doc_ref": doc_ref, "entity": a["entity"], "counterparty": b["entity"],
                "currency": a["currency"],
                "amount_txn": float(a["amount_txn"]), "counter_txn": float(b["amount_txn"]),
                "txn_break": txn_break,
                "booked_group": float(a["booked_group"]), "counter_group": float(b["booked_group"]),
                "gross_group_break": gross_group,
                "classification": classification,
                "timing": 0.0,
                "fx_revaluation": gross_group,
                "true_mismatch": 0.0,
                "detail": "Sides agree in transaction currency; the difference is translation only "
                          "(IAS 21.23(a) — retranslate at closing rate, difference to P&L).",
                "posting_date": a["posting_date"], "description": a["description"],
            })
        else:
            rate = closing_rate(conn, period, a["currency"])
            mismatch_group = round(txn_break / rate, 2)
            rows.append({
                "doc_ref": doc_ref, "entity": a["entity"], "counterparty": b["entity"],
                "currency": a["currency"],
                "amount_txn": float(a["amount_txn"]), "counter_txn": float(b["amount_txn"]),
                "txn_break": txn_break,
                "booked_group": float(a["booked_group"]), "counter_group": float(b["booked_group"]),
                "gross_group_break": gross_group,
                "classification": "True mismatch",
                "timing": 0.0,
                "fx_revaluation": round(gross_group - mismatch_group, 2),
                "true_mismatch": mismatch_group,
                "detail": "The two sides disagree in the transaction currency itself — "
                          "an invoicing or coding break, not a currency effect.",
                "posting_date": a["posting_date"], "description": a["description"],
            })

    return pd.DataFrame(rows).sort_values("gross_group_break", key=lambda s: s.abs(),
                                          ascending=False).reset_index(drop=True)


def decomposition(conn: sqlite3.Connection, period: str) -> dict:
    """Headline split of the intercompany break for the period."""
    m = match(conn, period)
    if m.empty:
        return {"documents": 0, "matched": 0, "gross": 0.0, "timing": 0.0,
                "fx": 0.0, "mismatch": 0.0, "unresolved": 0.0, "by_pair": pd.DataFrame()}

    by_pair = (
        m.groupby(["entity", "counterparty"], as_index=False)[
            ["gross_group_break", "timing", "fx_revaluation", "true_mismatch"]
        ].sum().round(2)
    )
    by_pair["explained"] = (by_pair["timing"] + by_pair["fx_revaluation"]
                            + by_pair["true_mismatch"]).round(2)
    by_pair["residual"] = (by_pair["gross_group_break"] - by_pair["explained"]).round(2)

    return {
        "documents": len(m),
        "matched": int((m["classification"] == "Matched").sum()),
        "gross": round(float(m["gross_group_break"].sum()), 2),
        "timing": round(float(m["timing"].sum()), 2),
        "fx": round(float(m["fx_revaluation"].sum()), 2),
        "mismatch": round(float(m["true_mismatch"].sum()), 2),
        "unresolved": round(float(m[m["classification"] == "Requires investigation"]
                                  ["gross_group_break"].sum()), 2),
        "by_pair": by_pair,
    }


def revaluation_schedule(conn: sqlite3.Connection, period: str) -> pd.DataFrame:
    """IAS 21 retranslation of every open monetary intercompany item.

    Unrealised gain/(loss) = amount at closing rate − carrying amount in the GL.
    """
    docs = documents(conn, period)
    if docs.empty:
        return docs
    out = []
    for r in docs.itertuples():
        cr = closing_rate(conn, period, r.currency)
        at_closing = round(r.amount_txn / cr, 2)
        movement = round(at_closing - r.booked_group, 2)
        out.append({
            "doc_ref": r.doc_ref,
            "entity": r.entity,
            "counterparty": r.counterparty,
            "account_no": r.account_no,
            "currency": r.currency,
            "amount_txn": r.amount_txn,
            "historical_rate": r.historical_rate,
            "closing_rate": cr,
            "carrying_group": r.booked_group,
            "at_closing_rate": at_closing,
            "fx_gain_loss": movement,
            "treatment": "P&L — exchange difference on a monetary item (IAS 21.28)",
        })
    df = pd.DataFrame(out)
    return df.sort_values("fx_gain_loss", key=lambda s: s.abs(), ascending=False).reset_index(drop=True)


def revaluation_journal(conn: sqlite3.Connection, period: str, entity: str | None = None) -> dict:
    """Build the proposed IAS 21 revaluation journal lines for an entity."""
    sched = revaluation_schedule(conn, period)
    if sched.empty:
        return {"lines": [], "net": 0.0}
    if entity and entity != "All":
        sched = sched[sched["entity"] == entity]
    if sched.empty:
        return {"lines": [], "net": 0.0}

    lines = []
    for account_no, grp in sched.groupby("account_no"):
        net = round(float(grp["fx_gain_loss"].sum()), 2)
        if abs(net) < 0.01:
            continue
        lines.append({
            "account_no": account_no,
            "debit": net if net > 0 else 0.0,
            "credit": -net if net < 0 else 0.0,
            "memo": f"IAS 21 retranslation of intercompany balances at {period} closing rate",
        })
    net_total = round(sum(l["debit"] - l["credit"] for l in lines), 2)
    if abs(net_total) >= 0.01:
        # balancing entry to the FX gain/loss P&L account
        lines.append({
            "account_no": "7810",
            "debit": -net_total if net_total < 0 else 0.0,
            "credit": net_total if net_total > 0 else 0.0,
            "memo": "Net unrealised exchange difference to profit or loss (IAS 21.28)",
        })
    return {"lines": lines, "net": net_total}


def add_document(conn: sqlite3.Connection, actor: str, period: str, doc_ref: str,
                 entity: str, counterparty: str, account_no: str, currency: str,
                 amount_txn: float, historical_rate: float, booked_group: float | None = None,
                 posting_date: str | None = None, description: str | None = None) -> int:
    if booked_group is None:
        booked_group = round(float(amount_txn) / float(historical_rate), 2)
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO intercompany(period, doc_ref, entity, counterparty, account_no,"
            " currency, amount_txn, historical_rate, booked_group, posting_date, description)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (period, doc_ref, entity, counterparty, account_no, currency.upper(),
             float(amount_txn), float(historical_rate), float(booked_group),
             posting_date or datetime.now().date().isoformat(), description),
        )
        new_id = int(cur.lastrowid)
        audit.record(conn, actor, "ic.document.add", "intercompany", doc_ref,
                     {"entity": entity, "counterparty": counterparty,
                      "amount_txn": amount_txn, "currency": currency},
                     control_ref="GL-C04", commit=False)
    return new_id
