"""
Demo data for GL-Command.

Everything here is fictional but structurally realistic for a global drilling /
mining-services group: multi-currency entities, a units-of-production rig fleet,
consumable inventory that goes obsolete, rehabilitation provisions, and an
intercompany ledger that does not agree.

The dataset is deliberately *imperfect* — there are flux breaches waiting for
commentary, an idle rig carrying an impairment indicator, inventory below NRV,
an intercompany break that is mostly FX, and a journal sitting in the review
queue. That is the point: every module should have something to do on first run.

Run ``python scripts/reset.py`` to clear it before loading your own data.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import date, datetime, timedelta

from . import audit, config, db
from .services import close as close_svc
from .services import recon as recon_svc

SEED_ACTOR = "system.seed"
RNG = random.Random(20260914)


# --------------------------------------------------------------------------
# Chart of accounts
# --------------------------------------------------------------------------
# (account_no, description, type, statement, recon_required, ifrs_standard)
ACCOUNTS = [
    ("1010", "Cash at bank — operating", "Asset", "BS", 1, "IAS 7"),
    ("1020", "Cash at bank — payroll", "Asset", "BS", 1, "IAS 7"),
    ("1030", "Cash — site floats", "Asset", "BS", 1, "IAS 7"),
    ("1100", "Trade receivables", "Asset", "BS", 1, "IFRS 9"),
    ("1150", "Allowance for expected credit losses", "Asset", "BS", 1, "IFRS 9"),
    ("1200", "Contract assets — unbilled revenue", "Asset", "BS", 1, "IFRS 15"),
    ("1300", "Inventory — consumables & drill bits", "Asset", "BS", 1, "IAS 2"),
    ("1310", "Inventory — spare parts", "Asset", "BS", 1, "IAS 2"),
    ("1320", "Inventory — raw materials", "Asset", "BS", 1, "IAS 2"),
    ("1330", "Inventory — finished goods", "Asset", "BS", 1, "IAS 2"),
    ("1390", "Provision for inventory obsolescence", "Asset", "BS", 1, "IAS 2"),
    ("1400", "Prepayments", "Asset", "BS", 1, "IAS 1"),
    ("1450", "Intercompany receivable", "Asset", "BS", 1, "IAS 21"),
    ("1500", "VAT / GST receivable", "Asset", "BS", 1, "IAS 12"),
    ("1600", "Property, plant & equipment — at cost", "Asset", "BS", 1, "IAS 16"),
    ("1650", "Accumulated depreciation", "Asset", "BS", 1, "IAS 16"),
    ("1660", "Accumulated impairment — PP&E", "Asset", "BS", 1, "IAS 36"),
    ("1700", "Right-of-use assets", "Asset", "BS", 1, "IFRS 16"),
    ("1750", "Accumulated amortisation — ROU assets", "Asset", "BS", 1, "IFRS 16"),
    ("1800", "Deferred tax asset", "Asset", "BS", 1, "IAS 12"),

    ("2010", "Trade payables", "Liability", "BS", 1, "IAS 1"),
    ("2050", "Accrued expenses", "Liability", "BS", 1, "IAS 37"),
    ("2100", "Intercompany payable", "Liability", "BS", 1, "IAS 21"),
    ("2150", "Payroll liabilities", "Liability", "BS", 1, "IAS 19"),
    ("2200", "VAT / GST payable", "Liability", "BS", 1, "IAS 12"),
    ("2300", "Lease liabilities", "Liability", "BS", 1, "IFRS 16"),
    ("2400", "Provision — site rehabilitation", "Liability", "BS", 1, "IAS 37"),
    ("2410", "Provision — warranty", "Liability", "BS", 1, "IAS 37"),
    ("2420", "Provision — legal & restructuring", "Liability", "BS", 1, "IAS 37"),
    ("2500", "Income tax payable", "Liability", "BS", 1, "IAS 12"),
    ("2600", "Borrowings", "Liability", "BS", 1, "IFRS 9"),

    ("3010", "Share capital", "Equity", "BS", 0, "IAS 1"),
    ("3100", "Retained earnings", "Equity", "BS", 0, "IAS 1"),
    ("3200", "Foreign currency translation reserve", "Equity", "BS", 0, "IAS 21"),

    ("6010", "Revenue — drilling services", "Revenue", "P&L", 0, "IFRS 15"),
    ("6020", "Revenue — products & manufacturing", "Revenue", "P&L", 0, "IFRS 15"),
    ("6030", "Revenue — equipment rental", "Revenue", "P&L", 0, "IFRS 16"),
    ("6090", "Other income", "Revenue", "P&L", 0, "IAS 1"),

    ("7010", "Direct labour", "Expense", "P&L", 0, "IAS 19"),
    ("7020", "Consumables & drill bits consumed", "Expense", "P&L", 0, "IAS 2"),
    ("7030", "Site & camp costs", "Expense", "P&L", 0, "IAS 1"),
    ("7040", "Equipment maintenance", "Expense", "P&L", 0, "IAS 16"),
    ("7050", "Freight & logistics", "Expense", "P&L", 0, "IAS 2"),
    ("7100", "Cost of goods sold", "Expense", "P&L", 0, "IAS 2"),
    ("7150", "Inventory write-down / (reversal)", "Expense", "P&L", 0, "IAS 2"),
    ("7200", "Depreciation", "Expense", "P&L", 0, "IAS 16"),
    ("7210", "Impairment loss — PP&E", "Expense", "P&L", 0, "IAS 36"),
    ("7250", "Amortisation — right-of-use assets", "Expense", "P&L", 0, "IFRS 16"),
    ("7300", "Salaries & wages — indirect", "Expense", "P&L", 0, "IAS 19"),
    ("7350", "Professional fees", "Expense", "P&L", 0, "IAS 1"),
    ("7400", "Insurance", "Expense", "P&L", 0, "IAS 1"),
    ("7450", "Rehabilitation & environmental costs", "Expense", "P&L", 0, "IAS 37"),
    ("7500", "IT & communications", "Expense", "P&L", 0, "IAS 1"),
    ("7600", "Travel & accommodation", "Expense", "P&L", 0, "IAS 1"),
    ("7810", "Foreign exchange (gain) / loss", "Expense", "P&L", 0, "IAS 21"),
    ("7900", "Finance costs", "Expense", "P&L", 0, "IFRS 9"),
    ("7950", "Income tax expense", "Expense", "P&L", 0, "IAS 12"),
]

ACCOUNT_OWNERS = {
    "1": "a.reyes", "2": "j.okafor", "3": "m.buenaventura",
    "6": "c.moreno", "7": "l.tan",
}

# Entities that carry a trial balance in the demo set
TB_ENTITIES = ["1100", "1200", "2100", "2200", "3100"]

# Base monthly magnitudes in *local* currency, scaled per entity below.
BASE_BALANCES = {
    "1010": 4_200_000, "1020": 310_000, "1030": 45_000,
    "1100": 9_800_000, "1150": -640_000, "1200": 2_450_000,
    "1300": 3_150_000, "1310": 1_980_000, "1320": 870_000, "1330": 1_240_000,
    "1390": -420_000, "1400": 560_000, "1450": 3_600_000, "1500": 310_000,
    "1600": 41_500_000, "1650": -18_900_000, "1660": -1_250_000,
    "1700": 5_400_000, "1750": -2_150_000, "1800": 940_000,
    "2010": -6_300_000, "2050": -2_450_000, "2100": -3_150_000,
    "2150": -1_180_000, "2200": -480_000, "2300": -3_380_000,
    "2400": -7_600_000, "2410": -880_000, "2420": -540_000,
    "2500": -720_000, "2600": -9_500_000,
    "3010": -12_000_000, "3200": -1_450_000,
    "6010": -14_800_000, "6020": -5_200_000, "6030": -1_900_000, "6090": -180_000,
    "7010": 5_100_000, "7020": 2_350_000, "7030": 1_420_000, "7040": 1_180_000,
    "7050": 690_000, "7100": 3_650_000, "7150": 95_000,
    "7200": 1_760_000, "7210": 0, "7250": 185_000,
    "7300": 1_540_000, "7350": 290_000, "7400": 215_000, "7450": 420_000,
    "7500": 165_000, "7600": 245_000, "7810": 85_000, "7900": 310_000,
    "7950": 480_000,
}

ENTITY_SCALE = {"1100": 1.0, "1200": 0.62, "2100": 0.78, "2200": 0.24, "3100": 0.55}

#: Designed movements so the flux engine has real, explainable breaches.
#: account -> (period_index -> multiplier)
DRIVERS = {
    "7020": {2: 1.38},   # consumables spike — new deep-hole programme
    "1300": {2: 1.31},   # consumable stock build ahead of the wet season
    "2400": {2: 1.22},   # rehabilitation provision remeasured at a lower discount rate
    "7450": {2: 1.55},   # environmental remediation work at the Chilean site
    "7810": {2: 2.40},   # FX loss on unhedged intercompany balances
    "7350": {2: 1.62},   # professional fees — audit readiness and tax review
    "6030": {2: 0.71},   # rental revenue down, two rigs off contract
    "1200": {2: 1.28},   # unbilled revenue build — invoicing lag at month end
    "7040": {2: 0.86},
    "2050": {2: 1.19},
}

FX_TABLE = {
    # period_index -> {ccy: (closing, average)}
    0: {"USD": (1.0, 1.0), "CAD": (1.352, 1.358), "AUD": (1.498, 1.505),
        "SGD": (1.344, 1.349), "CLP": (932.0, 938.0), "ZAR": (18.05, 18.12)},
    1: {"USD": (1.0, 1.0), "CAD": (1.371, 1.362), "AUD": (1.526, 1.512),
        "SGD": (1.352, 1.348), "CLP": (958.0, 945.0), "ZAR": (18.44, 18.26)},
    2: {"USD": (1.0, 1.0), "CAD": (1.394, 1.383), "AUD": (1.561, 1.544),
        "SGD": (1.331, 1.342), "CLP": (981.0, 970.0), "ZAR": (18.91, 18.68)},
}


def periods_for_today(today: date | None = None) -> list[str]:
    """The three most recent completed months, oldest first."""
    today = today or date.today()
    y, m = today.year, today.month
    out = []
    for back in (3, 2, 1):
        mm = m - back
        yy = y
        while mm <= 0:
            mm += 12
            yy -= 1
        out.append(f"{yy}-{mm:02d}")
    return out


# --------------------------------------------------------------------------
# Roadmap content — the professional substance of Module 1
# --------------------------------------------------------------------------
# (phase, workstream, objective, success_measure, due_day)
MILESTONES = [
    # ---------------------------------------------------------------- P1 week 1
    ("P1", "Systems access", "Obtain SAP FI/CO access and confirm role profile covers FB03, FAGLL03, FAGLB03, FS10N, GR55 and KSB1",
     "All six transactions execute without an authorisation failure", 3),
    ("P1", "Systems access", "Map the SAP company code / profit centre / cost centre hierarchy to the legal entity structure",
     "One-page hierarchy diagram reviewed by the Financial Controller", 5),
    ("P1", "Data baseline", "Extract the opening trial balance for every company code and tie to the last reported consolidation",
     "Zero unexplained difference to the reported group balance sheet", 5),
    ("P1", "Data baseline", "Inventory the existing close checklist, reconciliation templates and flux files",
     "Written baseline note listing every artefact and its owner", 4),
    ("P1", "Stakeholders", "Meet FP&A, Tax, Treasury, Operations finance and the external audit lead",
     "Five introductory meetings held; owner and cadence agreed for each", 5),
    ("P1", "Controls", "Read the current SOX / internal control matrix and identify GL-owned controls",
     "GL control inventory extracted with named owner per control", 5),
    ("P1", "Risk", "Log the top five inherited risks observed in week one",
     "Risk log created with impact, likelihood and first mitigation step", 5),

    # --------------------------------------------------------------- P2 month 1
    ("P2", "Close execution", "Execute the first month-end close end to end alongside the incumbent",
     "Close completed; every task signed with preparer and reviewer", 30),
    ("P2", "Close execution", "Time-stamp each close task to establish the true critical path",
     "Working-day map produced showing where the 3-day target is lost", 26),
    ("P2", "Reconciliations", "Take ownership of cash, intercompany and accruals reconciliations",
     "All three reconciled and independently reviewed within the close window", 28),
    ("P2", "Flux", "Produce the first flux pack with root-cause commentary above materiality",
     "100% of flagged accounts carry substantive commentary", 29),
    ("P2", "Journals", "Introduce mandatory preparer / reviewer segregation on all manual journals",
     "Zero self-reviewed journals posted in the period", 24),
    ("P2", "Documentation", "Document the as-is close process with SAP transaction codes at each step",
     "Process document circulated and acknowledged by the team", 30),
    ("P2", "Data integrity", "Build the trial-balance completeness check (TB nets to nil per company code)",
     "Automated check run before every flux pack; exceptions cleared", 27),
    ("P2", "Risk", "Confirm posting-period controls (OB52) restrict entry after close",
     "Evidence of period lock captured for the control file", 22),

    # -------------------------------------------------------------- P3 3 months
    ("P3", "Standardisation", "Standardise the close checklist across all company codes into one working-day model",
     "Single checklist adopted; variance in task naming eliminated", 75),
    ("P3", "Standardisation", "Roll out a single balance sheet reconciliation template with risk ratings",
     "Every recon-required account on the standard template", 80),
    ("P3", "Ownership", "Assume independent ownership of the group balance sheet",
     "Sign-off taken without supervisory review for two consecutive closes", 90),
    ("P3", "Peer review", "Establish a documented peer-review rota for judgemental journals and reconciliations",
     "Rota published; every high-risk item peer reviewed", 70),
    ("P3", "Close speed", "Bring the close inside the 3-working-day target",
     "Two consecutive closes completed by WD+3", 90),
    ("P3", "Data integrity", "Eliminate aged reconciling items over 30 days on cash and intercompany",
     "Aged item balance reduced to nil on both account groups", 85),
    ("P3", "Intercompany", "Agree a monthly intercompany confirmation cut-off with every counterparty entity",
     "Cut-off calendar signed by each entity controller", 65),
    ("P3", "IFRS", "Review the fixed asset register against IAS 16 componentisation and useful lives",
     "Register reviewed; proposed life changes documented for approval", 88),
    ("P3", "Team", "Define the pioneer team's roles, RACI and escalation path",
     "RACI approved by the Finance Director", 78),

    # -------------------------------------------------------------- P4 6 months
    ("P4", "Automation", "Automate the trial-balance extract and flux calculation end to end",
     "Flux pack produced within two hours of the TB being final", 150),
    ("P4", "Automation", "Automate sub-ledger-to-GL matching for AR, AP and fixed assets",
     "Manual matching effort reduced to exception handling only", 165),
    ("P4", "FP&A", "Agree a shared actual-vs-budget variance definition and single source of truth with FP&A",
     "One reconciled variance report used by both teams", 140),
    ("P4", "Tax", "Build the tax-sensitive account mapping and quarterly data pack with Tax",
     "Tax pack delivered from the GL with no rework requested", 155),
    ("P4", "Operations", "Connect operational drivers (metres drilled, rig utilisation) to GL cost lines",
     "Cost per metre reported monthly and accepted by Operations", 170),
    ("P4", "Treasury", "Implement a monthly FX revaluation routine isolating timing from translation",
     "IAS 21 revaluation posted from a reviewed schedule each month", 160),
    ("P4", "Close speed", "Move two close tasks from WD+2 to pre-close (WD-2 / WD-1)",
     "Critical path shortened by at least half a working day", 145),
    ("P4", "Controls", "Rationalise duplicated controls and evidence requirements with Internal Audit",
     "Control matrix reduced without loss of coverage; agreed in writing", 175),

    # --------------------------------------------------------------- P5 1 year
    ("P5", "Audit", "Deliver a clean year-end audit with a prepared-by-client schedule issued before fieldwork",
     "Zero audit adjustments above performance materiality", 330),
    ("P5", "Audit", "Build the standing audit readiness file refreshed at each quarter end",
     "File complete at every quarter end without a scramble", 300),
    ("P5", "SAP", "Implement advanced SAP FI/CO integration — automated accrual reversal and recurring entries",
     "Manual recurring journals reduced by at least half", 280),
    ("P5", "SAP", "Deploy validation rules blocking postings to restricted accounts and periods",
     "Zero postings to blocked accounts in the following quarter", 260),
    ("P5", "IFRS", "Complete an IAS 36 impairment review across all CGUs with Finance and Operations",
     "Review documented, conclusions supported, reviewed by audit", 320),
    ("P5", "IFRS", "Rebuild the IAS 37 rehabilitation provision model with documented discount rates",
     "Model reviewed and unwind posted monthly without adjustment", 310),
    ("P5", "Stability", "Institutionalise the close: any team member can run it from the documentation",
     "A close executed by a deputy with no escalation", 350),
    ("P5", "Reporting", "Deliver dual IFRS / US GAAP reporting from one reconciled ledger",
     "Both packs produced from a single source with a documented bridge", 340),

    # -------------------------------------------------------------- P6 2 years
    ("P6", "Scale", "Build out the pioneer GL team and complete onboarding of every hire",
     "Team fully staffed; each member owns a documented portfolio", 480),
    ("P6", "Scale", "Establish a continuous control monitoring dashboard reviewed weekly",
     "Exceptions detected and cleared before period end, not after", 520),
    ("P6", "Close speed", "Sustain a 3-working-day close across all entities for four consecutive quarters",
     "Close SLA met every month for a full year", 700),
    ("P6", "Leadership", "Own the finance element of a new-region or new-entity integration",
     "Entity integrated into the group close on the standard model", 600),
    ("P6", "Leadership", "Run a quarterly technical accounting forum for the wider finance function",
     "Four sessions delivered; attendance and feedback recorded", 640),
    ("P6", "Automation", "Retire the last manual spreadsheet from the critical close path",
     "No spreadsheet is a single point of failure in the close", 660),
    ("P6", "Risk", "Achieve zero significant deficiencies across two consecutive audit cycles",
     "Audit and Internal Audit both report no significant deficiency", 720),
]


# --------------------------------------------------------------------------
# Close checklist template
# --------------------------------------------------------------------------
# (workday, stream, task, control_ref)
CLOSE_TEMPLATE = [
    ("WD-2", "Sub-ledger cut-off", "Confirm AP invoice cut-off and communicate to procurement", None),
    ("WD-2", "Sub-ledger cut-off", "Confirm goods receipt / service entry cut-off with site supervisors", None),
    ("WD-2", "Intercompany & FX", "Issue intercompany confirmation requests to all counterparties", "GL-C04"),
    ("WD-2", "Inventory", "Confirm site stock counts complete and variances investigated", "GL-C07"),
    ("WD-1", "Intercompany & FX", "Load month-end closing and average FX rates from the group rate table", "GL-C10"),
    ("WD-1", "Payroll", "Agree the payroll interface to the payroll provider control total", None),
    ("WD-1", "Accruals & provisions", "Circulate the accrual template to cost centre owners", None),
    ("WD-1", "Fixed assets", "Confirm all capex additions and disposals captured in the register", "GL-C05"),
    ("WD+1", "Sub-ledger cut-off", "Close AP, AR and inventory sub-ledgers in SAP", None),
    ("WD+1", "Bank & cash", "Import bank statements and complete bank reconciliations", "GL-C09"),
    ("WD+1", "Fixed assets", "Execute the depreciation run (AFAB) and review against the register", "GL-C05"),
    ("WD+1", "Inventory", "Post inventory revaluation and obsolescence movement", "GL-C07"),
    ("WD+1", "Payroll", "Post payroll journals and reconcile payroll liability accounts", None),
    ("WD+1", "Accruals & provisions", "Post recurring accruals and reverse prior-period accruals", None),
    ("WD+2", "Intercompany & FX", "Run FX revaluation and isolate timing from translation differences", "GL-C04"),
    ("WD+2", "Intercompany & FX", "Agree intercompany balances and clear breaks above tolerance", "GL-C04"),
    ("WD+2", "Accruals & provisions", "Review provision roll-forward and post the discount unwind", "GL-C08"),
    ("WD+2", "Inventory", "Complete the lower of cost and NRV assessment", "GL-C07"),
    ("WD+2", "GL review & flux", "Complete all balance sheet reconciliations", "GL-C01"),
    ("WD+2", "GL review & flux", "Run the flux analysis and obtain root-cause commentary", "GL-C03"),
    ("WD+3", "GL review & flux", "Independent review of all reconciliations and journals", "GL-C01"),
    ("WD+3", "GL review & flux", "Final trial balance review and balance sheet sign-off", "GL-C03"),
    ("WD+3", "Reporting pack", "Produce the IFRS reporting pack and submit to consolidation", None),
    ("WD+3", "Reporting pack", "Produce the US GAAP bridge and reconcile to IFRS result", None),
    ("WD+3", "Reporting pack", "Lock the posting period in SAP (OB52) and archive the audit trail", "GL-C11"),
]


# --------------------------------------------------------------------------
# Fixed assets
# --------------------------------------------------------------------------
# (asset_no, description, entity, class, cgu, acq_date, cost, residual, life_m,
#  method, total_units, units_to_date, accum_dep, status, recoverable, indicator)
PPE = [
    ("FA-1001", "Surface core drill rig LF-160 #1", "1100", "Drill rigs", "North America Drilling",
     "2021-03-15", 1_850_000, 150_000, None, "Units of production", 60_000, 31_400, 889_000, "In use", None, None),
    ("FA-1002", "Surface core drill rig LF-160 #2", "1100", "Drill rigs", "North America Drilling",
     "2021-07-01", 1_850_000, 150_000, None, "Units of production", 60_000, 26_800, 758_900, "In use", None, None),
    ("FA-1003", "Underground drill rig S250 #1", "1200", "Drill rigs", "North America Drilling",
     "2020-01-20", 2_150_000, 180_000, None, "Units of production", 55_000, 41_200, 1_474_600, "In use", None, None),
    # Idle rig: still has useful life left, but the recoverable amount on the
    # revised programme sits below carrying value — an IAS 36 loss to recognise.
    ("FA-1004", "Underground drill rig S250 #2", "3100", "Drill rigs", "LATAM Mining Services",
     "2019-09-10", 2_150_000, 180_000, None, "Units of production", 55_000, 22_500, 805_909.09, "Idle",
     880_000, "Asset idle or under-utilised: off contract since the Atacama programme ended"),
    ("FA-1005", "Reverse circulation rig RC-500", "3100", "Drill rigs", "LATAM Mining Services",
     "2022-06-01", 1_420_000, 120_000, None, "Units of production", 48_000, 18_300, 495_800, "In use", None, None),
    ("FA-1010", "CNC machining centre — bit manufacturing", "2100", "Heavy plant & machinery",
     "APAC Products", "2020-11-05", 3_250_000, 250_000, 180, "Straight line", None, None, 1_183_300, "In use", None, None),
    ("FA-1011", "Heat treatment furnace line", "2100", "Heavy plant & machinery", "APAC Products",
     "2019-04-18", 2_780_000, 200_000, 180, "Straight line", None, None, 1_360_000, "In use", None, None),
    ("FA-1012", "Diamond impregnation press", "2100", "Heavy plant & machinery", "APAC Products",
     "2023-02-10", 1_640_000, 120_000, 144, "Straight line", None, None, 379_000, "In use", None, None),
    ("FA-1020", "Core handling & sample prep plant", "3100", "Mining equipment", "LATAM Mining Services",
     "2021-08-22", 980_000, 60_000, 120, "Straight line", None, None, 391_000, "In use", None, None),
    ("FA-1021", "Mobile crushing unit", "1100", "Mining equipment", "North America Drilling",
     "2022-03-01", 745_000, 55_000, 120, "Straight line", None, None, 264_000, "In use", None,
     "Commodity price decline: client programme under review"),
    ("FA-1030", "Heavy haul truck fleet (6 units)", "1200", "Motor vehicles", "North America Drilling",
     "2022-09-15", 1_320_000, 180_000, 84, "Straight line", None, None, 560_000, "In use", None, None),
    ("FA-1031", "Light vehicle fleet (14 units)", "3100", "Motor vehicles", "LATAM Mining Services",
     "2023-05-01", 690_000, 95_000, 60, "Straight line", None, None, 260_000, "In use", None, None),
    ("FA-1040", "Regional workshop & warehouse", "2100", "Buildings & infrastructure", "APAC Products",
     "2018-01-10", 6_400_000, 900_000, 480, "Straight line", None, None, 1_237_500, "In use", None, None),
    ("FA-1041", "Camp accommodation modules", "3100", "Buildings & infrastructure", "LATAM Mining Services",
     "2021-11-30", 1_150_000, 80_000, 120, "Straight line", None, None, 356_000, "In use", None, None),
    ("FA-1050", "ERP infrastructure & servers", "2200", "IT & office equipment", "APAC Shared Services",
     "2023-01-15", 520_000, 20_000, 60, "Straight line", None, None, 268_000, "In use", None, None),
    ("FA-1051", "Field data capture tablets & telemetry", "1100", "IT & office equipment",
     "North America Drilling", "2024-04-01", 285_000, 10_000, 36, "Straight line", None, None, 138_000, "In use", None, None),
    ("FA-1060", "Rig mobilisation — capital work in progress", "3100", "Capital work in progress",
     "LATAM Mining Services", "2026-02-01", 1_280_000, 0, None, "Straight line", None, None, 0, "Under construction", None, None),
]


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------
# (entity, sku, description, category, qty, unit_cost, selling_price,
#  cost_to_complete, cost_to_sell, ageing_days, existing_provision)
INVENTORY = [
    ("2100", "BIT-PQ-STD", "PQ impregnated diamond bit — standard series", "Finished goods",
     1_450, 640.00, 890.00, 0, 42.00, 95, 0),
    ("2100", "BIT-HQ-STD", "HQ impregnated diamond bit — standard series", "Finished goods",
     2_100, 520.00, 715.00, 0, 35.00, 70, 0),
    ("2100", "BIT-NQ-LEG", "NQ bit — legacy matrix, superseded design", "Finished goods",
     880, 495.00, 410.00, 0, 30.00, 540, 38_000),
    ("2100", "REAM-PQ", "PQ reaming shell", "Finished goods", 640, 310.00, 455.00, 0, 18.00, 120, 0),
    ("2100", "CORE-BRL-3M", "Core barrel assembly — 3 metre", "Finished goods",
     310, 1_850.00, 2_480.00, 0, 95.00, 80, 0),
    ("2100", "RAW-DIA-SYN", "Synthetic diamond grit — feedstock", "Raw materials",
     5_600, 88.00, 0.00, 0, 0, 150, 0),
    ("2100", "RAW-MATRIX", "Matrix powder blend", "Raw materials", 12_400, 34.00, 0.00, 0, 0, 210, 0),
    ("1100", "ROD-NQ-3M", "NQ drill rod — 3 metre", "Consumables", 3_200, 285.00, 0.00, 0, 0, 60, 0),
    ("1100", "ROD-HQ-3M", "HQ drill rod — 3 metre", "Consumables", 2_450, 340.00, 0.00, 0, 0, 55, 0),
    ("1100", "CAS-HW-6M", "HW casing — 6 metre", "Consumables", 780, 610.00, 0.00, 0, 0, 190, 0),
    ("1100", "SPR-PUMP-KIT", "Mud pump rebuild kit", "Spare parts", 145, 4_250.00, 0.00, 0, 0, 400, 0),
    ("1100", "SPR-LF160-OBS", "LF-160 head assembly — obsolete revision", "Spare parts",
     38, 12_400.00, 6_800.00, 0, 450.00, 820, 95_000),
    ("3100", "CON-GROUT", "Grout & drilling additives", "Consumables", 9_400, 46.00, 0.00, 0, 0, 75, 0),
    ("3100", "SPR-S250-MISC", "S250 rig spares — mixed", "Spare parts", 260, 2_150.00, 0.00, 0, 0, 380, 0),
    ("3100", "CON-FUEL-BULK", "Bulk diesel — site storage", "Consumables", 180_000, 1.12, 0.00, 0, 0, 20, 0),
]


# --------------------------------------------------------------------------
# Provisions
# --------------------------------------------------------------------------
# (entity, ref, category, description, opening, additions, utilised, unused_rev,
#  unwind, fx, rate, settle, basis, evidence)
PROVISIONS = [
    ("3100", "PRV-REH-001", "Site rehabilitation / environmental",
     "Atacama drill pad rehabilitation and water treatment obligation",
     4_850_000, 180_000, 95_000, 0, 62_000, -118_000, 0.041, "2031-12-31",
     "Present obligation — probable outflow, reliably estimable (provide)",
     "Environmental permit CL-2021-884; closure cost study by Ramboll dated 2026-03"),
    ("3100", "PRV-CLO-002", "Mine closure",
     "Camp decommissioning and land reinstatement at the Copiapó site",
     1_920_000, 0, 0, 45_000, 24_000, -47_000, 0.041, "2029-06-30",
     "Present obligation — probable outflow, reliably estimable (provide)",
     "Closure plan approved by SERNAGEOMIN; internal cost model v4"),
    ("1100", "PRV-REH-003", "Site rehabilitation / environmental",
     "Nevada exploration pad reinstatement obligations",
     680_000, 42_000, 31_000, 0, 8_500, 0, 0.038, "2028-12-31",
     "Present obligation — probable outflow, reliably estimable (provide)",
     "BLM reclamation bond schedule; site register extract 2026-08"),
    ("2100", "PRV-WAR-004", "Warranty",
     "12-month product warranty on manufactured bits and reaming shells",
     720_000, 118_000, 96_000, 22_000, 0, -18_000, None, "2027-06-30",
     "Present obligation — probable outflow, reliably estimable (provide)",
     "Warranty claims history 24 months; 1.4% of rolling revenue"),
    ("1200", "PRV-LEG-005", "Legal claim",
     "Contract dispute with a Canadian mining client over demobilisation costs",
     450_000, 0, 0, 0, 0, -9_000, None, "2027-03-31",
     "Present obligation — probable outflow, reliably estimable (provide)",
     "External counsel opinion dated 2026-05-18; probable outflow assessed"),
    ("2200", "PRV-RES-006", "Restructuring",
     "Shared services relocation — announced and communicated to affected staff",
     0, 385_000, 112_000, 0, 0, 6_000, None, "2027-03-31",
     "Present obligation — probable outflow, reliably estimable (provide)",
     "Board approval 2026-06-11; staff communication 2026-06-20; detailed plan attached"),
    ("1100", "PRV-CON-007", "Legal claim",
     "Employee injury claim — liability contested, outcome uncertain",
     0, 0, 0, 0, 0, 0, None, "2028-12-31",
     "Possible obligation — disclose as contingent liability",
     "Counsel assesses outflow as possible but not probable; disclosed only"),
    ("2100", "PRV-ONE-008", "Onerous contract",
     "Loss-making supply contract for legacy NQ bit series",
     0, 96_000, 0, 0, 0, -2_400, None, "2027-09-30",
     "Present obligation — probable outflow, reliably estimable (provide)",
     ""),
]


# --------------------------------------------------------------------------
# Intercompany documents
# --------------------------------------------------------------------------
# (doc_ref, entity, counterparty, account, ccy, amount_txn, hist_rate, booked_group_override)
def intercompany_rows(period: str, fx: dict) -> list[tuple]:
    """Build a deliberately imperfect intercompany ledger for the period."""
    cad, aud, sgd, clp = (fx["CAD"][0], fx["AUD"][0], fx["SGD"][0], fx["CLP"][0])
    return [
        # --- clean match, same currency, both sides at the same rate
        ("IC-4411", "1100", "1200", "1450", "USD", 1_250_000.00, 1.0, 1_250_000.00,
         "Management recharge — North America drilling support"),
        ("IC-4411", "1200", "1100", "2100", "USD", -1_250_000.00, 1.0, -1_250_000.00,
         "Management recharge — North America drilling support"),

        # --- FX-only break: both sides agree in AUD, but the APAC side has not yet
        #     retranslated at the closing rate, so only the carrying amounts differ
        ("IC-4412", "1100", "2100", "1450", "AUD", 1_340_000.00, aud,
         round(1_340_000.00 / aud, 2), "Bit and consumable supply — APAC to US"),
        ("IC-4412", "2100", "1100", "2100", "AUD", -1_340_000.00, 1.498,
         round(-1_340_000.00 / 1.498, 2), "Bit and consumable supply — APAC to US"),

        # --- FX-only break in AUD: both sides agree in AUD, different translation rates
        ("IC-4415", "2100", "2200", "1450", "AUD", 1_420_000.00, 1.498,
         round(1_420_000.00 / 1.498, 2), "Shared services allocation — APAC"),
        ("IC-4415", "2200", "2100", "2100", "AUD", -1_420_000.00, aud,
         round(-1_420_000.00 / aud, 2), "Shared services allocation — APAC"),

        # --- timing difference: booked by Chile, not yet by the US
        ("IC-4418", "3100", "1100", "1450", "CLP", 412_000_000.00, clp,
         round(412_000_000.00 / clp, 2), "Rig mobilisation recharge — invoiced 2 days before cut-off"),

        # --- timing difference the other way
        ("IC-4419", "1200", "2200", "2100", "CAD", -298_000.00, cad,
         round(-298_000.00 / cad, 2), "IT platform recharge — counterparty posts next period"),

        # --- true mismatch: sides disagree in the transaction currency itself
        ("IC-4421", "1100", "3100", "1450", "USD", 640_000.00, 1.0, 640_000.00,
         "Equipment transfer — quantity dispute"),
        ("IC-4421", "3100", "1100", "2100", "USD", -595_000.00, 1.0, -595_000.00,
         "Equipment transfer — quantity dispute"),

        # --- cross-currency pair: USD invoice settled through an SGD treasury account
        ("IC-4424", "2200", "1100", "1450", "SGD", 1_075_000.00, sgd,
         round(1_075_000.00 / sgd, 2), "Treasury funding leg — Singapore"),
        ("IC-4424", "1100", "2200", "2100", "USD", -804_000.00, 1.0, -804_000.00,
         "Treasury funding leg — Singapore"),

        # --- clean CAD match
        ("IC-4427", "1200", "1100", "1450", "CAD", 515_000.00, cad,
         round(515_000.00 / cad, 2), "Labour secondment recharge"),
        ("IC-4427", "1100", "1200", "2100", "CAD", -515_000.00, cad,
         round(-515_000.00 / cad, 2), "Labour secondment recharge"),

        # --- small FX break on a CLP balance
        ("IC-4430", "3100", "2100", "2100", "CLP", -186_000_000.00, 958.0,
         round(-186_000_000.00 / 958.0, 2), "Spare parts purchase — LATAM from APAC"),
        ("IC-4430", "2100", "3100", "1450", "CLP", 186_000_000.00, clp,
         round(186_000_000.00 / clp, 2), "Spare parts purchase — LATAM from APAC"),
    ]


# ==========================================================================
# Seeding
# ==========================================================================
def seed_all(conn: sqlite3.Connection, today: date | None = None) -> dict:
    """Populate an empty database. Idempotent guard: does nothing if seeded."""
    if db.is_seeded(conn):
        return {"seeded": False, "message": "Database already contains data."}

    today = today or date.today()
    periods = periods_for_today(today)
    counts: dict[str, int] = {}

    counts["accounts"] = _seed_accounts(conn)
    counts["fx_rates"] = _seed_fx(conn, periods)
    counts["trial_balance"] = _seed_trial_balance(conn, periods)
    counts["milestones"] = _seed_milestones(conn, today)
    counts["close_tasks"] = _seed_close(conn, periods, today)
    counts["journals"] = _seed_journals(conn, periods)
    counts["intercompany"] = _seed_intercompany(conn, periods)
    counts["reconciliations"] = _seed_recon(conn, periods)
    counts["ppe"] = _seed_ppe(conn)
    counts["inventory"] = _seed_inventory(conn, periods)
    counts["provisions"] = _seed_provisions(conn, periods)
    counts["controls"] = _seed_controls(conn, today)
    counts["flux_commentary"] = _seed_commentary(conn, periods)

    db.set_setting(conn, "seeded_at", datetime.now().isoformat(timespec="seconds"))
    db.set_setting(conn, "active_period", periods[-1])
    audit.record(conn, SEED_ACTOR, "system.seed", "database", "demo",
                 {"periods": periods, **counts})
    return {"seeded": True, "periods": periods, "counts": counts}


def _seed_accounts(conn: sqlite3.Connection) -> int:
    rows = [
        (no, desc, typ, stmt, recon, ACCOUNT_OWNERS.get(no[0], "m.buenaventura"), std)
        for no, desc, typ, stmt, recon, std in ACCOUNTS
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO accounts(account_no, description, account_type, statement,"
        " recon_required, owner, ifrs_standard) VALUES (?,?,?,?,?,?,?)", rows,
    )
    conn.commit()
    return len(rows)


def _seed_fx(conn: sqlite3.Connection, periods: list[str]) -> int:
    n = 0
    for i, p in enumerate(periods):
        for ccy, (closing, avg) in FX_TABLE[i].items():
            conn.execute(
                "INSERT OR REPLACE INTO fx_rates(period, currency, closing_rate, average_rate)"
                " VALUES (?,?,?,?)", (p, ccy, closing, avg),
            )
            n += 1
    conn.commit()
    return n


def _seed_trial_balance(conn: sqlite3.Connection, periods: list[str]) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    n = 0
    for pi, period in enumerate(periods):
        for ent_code in TB_ENTITIES:
            ent = config.ENTITY_BY_CODE[ent_code]
            scale = ENTITY_SCALE[ent_code]
            rate = FX_TABLE[pi][ent.functional_ccy][0]
            rows: list[tuple] = []
            running_group = 0.0

            for no, desc, typ, stmt, recon, std in ACCOUNTS:
                if no == "3100":
                    continue  # retained earnings is the balancing plug
                base = BASE_BALANCES.get(no, 0)
                if base == 0:
                    continue
                drift = 1.0 + (pi * 0.014) + RNG.uniform(-0.035, 0.035)
                mult = DRIVERS.get(no, {}).get(pi, 1.0)
                local = base * scale * drift * mult * rate
                # keep figures readable
                local = round(local, 2)
                group = round(local / rate, 2)
                budget_group = round(group * RNG.uniform(0.93, 1.07), 2)
                running_group += group
                rows.append((period, ent_code, no, ent.functional_ccy, local, group,
                             budget_group, "SAP FAGLL03", now))

            # plug retained earnings so the TB nets to nil in group currency
            plug_group = round(-running_group, 2)
            rows.append((period, ent_code, "3100", ent.functional_ccy,
                         round(plug_group * rate, 2), plug_group,
                         round(plug_group * RNG.uniform(0.97, 1.03), 2),
                         "SAP FAGLL03", now))

            conn.executemany(
                "INSERT OR REPLACE INTO trial_balance(period, entity, account_no, currency,"
                " amount_local, amount_group, budget_group, source, loaded_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)", rows,
            )
            n += len(rows)
    conn.commit()
    return n


def _seed_milestones(conn: sqlite3.Connection, today: date) -> int:
    # Start date ~5 weeks ago: phase 1 complete, phase 2 in flight.
    start = today - timedelta(days=36)
    db.set_setting(conn, "start_date", start.isoformat())
    elapsed = 36

    rows = []
    for phase, workstream, objective, measure, due_day in MILESTONES:
        if due_day <= elapsed - 6:
            status, pct = "Complete", 100
        elif due_day <= elapsed + 2:
            status, pct = RNG.choice(
                [("In progress", 70), ("In progress", 45), ("Complete", 100), ("Blocked", 30)]
            )
        elif due_day <= elapsed + 30:
            status, pct = RNG.choice([("In progress", 25), ("Not started", 0), ("Not started", 0)])
        else:
            status, pct = "Not started", 0
        owner = "m.buenaventura" if phase in ("P1", "P2", "P3") else RNG.choice(
            ["m.buenaventura", "m.buenaventura", "l.tan", "a.reyes"]
        )
        evidence = None
        if status == "Complete":
            evidence = "Evidence filed in the close pack / control file."
        rows.append((phase, workstream, objective, measure, due_day, status, pct, owner,
                     evidence, None, datetime.now().isoformat(timespec="seconds")))

    conn.executemany(
        "INSERT INTO milestones(phase, workstream, objective, success_measure, due_day,"
        " status, progress_pct, owner, evidence, notes, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows,
    )
    conn.commit()
    return len(rows)


def _seed_close(conn: sqlite3.Connection, periods: list[str], today: date) -> int:
    n = 0
    for i, period in enumerate(periods):
        close_svc.ensure_period(conn, period)
        is_active = (i == len(periods) - 1)

        if is_active:
            # Put the active close window around today so the demo reads as live.
            wd1 = close_svc.add_business_days(today, -1)
            target = close_svc.add_business_days(wd1, config.CLOSE_SLA_DAYS - 1)
            conn.execute(
                "UPDATE close_periods SET close_start = ?, target_close = ?, status='Open',"
                " actual_close = NULL WHERE period = ?",
                (wd1.isoformat(), target.isoformat(), period),
            )
        else:
            row = db.fetch_one(conn, "SELECT target_close FROM close_periods WHERE period=?",
                               (period,))
            target = date.fromisoformat(row["target_close"])
            actual = target + timedelta(days=(1 if i == 0 else 0))
            conn.execute(
                "UPDATE close_periods SET status='Closed', actual_close=? WHERE period=?",
                (actual.isoformat(), period),
            )

        for workday, stream, task, control_ref in CLOSE_TEMPLATE:
            if is_active:
                idx = config.CLOSE_WORKDAYS.index(workday)
                # everything up to WD+1 done, WD+2 partly done, WD+3 open
                if idx <= 2:
                    status = "Complete"
                elif idx == 3:
                    status = RNG.choice(["Complete", "In progress", "Complete", "Blocked"])
                else:
                    status = "Not started"
            else:
                status = "Complete"
            blocker = ("Awaiting intercompany confirmation from 2200 — chased 2 days ago"
                       if status == "Blocked" else None)
            completed = (datetime.now().isoformat(timespec="seconds")
                         if status == "Complete" else None)
            owner = RNG.choice(["a.reyes", "j.okafor", "m.buenaventura"])
            reviewer = RNG.choice(["l.tan", "c.moreno", "m.buenaventura"])
            conn.execute(
                "INSERT INTO close_tasks(period, workday, stream, task, entity, owner,"
                " reviewer, control_ref, status, completed_at, blocker)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (period, workday, stream, task, "All", owner, reviewer, control_ref,
                 status, completed, blocker),
            )
            n += 1
    conn.commit()
    return n


def _seed_journals(conn: sqlite3.Connection, periods: list[str]) -> int:
    from .services import journals as je_svc

    active = periods[-1]
    prior = periods[-2]

    specs = [
        # (period, entity, description, type, risk_tag, ccy, lines, final_state)
        (active, "1100",
         "Accrual for unbilled drilling consumables received at the Nevada site prior to cut-off",
         "Accrual", "Cut-off sensitive", "USD",
         [("7020", 428_500, 0), ("2050", 0, 428_500)], "posted"),
        (active, "3100",
         "Remeasurement of the Atacama rehabilitation provision following the revised closure study",
         "Provision", "Estimate / Judgement", "USD",
         [("7450", 180_000, 0), ("2400", 0, 180_000)], "pending"),
        (active, "2100",
         "Inventory write-down of the legacy NQ bit series to net realisable value under IAS 2",
         "Provision", "Estimate / Judgement", "USD",
         [("7150", 112_800, 0), ("1390", 0, 112_800)], "pending"),
        (active, "1100",
         "Reclassification of contract assets misposted to trade receivables during the period",
         "Reclassification", "Routine", "USD",
         [("1200", 645_000, 0), ("1100", 0, 645_000)], "approved"),
        (active, "2200",
         "Restructuring provision for the announced Singapore shared services relocation plan",
         "Provision", "Estimate / Judgement", "USD",
         [("7300", 385_000, 0), ("2420", 0, 385_000)], "draft"),
        (active, "1200",
         "Top-side adjustment to align the Canadian result with the group consolidation pack",
         "Correction", "Top-side", "USD",
         [("7350", 1_240_000, 0), ("2050", 0, 1_240_000)], "pending"),
        (prior, "2100",
         "Monthly depreciation charge for the APAC manufacturing asset base per the register",
         "Standard", "Routine", "USD",
         [("7200", 38_542, 0), ("1650", 0, 38_542)], "posted"),
        (prior, "3100",
         "IAS 21 retranslation of intercompany monetary balances at the period closing rate",
         "FX revaluation", "FX revaluation", "USD",
         [("7810", 96_400, 0), ("1450", 0, 96_400)], "posted"),
        (prior, "1100",
         "Reversal of the prior period consumables accrual on receipt of the supplier invoice",
         "Reversal", "Routine", "USD",
         [("2050", 391_200, 0), ("7020", 0, 391_200)], "posted"),
        (active, "1200",
         "Warranty provision movement based on the rolling twenty-four month claims experience",
         "Provision", "Estimate / Judgement", "USD",
         [("7100", 96_000, 0), ("2410", 0, 96_000)], "rejected"),
    ]

    n = 0
    for period, entity, desc, jtype, risk, ccy, lines, state in specs:
        preparer = RNG.choice(["a.reyes", "j.okafor"])
        entry_lines = [
            {"account_no": a, "debit": d, "credit": c, "cost_center": "CC-" + entity,
             "memo": desc[:60]}
            for a, d, c in lines
        ]
        ref = je_svc.create(conn, preparer, period, entity, desc, jtype, risk, ccy, entry_lines)
        n += 1

        # judgemental entries need support in the vault before they can be approved
        if risk in je_svc.JUDGEMENTAL_TAGS:
            je_svc.attach_document(
                conn, ref, preparer, f"{ref}_support.txt",
                f"Supporting calculation for {ref}\n{desc}\n"
                f"Prepared by {preparer} on {date.today()}.\n"
                "Basis, inputs and management assumptions documented in the close pack.\n".encode(),
                doc_type="Calculation",
            )

        if state == "draft":
            continue
        je_svc.submit(conn, ref, preparer)
        if state == "pending":
            continue

        reviewer = "l.tan" if preparer != "l.tan" else "c.moreno"
        if state == "rejected":
            je_svc.review(conn, ref, reviewer, "Reject",
                          "Claims data supports a lower rate — resubmit using the 24-month "
                          "experience rather than the 12-month window.")
            continue

        result = je_svc.review(conn, ref, reviewer, "Approve")
        if result.get("controller_required"):
            je_svc.controller_approve(conn, ref, "m.buenaventura")
        if state == "posted":
            je_svc.post(conn, ref, "m.buenaventura")
    return n


def _seed_intercompany(conn: sqlite3.Connection, periods: list[str]) -> int:
    period = periods[-1]
    fx = FX_TABLE[len(periods) - 1]
    rows = intercompany_rows(period, fx)
    n = 0
    for doc_ref, entity, cp, account, ccy, amt, hist, booked, desc in rows:
        conn.execute(
            "INSERT INTO intercompany(period, doc_ref, entity, counterparty, account_no,"
            " currency, amount_txn, historical_rate, booked_group, posting_date, description)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (period, doc_ref, entity, cp, account, ccy, amt, hist, booked,
             (date.today() - timedelta(days=RNG.randint(3, 25))).isoformat(), desc),
        )
        n += 1
    conn.commit()
    return n


def _seed_recon(conn: sqlite3.Connection, periods: list[str]) -> int:
    period = periods[-1]
    created = recon_svc.ensure_schedule(conn, period, SEED_ACTOR)

    recs = db.query(conn, "SELECT * FROM reconciliations WHERE period = ?", (period,))
    for r in recs.itertuples():
        # sub-ledger agrees for most accounts; a handful carry reconciling items
        noise = RNG.random()
        if noise < 0.68:
            sub = r.gl_balance
        elif noise < 0.88:
            sub = round(r.gl_balance - RNG.uniform(5_000, 120_000), 2)
        else:
            sub = round(r.gl_balance - RNG.uniform(150_000, 900_000), 2)
        conn.execute("UPDATE reconciliations SET subledger_balance = ? WHERE id = ?",
                     (sub, r.id))
        diff = round(r.gl_balance - sub, 2)
        if abs(diff) > recon_svc.TOLERANCE:
            # explain most of it with dated reconciling items
            explained = round(diff * RNG.uniform(0.7, 1.0), 2)
            age = RNG.choice([4, 9, 15, 22, 38, 61])
            conn.execute(
                "INSERT INTO recon_items(recon_id, item_ref, description, amount, item_date,"
                " category) VALUES (?,?,?,?,?,?)",
                (r.id, f"RI-{r.id:04d}",
                 RNG.choice([
                     "Deposit in transit — cleared the following business day",
                     "Supplier invoice received after the sub-ledger cut-off",
                     "Timing: goods receipt posted ahead of the invoice",
                     "Bank charge not yet reflected in the sub-ledger",
                     "Unapplied customer receipt pending allocation",
                     "Site float top-up in transit",
                 ]),
                 explained, (date.today() - timedelta(days=age)).isoformat(),
                 RNG.choice(["Timing", "In transit", "Timing", "Unidentified"])),
            )
    conn.commit()

    # progress a realistic proportion through preparation and review
    recs = db.query(conn, "SELECT id, preparer FROM reconciliations WHERE period = ?", (period,))
    for r in recs.itertuples():
        roll = RNG.random()
        if roll < 0.45:
            recon_svc.prepare(conn, int(r.id), RNG.choice(["a.reyes", "j.okafor"]))
            recon_svc.review(conn, int(r.id), "l.tan")
        elif roll < 0.75:
            recon_svc.prepare(conn, int(r.id), RNG.choice(["a.reyes", "j.okafor"]))
    return created


def _seed_ppe(conn: sqlite3.Connection) -> int:
    for row in PPE:
        (asset_no, desc, entity, cls, cgu, acq, cost, residual, life, method,
         total_units, units_todate, accum_dep, status, recoverable, indicator) = row
        conn.execute(
            "INSERT OR REPLACE INTO ppe_register(asset_no, description, entity, asset_class,"
            " cgu, acquisition_date, cost, residual_value, useful_life_m, method, total_units,"
            " units_to_date, accum_dep, accum_impairment, status, recoverable_amt,"
            " indicator_notes, last_reviewed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,NULL)",
            (asset_no, desc, entity, cls, cgu, acq, cost, residual, life, method,
             total_units, units_todate, accum_dep, status, recoverable, indicator),
        )
    conn.commit()
    return len(PPE)


def _seed_inventory(conn: sqlite3.Connection, periods: list[str]) -> int:
    period = periods[-1]
    for (entity, sku, desc, cat, qty, cost, price, ctc, cts, ageing, prov) in INVENTORY:
        conn.execute(
            "INSERT OR REPLACE INTO inventory_valuation(period, entity, sku, description,"
            " category, quantity, unit_cost, selling_price, cost_to_complete, cost_to_sell,"
            " ageing_days, existing_provision) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (period, entity, sku, desc, cat, qty, cost, price, ctc, cts, ageing, prov),
        )
    conn.commit()
    return len(INVENTORY)


def _seed_provisions(conn: sqlite3.Connection, periods: list[str]) -> int:
    period = periods[-1]
    for (entity, ref, cat, desc, opening, add, used, unused, unwind, fx_mv,
         rate, settle, basis, evidence) in PROVISIONS:
        conn.execute(
            "INSERT OR REPLACE INTO provisions(period, entity, provision_ref, category,"
            " description, opening, additions, utilised, unused_reversed, unwind_discount,"
            " fx_movement, discount_rate, expected_settle, recognition_basis, evidence)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (period, entity, ref, cat, desc, opening, add, used, unused, unwind, fx_mv,
             rate, settle, basis, evidence or None),
        )
    conn.commit()
    return len(PROVISIONS)


def _seed_controls(conn: sqlite3.Connection, today: date) -> int:
    for ref, name, standard, freq, assertion in config.CONTROL_LIBRARY:
        roll = RNG.random()
        if roll < 0.6:
            last = (today - timedelta(days=RNG.randint(3, 25))).isoformat()
            result = "Pass"
            evidence = "Sample tested and evidence filed in the control file."
        elif roll < 0.8:
            last = (today - timedelta(days=RNG.randint(60, 140))).isoformat()
            result = "Pass"
            evidence = "Prior-quarter test; refresh due."
        elif roll < 0.9:
            last = (today - timedelta(days=RNG.randint(10, 30))).isoformat()
            result = "Fail"
            evidence = "Exception noted — remediation plan open."
        else:
            last, result, evidence = None, None, None
        conn.execute(
            "INSERT OR REPLACE INTO controls(control_ref, name, standard, frequency,"
            " assertion, owner, last_tested, test_result, evidence) VALUES (?,?,?,?,?,?,?,?,?)",
            (ref, name, standard, freq, assertion, "m.buenaventura", last, result, evidence),
        )
    conn.commit()
    return len(config.CONTROL_LIBRARY)


def _seed_commentary(conn: sqlite3.Connection, periods: list[str]) -> int:
    """Explain a few of the designed flux breaches so coverage is partial, not nil."""
    from .services import flux as flux_svc

    period = periods[-1]
    prepared = [
        ("1100", "7020", "Volume / activity",
         "Consumable and drill bit consumption rose with the deep-hole programme at the Nevada "
         "site. Metres drilled increased 34% against the prior period; consumption per metre is "
         "flat, so the movement is volume-driven and not a costing issue."),
        ("3100", "7450", "One-off / non-recurring",
         "Environmental remediation works at the Copiapó site were accelerated to meet the "
         "SERNAGEOMIN permit deadline. The spend is within the approved closure study and does "
         "not indicate a change in the total rehabilitation obligation."),
        ("1100", "1200", "Timing / cut-off",
         "Contract assets increased because three progress claims totalling 1.8m were certified "
         "after the invoicing cut-off. All three were invoiced in the first week of the "
         "following period and have since been collected within terms."),
    ]
    n = 0
    for entity, account, driver, text in prepared:
        res = flux_svc.add_commentary(
            conn, "a.reyes", period, entity, account, "PoP", text, driver,
            action="No action required — monitored in the following period.",
        )
        if res["ok"]:
            n += 1
    return n
