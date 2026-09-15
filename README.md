# GL-Command — The 2-Year Pioneer Scale & Compliance Hub

An operating framework and working application for a Senior General Ledger Accountant
leading a pioneer finance team in a global industrial, manufacturing and mining-services
group — built around SAP FI/CO data structures, a 3-working-day close, IFRS
(IAS 2, IAS 16, IAS 21, IAS 36, IAS 37) and dual IFRS / US GAAP reporting.

It is a single-user, local-first Python application. Nothing leaves your machine.

---

## Run it

```bash
cd ~/Documents/gl-command
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

It opens at <http://localhost:8501>. The first run creates `data/glcommand.db` and loads
a demo dataset so every module has something real in it.

```bash
python scripts/reset.py            # wipe, start empty and load your own data
python scripts/reset.py --seed     # wipe and reload the demo dataset
python tests/test_smoke.py         # 83 behavioural checks over the control logic
```

---

## The design principle

Most "close trackers" are checklists that record what you *say* you did. This one is
built the other way round: the controls are enforced by the code, and the audit evidence
is produced as a by-product of doing the work.

Concretely, the application will refuse to let you:

- submit a journal that does not balance, posts to an unknown account, or carries a
  description too thin to stand as evidence;
- review a journal, a reconciliation or a flux commentary that you prepared yourself —
  and the attempt is written to the audit trail, because an attempted self-review is
  itself something a reviewer wants to see;
- post an entry above the escalation threshold on two signatures;
- approve a judgemental entry with nothing in the document vault;
- sign off a period while any checklist task is open, any journal unposted, or any
  reconciliation unreviewed;
- review a reconciliation whose unexplained residual is outside tolerance;
- record "timing" as a root cause and call the flux pack done.

Switch identity in the sidebar (**Acting as**) to see the segregation-of-duties gates
actually fire.

---

## Architecture

```
gl-command/
├── app.py                       Command centre — the single screen that answers
│                                "what needs me today"
├── requirements.txt
├── .streamlit/config.toml       Theme (pinned light so charts and chrome agree)
│
├── glcmd/                       ── the application package ──────────────────────
│   ├── config.py                Entities, currencies, materiality policy, close SLA,
│   │                            IFRS control library, users and roles. Everything a
│   │                            controller would change without touching logic.
│   ├── db.py                    SQLite schema and connection handling. Append-only by
│   │                            convention: rows carry status, nothing is deleted.
│   ├── audit.py                 Hash-chained immutable audit trail + verifier.
│   ├── seed.py                  The demo dataset (see below).
│   │
│   ├── services/                ── all business logic lives here, never in a page ──
│   │   ├── roadmap.py           Module 1 — phases, milestones, phase gates.
│   │   ├── close.py             Module 2a — working-day close model, SLA, sign-off gate.
│   │   ├── journals.py          Module 2b — JE lifecycle, dual sign-off, document vault.
│   │   ├── flux.py              Module 3 — TB ingestion, PoP / BvA flux, materiality gate,
│   │   │                        commentary obligation.
│   │   ├── recon.py             Module 4a — reconciliation tracker, sub-ledger matching.
│   │   ├── fx.py                Module 4b — intercompany matching, IAS 21 decomposition.
│   │   └── compliance.py        Module 5 — IAS 16/36, IAS 2, IAS 37, control library.
│   │
│   └── ui/components.py         Shared chrome: palette, KPI tiles, chart builders.
│
├── pages/                       ── Streamlit multipage UI, presentation only ──────
│   ├── 1_Roadmap.py
│   ├── 2_Close_and_GL.py
│   ├── 3_Flux_Analysis.py
│   ├── 4_Reconciliations.py
│   ├── 5_Compliance_Vault.py
│   └── 6_Audit_Trail.py
│
├── data/
│   ├── glcommand.db             Created on first run (git-ignored)
│   ├── templates/               CSV import templates matching SAP extract shapes
│   └── vault/                   Supporting documents, stored by JE reference
│
├── scripts/reset.py
└── tests/test_smoke.py          83 behavioural checks
```

**The layering rule:** a page never contains a calculation. Every number on screen comes
from a function in `glcmd/services/`, which means the logic is testable without
Streamlit — and `tests/test_smoke.py` exercises all of it headlessly.

---

## The five modules

### 1 · Timeline & Milestone Operating Engine

Forty-seven milestones across six phases — first week, first month, 3 months, 6 months,
1 year, 2 years — each with an objective, a *success measure* (how you would evidence it
to a reviewer), an owner and a due date derived from your start date.

The phase **gate** is the mechanism that makes it an operating engine rather than a list:
a phase is not passed until every milestone in it is complete, and the engine flags a
phase showing progress while an earlier gate is still open. In a pioneer role the
sequencing *is* the control — you cannot credibly automate a process you have not yet
standardised, and the roadmap says so.

### 2 · Financial Close & GL Operations

The close is modelled as working days (WD-2 → WD+3), not as a flat list, so slippage is
visible as *which day broke*. Target close is WD+3 by default (`CLOSE_SLA_DAYS`), and
the SLA trend chart shows days against target for every completed period.

The journal workspace enforces the sign-off chain:

```
Draft → Pending Review → Approved → Posted
         (reviewer ≠ preparer)         │
         + Controller if ≥ 1,000,000   └→ Reversed (by a new, mirrored entry)
```

Supporting documents are SHA-256 fingerprinted on upload and re-verified on display, so
a swapped file shows as `ALTERED` rather than passing silently.

### 3 · Automated Flux & Variance Analysis Engine

Ingests SAP trial balances (`period, entity, account_no, currency, amount_local`, with
optional budget columns), translates to group currency at the period closing rate, and
**refuses the load** on unknown accounts or entities. It warns when the trial balance
does not net to nil per company code — the completeness check that should run before any
flux pack.

Two bases: period over period, and budget vs actual. Both apply a two-dimensional
materiality gate:

| Rule | Behaviour | When to use |
|---|---|---|
| `and` (default) | flags only when **both** the % and the absolute movement break | normal month — stops a $40 swing on a dormant account demanding a narrative |
| `or` | flags when **either** breaks | audit season, or a period you do not yet trust |

Thresholds are live in the sidebar. Every flagged account carries a commentary
obligation, the engine rejects one-word answers, and commentary needs an independent
reviewer — the same rule as a journal.

### 4 · Complex Reconciliations & Intercurrency Matching Hub

The unexplained difference is **computed, never typed**:

```
unexplained = GL − sub-ledger − Σ(open reconciling items)
```

Auto-match pulls the GL side from the loaded trial balance, nets the open items, and
advances anything inside tolerance to *Prepared*. It never signs anything off. Items
ageing past 30 days are reported as exceptions (GL-C09).

The intercompany engine decomposes the break document by document:

```
gross difference = timing difference + FX revaluation + true mismatch
```

- **Timing** — booked by one side, not yet by the counterparty.
- **FX revaluation** — the sides agree in the transaction currency; only the carrying
  amounts differ. IAS 21.23(a): retranslate the monetary item at the closing rate;
  IAS 21.28: the exchange difference goes to profit or loss. The app builds the
  proposed journal.
- **True mismatch** — the sides disagree in the *transaction currency itself*. That is
  not currency. It is an invoicing or coding break and it needs a journal.

That distinction is the whole point of the module, and it is the thing that turns a
week of chasing into an afternoon.

### 5 · Risk, Audit & Compliance Vault

- **IAS 16 / IAS 36** — a register carrying both straight-line and **units-of-production**
  depreciation (the method that matters for a rig fleet). The app recomputes accumulated
  depreciation from cost, residual and units/life and compares it with what the register
  carries — a difference usually means a life change was never applied or a disposal was
  never processed. Impairment indicators are tracked separately from the loss itself, so
  "indicator identified, test outstanding" is a visible state.
- **IAS 2** — lower of cost and NRV per line, where NRV = selling price − costs to
  complete − costs to sell. Both write-downs (IAS 2.34) and permitted reversals
  (IAS 2.33) are computed, and the proposed journal is generated.
- **IAS 37** — a provision roll-forward with discount unwind and FX, splitting recognised
  provisions from contingent items that are *disclosed but not provided*. Provisions
  without evidence on file are flagged before period end, not at audit.
- **Control library** — twelve GL and ITGC controls, each referenced from the module that
  performs it, with test freshness tracked against frequency.

### Cross-cutting · Immutable audit trail

```
row_hash = SHA256( prev_hash ‖ canonical_json(entry) )
```

Verification recomputes the entire chain and reports the id of the first broken link —
detecting both an edited entry (hash mismatch) and a deleted, inserted or re-ordered one
(broken `prev_hash` pointer). The *Object lineage* view traces any journal,
reconciliation or asset end to end: every hand that touched it, in order, with the hash.

This is tamper **evidence**, not tamper **prevention** — an administrator with write
access to the database file could rewrite the whole chain. The production answer is to
anchor the chain head externally at each close (a notarised log, or an object store with
a retention lock). The app shows you the current chain head for exactly that purpose.

---

## The demo dataset

Seven entities across five currencies (USD, CAD, AUD, SGD, CLP, ZAR), group currency USD.
Three periods of trial balance that each net to nil per company code. Seventeen fixed
assets including four drill rigs on units-of-production. Fifteen inventory lines.
Eight provisions. Sixteen intercompany documents.

It is deliberately **imperfect**, so every module has something to do on first run:

- flux breaches waiting for commentary (consumables up 38%, environmental spend up 55%,
  rental revenue down 29%, an FX loss that more than doubled);
- an idle drill rig in Chile whose recoverable amount sits ~464k below carrying value;
- a legacy NQ bit series and an obsolete head assembly carried above NRV;
- an onerous-contract provision with no evidence on file;
- an intercompany ledger with a timing difference each way, two FX-only breaks, a
  cross-currency treasury leg, and one 45,000 invoicing break that is a *real* error;
- journals in every state, including one in your review queue and one above the
  escalation threshold.

Reset it the moment you have your own data:

```bash
python scripts/reset.py --yes
```

---

## Adapting it to the real role

The things you will change first, in order:

1. **`glcmd/config.py`** — `ENTITIES` (company codes and functional currencies),
   `USERS` (your actual team and their roles), `DEFAULT_MATERIALITY`,
   `JE_ESCALATION_THRESHOLD`, `CLOSE_SLA_DAYS`, and `CONTROL_LIBRARY` to match the
   group's control matrix.
2. **`glcmd/seed.py` → `ACCOUNTS`** — replace with the real chart of accounts, setting
   `recon_required` on the accounts that carry a reconciliation obligation. The
   reconciliation schedule generates itself from that flag.
3. **`CLOSE_TEMPLATE`** in `seed.py` — replace with the group's close checklist, keeping
   the working-day tags and the control references.
4. **`MILESTONES`** — the roadmap is written to be defensible in a first-90-days
   conversation; edit the success measures to match what your Financial Controller will
   actually accept as evidence.
5. **Ingestion** — the TB loader expects a flat extract. If you export from FAGLL03 or
   FAGLB03 with different column names, map them once in
   `glcmd/services/flux.ingest_trial_balance`.

### Where you would go next

- Replace the CSV ingest with a direct SAP pull (RFC / OData / an extract drop folder
  watched on a schedule).
- Anchor the audit chain head externally at each close.
- Move from SQLite to Postgres if the file is ever shared — the schema is portable and
  the service layer does not care.
- Add the US GAAP bridge as a first-class object rather than a framework tag on the
  journal, once you know which differences actually recur.

---

## Notes and limits

- Single-user and local by design. SQLite in WAL mode handles one Streamlit session
  comfortably; it is not a concurrent multi-user server.
- The "identity" selector is a demonstration device, not authentication. Real deployment
  needs SSO and server-side role enforcement — the service layer already takes the actor
  as an argument everywhere, so that change is confined to how `actor` is obtained.
- Nothing here is accounting advice, and the IFRS logic implements the common cases, not
  every scenario in the standards. It is a working instrument for someone who already
  knows the standards, and it shows its workings so you can check them.
