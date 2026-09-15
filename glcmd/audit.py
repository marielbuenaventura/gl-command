"""
Immutable audit trail.

Each entry is chained: ``row_hash = sha256(prev_hash || canonical_payload)``.
Changing or removing any historical row breaks every hash after it, and
:func:`verify_chain` reports the first broken link.  That is what makes the
trail evidential rather than merely a log table.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from . import db

GENESIS = "0" * 64


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)


def _hash(prev_hash: str, record: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + _canonical(record)).encode("utf-8")).hexdigest()


def last_hash(conn: sqlite3.Connection) -> str:
    row = db.fetch_one(conn, "SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1")
    return row["row_hash"] if row else GENESIS


def record(
    conn: sqlite3.Connection,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str | int | None = None,
    payload: dict[str, Any] | None = None,
    control_ref: str | None = None,
    commit: bool = True,
) -> str:
    """Append one tamper-evident entry. Returns the new row hash."""
    ts = _now()
    prev = last_hash(conn)
    body = {
        "ts": ts,
        "actor": actor,
        "action": action,
        "entity_type": entity_type,
        "entity_id": str(entity_id) if entity_id is not None else None,
        "control_ref": control_ref,
        "payload": payload or {},
    }
    row_hash = _hash(prev, body)
    conn.execute(
        "INSERT INTO audit_log(ts, actor, action, entity_type, entity_id, control_ref,"
        " payload, prev_hash, row_hash) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            ts,
            actor,
            action,
            entity_type,
            str(entity_id) if entity_id is not None else None,
            control_ref,
            _canonical(payload or {}),
            prev,
            row_hash,
        ),
    )
    if commit:
        conn.commit()
    return row_hash


def verify_chain(conn: sqlite3.Connection) -> dict[str, Any]:
    """Recompute the whole chain.

    Returns ``{"valid", "checked", "broken_at", "reason"}``.
    """
    rows = conn.execute(
        "SELECT id, ts, actor, action, entity_type, entity_id, control_ref,"
        " payload, prev_hash, row_hash FROM audit_log ORDER BY id"
    ).fetchall()

    prev = GENESIS
    for r in rows:
        if r["prev_hash"] != prev:
            return {
                "valid": False,
                "checked": len(rows),
                "broken_at": r["id"],
                "reason": "Previous-hash pointer does not match the preceding entry "
                          "(an entry was deleted, re-ordered or inserted).",
            }
        body = {
            "ts": r["ts"],
            "actor": r["actor"],
            "action": r["action"],
            "entity_type": r["entity_type"],
            "entity_id": r["entity_id"],
            "control_ref": r["control_ref"],
            "payload": json.loads(r["payload"] or "{}"),
        }
        expected = _hash(prev, body)
        if expected != r["row_hash"]:
            return {
                "valid": False,
                "checked": len(rows),
                "broken_at": r["id"],
                "reason": "Entry content was modified after it was written.",
            }
        prev = r["row_hash"]

    return {"valid": True, "checked": len(rows), "broken_at": None, "reason": ""}


def trail(
    conn: sqlite3.Connection,
    entity_type: str | None = None,
    entity_id: str | None = None,
    actor: str | None = None,
    action: str | None = None,
    limit: int = 500,
) -> pd.DataFrame:
    sql = "SELECT id, ts, actor, action, entity_type, entity_id, control_ref, payload, row_hash FROM audit_log WHERE 1=1"
    params: list[Any] = []
    if entity_type:
        sql += " AND entity_type = ?"
        params.append(entity_type)
    if entity_id:
        sql += " AND entity_id = ?"
        params.append(str(entity_id))
    if actor:
        sql += " AND actor = ?"
        params.append(actor)
    if action:
        sql += " AND action LIKE ?"
        params.append(f"%{action}%")
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return db.query(conn, sql, params)


def lineage(conn: sqlite3.Connection, entity_type: str, entity_id: str) -> pd.DataFrame:
    """Full chronological history for one object — what an auditor asks for."""
    return db.query(
        conn,
        "SELECT id, ts, actor, action, control_ref, payload, row_hash FROM audit_log"
        " WHERE entity_type = ? AND entity_id = ? ORDER BY id",
        (entity_type, str(entity_id)),
    )
