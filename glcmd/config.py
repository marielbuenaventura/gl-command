"""
GL-Command — central configuration.

Everything that a controller would want to change without touching logic lives
here: entity/currency master data, materiality policy, close-cycle SLA, and the
IFRS control library references.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "GL-Command"
APP_TAGLINE = "The 2-Year Pioneer Scale & Compliance Hub"
APP_VERSION = "1.0.0"

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("GLCMD_DATA_DIR", BASE_DIR / "data"))
TEMPLATE_DIR = DATA_DIR / "templates"
VAULT_DIR = DATA_DIR / "vault"          # supporting-document store
DB_PATH = Path(os.environ.get("GLCMD_DB", DATA_DIR / "glcommand.db"))

for _d in (DATA_DIR, TEMPLATE_DIR, VAULT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------
# Group reporting basis
# --------------------------------------------------------------------------
GROUP_CURRENCY = "USD"

#: Dual-reporting frameworks maintained in parallel.
REPORTING_FRAMEWORKS = ["IFRS", "US GAAP"]


@dataclass(frozen=True)
class Entity:
    code: str
    name: str
    functional_ccy: str
    region: str
    consolidation: str = "Full"


ENTITIES: list[Entity] = [
    Entity("1000", "Group Holdings (US)", "USD", "North America"),
    Entity("1100", "Drilling Services US", "USD", "North America"),
    Entity("1200", "Drilling Services Canada", "CAD", "North America"),
    Entity("2100", "Products & Manufacturing AU", "AUD", "APAC"),
    Entity("2200", "Regional Shared Services SG", "SGD", "APAC"),
    Entity("3100", "Mining Services Chile", "CLP", "LATAM"),
    Entity("4100", "Mining Services South Africa", "ZAR", "EMEA"),
]

ENTITY_BY_CODE = {e.code: e for e in ENTITIES}
ENTITY_LABELS = {e.code: f"{e.code} — {e.name} ({e.functional_ccy})" for e in ENTITIES}


# --------------------------------------------------------------------------
# Materiality policy (flux & variance engine)
# --------------------------------------------------------------------------
@dataclass
class MaterialityPolicy:
    """Two-dimensional materiality gate.

    ``rule='and'`` (default) flags an account only when BOTH the percentage and
    the absolute movement breach their thresholds.  This is the conventional
    controller setting: it stops a $40 swing on a dormant account from
    generating a mandatory root-cause narrative.  ``rule='or'`` is the
    conservative audit-season setting.
    """

    pct_threshold: float = 10.0       # percent
    abs_threshold: float = 50_000.0   # group currency
    rule: str = "and"                 # "and" | "or"

    def breaches(self, pct: float | None, delta: float) -> bool:
        pct_hit = pct is not None and abs(pct) >= self.pct_threshold
        abs_hit = abs(delta) >= self.abs_threshold
        if self.rule == "or":
            return pct_hit or abs_hit
        return pct_hit and abs_hit


DEFAULT_MATERIALITY = MaterialityPolicy()

#: Overall group performance materiality — drives audit sampling in the vault.
PERFORMANCE_MATERIALITY = 2_500_000.0

# --------------------------------------------------------------------------
# Close cycle SLA — the "tight 3-day close"
# --------------------------------------------------------------------------
CLOSE_SLA_DAYS = 3

#: Working-day buckets used across the close calendar.
CLOSE_WORKDAYS = ["WD-2", "WD-1", "WD+1", "WD+2", "WD+3"]

CLOSE_STREAMS = [
    "Sub-ledger cut-off",
    "Fixed assets",
    "Inventory",
    "Intercompany & FX",
    "Accruals & provisions",
    "Payroll",
    "Bank & cash",
    "GL review & flux",
    "Reporting pack",
]

# --------------------------------------------------------------------------
# Journal entry governance
# --------------------------------------------------------------------------
JE_STATUSES = ["Draft", "Pending Review", "Approved", "Posted", "Rejected", "Reversed"]

JE_RISK_TAGS = [
    "Routine",
    "Estimate / Judgement",
    "Top-side",
    "Management override risk",
    "Related party",
    "Non-recurring",
    "Cut-off sensitive",
    "FX revaluation",
]

#: Journals above this value require a second-level (controller) approval on top
#: of the standard preparer/reviewer split.
JE_ESCALATION_THRESHOLD = 1_000_000.0

# --------------------------------------------------------------------------
# IFRS control library
# --------------------------------------------------------------------------
IFRS_STANDARDS = {
    "IAS 2": "Inventories — measured at the lower of cost and net realisable value.",
    "IAS 16": "Property, Plant & Equipment — recognition, depreciation, impairment.",
    "IAS 21": "The Effects of Changes in Foreign Exchange Rates — translation & revaluation.",
    "IAS 36": "Impairment of Assets — recoverable amount testing (CGU level).",
    "IAS 37": "Provisions, Contingent Liabilities and Contingent Assets.",
    "IFRS 15": "Revenue from Contracts with Customers.",
}

CONTROL_LIBRARY = [
    # (control_ref, name, standard, frequency, assertion)
    ("GL-C01", "Balance sheet reconciliations prepared and independently reviewed",
     "IAS 1", "Monthly", "Existence / Valuation"),
    ("GL-C02", "Journal entries require segregated preparer and approver",
     "IAS 1", "Per entry", "Occurrence / Authorisation"),
    ("GL-C03", "Flux analysis performed on all accounts breaching materiality",
     "IAS 1", "Monthly", "Accuracy / Completeness"),
    ("GL-C04", "Intercompany balances agreed and eliminated; FX isolated",
     "IAS 21", "Monthly", "Accuracy / Valuation"),
    ("GL-C05", "Depreciation run reviewed against the fixed asset register",
     "IAS 16", "Monthly", "Valuation / Allocation"),
    ("GL-C06", "Impairment indicators assessed for rigs and heavy plant",
     "IAS 36", "Quarterly", "Valuation"),
    ("GL-C07", "Inventory costed and tested against net realisable value",
     "IAS 2", "Monthly", "Valuation"),
    ("GL-C08", "Provision roll-forward evidenced and discount unwind recorded",
     "IAS 37", "Quarterly", "Completeness / Valuation"),
    ("GL-C09", "Bank reconciliations cleared of items aged over 30 days",
     "IAS 7", "Monthly", "Existence"),
    ("GL-C10", "Period-end FX rates loaded from the group rate table",
     "IAS 21", "Monthly", "Accuracy"),
    ("GL-C11", "System access and posting periods restricted after close",
     "SOX ITGC", "Monthly", "Authorisation"),
    ("GL-C12", "Audit trail integrity verified and archived",
     "SOX ITGC", "Monthly", "Completeness"),
]

# --------------------------------------------------------------------------
# Roadmap phases — the 2-year operating engine
# --------------------------------------------------------------------------
PHASES = [
    ("P1", "First Week", "Day 1–5",
     "Immersion, SAP access mapping, system baseline checks."),
    ("P2", "First Month", "Day 6–30",
     "First close executed end-to-end under supervision; baseline documented."),
    ("P3", "First 3 Months", "Month 2–3",
     "Process standardisation and independent balance sheet ownership."),
    ("P4", "First 6 Months", "Month 4–6",
     "Cross-functional automation with FP&A, Tax and Operations."),
    ("P5", "First Year", "Month 7–12",
     "Institutional stability, audit readiness, advanced SAP FI/CO integration."),
    ("P6", "Second Year", "Month 13–24",
     "Operational leadership scale, team build-out, continuous control monitoring."),
]

PHASE_LABELS = {p[0]: p[1] for p in PHASES}

# --------------------------------------------------------------------------
# Users / segregation of duties
# --------------------------------------------------------------------------
ROLES = ["Preparer", "Reviewer", "Controller", "Auditor"]


@dataclass
class User:
    username: str
    display_name: str
    role: str
    entities: list[str] = field(default_factory=list)


USERS: list[User] = [
    User("m.buenaventura", "Mariel Buenaventura", "Controller", [e.code for e in ENTITIES]),
    User("a.reyes", "Ana Reyes", "Preparer", ["1100", "1200"]),
    User("j.okafor", "Joseph Okafor", "Preparer", ["4100", "2100"]),
    User("l.tan", "Li Wei Tan", "Reviewer", ["2200", "2100"]),
    User("c.moreno", "Carlos Moreno", "Reviewer", ["3100", "1100"]),
    User("external.audit", "Group External Audit", "Auditor", [e.code for e in ENTITIES]),
]

USER_BY_NAME = {u.username: u for u in USERS}
USER_LABELS = {u.username: f"{u.display_name} ({u.role})" for u in USERS}
