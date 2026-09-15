"""Immutable audit trail — integrity verification and object lineage."""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from glcmd import audit, config, db
from glcmd.ui import components as ui

conn = ui.page(
    "Immutable Audit Trail",
    "🔒",
    "Every write in this system appends a hash-chained entry. Each entry carries the "
    "hash of the one before it, so deleting, re-ordering or editing any historical "
    "record breaks the chain from that point onward — and the verifier names the first "
    "broken link.",
)
ctx = ui.sidebar(conn, show_period=False)
actor = ctx["actor"]

integrity = audit.verify_chain(conn)
total = db.scalar(conn, "SELECT COUNT(*) FROM audit_log")
actors = db.scalar(conn, "SELECT COUNT(DISTINCT actor) FROM audit_log")
controls_evidenced = db.scalar(
    conn, "SELECT COUNT(DISTINCT control_ref) FROM audit_log WHERE control_ref IS NOT NULL"
)

ui.tiles([
    ("Chain status", "Verified" if integrity["valid"] else "BROKEN",
     f"{integrity['checked']:,} entries recomputed"
     if integrity["valid"] else integrity["reason"],
     "good" if integrity["valid"] else "critical"),
    ("Entries", f"{total:,}", f"{actors} distinct actor(s)", None),
    ("Controls evidenced", f"{controls_evidenced}",
     f"of {len(config.CONTROL_LIBRARY)} in the library", None),
    ("Verification", "SHA-256", "prev_hash ‖ canonical payload", None),
])

ui.rule()

tab_browse, tab_lineage, tab_proof = st.tabs(
    ["Browse the trail", "Object lineage", "How the proof works"]
)

# ==========================================================================
with tab_browse:
    c1, c2, c3, c4 = st.columns(4)
    types = db.query(conn, "SELECT DISTINCT entity_type FROM audit_log ORDER BY entity_type")
    f_type = c1.selectbox("Object type", ["All"] + types["entity_type"].tolist())
    actor_list = db.query(conn, "SELECT DISTINCT actor FROM audit_log ORDER BY actor")
    f_actor = c2.selectbox("Actor", ["All"] + actor_list["actor"].tolist())
    f_action = c3.text_input("Action contains", placeholder="je.post")
    limit = c4.number_input("Rows", 50, 5000, 400, 50)

    trail = audit.trail(
        conn,
        entity_type=None if f_type == "All" else f_type,
        actor=None if f_actor == "All" else f_actor,
        action=f_action or None,
        limit=int(limit),
    )

    if trail.empty:
        st.info("No entries match this filter.")
    else:
        disp = trail.copy()
        disp["payload"] = disp["payload"].apply(
            lambda p: json.dumps(json.loads(p or "{}"), separators=(", ", ": "))[:180]
        )
        disp["row_hash"] = disp["row_hash"].str.slice(0, 14) + "…"
        ui.dataframe(disp.rename(columns={
            "id": "#", "ts": "Timestamp (UTC)", "actor": "Actor", "action": "Action",
            "entity_type": "Object", "entity_id": "Id", "control_ref": "Control",
            "payload": "Payload", "row_hash": "Hash"}), height=520)

        st.download_button("Export the trail (CSV)", trail.to_csv(index=False).encode(),
                           file_name="audit_trail.csv", mime="text/csv")

    ui.rule()
    st.markdown("#### Activity by action")
    by_action = db.query(
        conn,
        "SELECT action, COUNT(*) AS entries FROM audit_log GROUP BY action"
        " ORDER BY entries DESC LIMIT 18",
    )
    ui.bar_h(by_action, "action", "entries", height=max(220, 26 * len(by_action)),
             tooltip=["action", "entries"])

    st.markdown("#### Control coverage")
    by_control = db.query(
        conn,
        "SELECT control_ref, COUNT(*) AS entries FROM audit_log"
        " WHERE control_ref IS NOT NULL GROUP BY control_ref ORDER BY control_ref",
    )
    if by_control.empty:
        st.caption("No control-referenced activity yet.")
    else:
        names = {c[0]: c[1] for c in config.CONTROL_LIBRARY}
        by_control["label"] = by_control["control_ref"] + " — " + \
            by_control["control_ref"].map(names).fillna("")
        by_control["label"] = by_control["label"].str.slice(0, 60)
        ui.bar_h(by_control, "label", "entries", sort=None,
                 height=max(220, 28 * len(by_control)),
                 tooltip=["control_ref", "entries"])
        st.caption("Each bar is evidence that the control operated, generated as a "
                   "by-product of doing the work rather than assembled afterwards.")

# ==========================================================================
with tab_lineage:
    st.markdown("#### Trace one object end to end")
    ui.note(
        "This is the view an auditor asks for: take one journal entry, one "
        "reconciliation or one asset, and show every hand that touched it, in order, "
        "with the hash that proves the record has not moved since."
    )
    st.write("")

    c1, c2 = st.columns([1, 2])
    types = db.query(conn, "SELECT DISTINCT entity_type FROM audit_log ORDER BY entity_type")
    obj_type = c1.selectbox("Object type", types["entity_type"].tolist(),
                            key="lin_type")
    ids = db.query(
        conn,
        "SELECT DISTINCT entity_id FROM audit_log WHERE entity_type = ? AND entity_id IS NOT NULL"
        " ORDER BY entity_id", (obj_type,),
    )
    if ids.empty:
        st.info("No identified objects of this type.")
    else:
        obj_id = c2.selectbox("Object", ids["entity_id"].tolist(), key="lin_id")
        lin = audit.lineage(conn, obj_type, obj_id)
        if lin.empty:
            st.info("No history for this object.")
        else:
            for r in lin.itertuples():
                payload = json.loads(r.payload or "{}")
                user = config.USER_BY_NAME.get(r.actor)
                name = user.display_name if user else r.actor
                st.markdown(
                    f"**{r.action}** &nbsp;<span style='color:{ui.MUTED};font-size:.78rem'>"
                    f"#{r.id} · {r.ts} · {name}"
                    + (f" · {r.control_ref}" if r.control_ref else "") + "</span>",
                    unsafe_allow_html=True)
                if payload:
                    st.json(payload, expanded=False)
                st.markdown(
                    f"<span style='color:{ui.MUTED};font-size:.72rem;"
                    f"font-family:ui-monospace,monospace'>{r.row_hash}</span>",
                    unsafe_allow_html=True)
                st.write("")

# ==========================================================================
with tab_proof:
    st.markdown("#### How the chain works")
    st.markdown(
        """
Each entry stores the SHA-256 of the previous entry's hash concatenated with its own
canonicalised payload:

```
row_hash = SHA256( prev_hash ‖ canonical_json(entry) )
```

The first entry chains from a genesis value of sixty-four zeros. Verification walks the
whole table in order and recomputes every hash. Two things are therefore detectable and
indistinguishable from each other only in the sense that both are *fatal*:

* **content was edited** — the recomputed hash of that row no longer matches what is stored;
* **a row was deleted, inserted or re-ordered** — the `prev_hash` pointer no longer matches
  the preceding row.

In both cases the verifier reports the id of the first broken link, which bounds the
period that can still be relied on. Nothing in the application updates or deletes an
audit row; the table is append-only by construction.

This is tamper **evidence**, not tamper **prevention**. A determined administrator with
write access to the database file can rewrite the whole chain. The production answer is
to periodically anchor the latest `row_hash` somewhere outside the database — a
notarised log, an append-only object store with retention lock, or the group's own
evidence vault — so that a wholesale rewrite is also detectable.
        """
    )

    ui.rule()
    st.markdown("#### Run the verification now")
    if st.button("Verify the chain", type="primary"):
        result = audit.verify_chain(conn)
        if result["valid"]:
            st.success(f"Chain verified. {result['checked']:,} entries recomputed and "
                       "every hash matches.")
        else:
            st.error(f"Chain broken at entry #{result['broken_at']}. {result['reason']}")

    latest = db.fetch_one(conn, "SELECT id, ts, row_hash FROM audit_log ORDER BY id DESC LIMIT 1")
    if latest:
        st.markdown("##### Current chain head")
        st.code(f"entry #{latest['id']}  {latest['ts']}\n{latest['row_hash']}",
                language="text")
        st.caption("Anchor this value externally at each period close and the whole "
                   "period becomes independently provable.")
