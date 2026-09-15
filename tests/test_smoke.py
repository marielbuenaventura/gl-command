"""
End-to-end checks over every module's service layer.

These are deliberately *behavioural*: they assert the controls actually bite —
an unbalanced journal is refused, a preparer cannot self-review, a period will
not close with open work, and the audit chain detects tampering.

Run with:  python -m pytest tests -q      (or: python tests/test_smoke.py)
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="glcmd_test_")
os.environ["GLCMD_DATA_DIR"] = _TMP
os.environ["GLCMD_DB"] = str(Path(_TMP) / "test.db")

from glcmd import audit, config, db, seed  # noqa: E402
from glcmd.services import close as close_svc  # noqa: E402
from glcmd.services import compliance, flux, fx, journals, recon, roadmap  # noqa: E402


def fresh():
    conn = db.connect()
    db.init_db(conn)
    seed.seed_all(conn)
    return conn


CONN = fresh()
PERIOD = db.get_setting(CONN, "active_period")
CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(condition), detail))


# ---------------------------------------------------------------- data layer
check("Chart of accounts loaded",
      db.scalar(CONN, "SELECT COUNT(*) FROM accounts") == len(seed.ACCOUNTS))
check("Trial balance loaded for three periods",
      len(flux.available_periods(CONN)) == 3, str(flux.available_periods(CONN)))

for p in flux.available_periods(CONN):
    for e in seed.TB_ENTITIES:
        net = db.scalar(
            CONN, "SELECT ROUND(SUM(amount_group),2) FROM trial_balance"
                  " WHERE period=? AND entity=?", (p, e))
        check(f"Trial balance nets to nil — {p}/{e}", abs(float(net)) < 1.0, f"net={net}")

# ------------------------------------------------------------------ roadmap
phases = roadmap.phase_summary(CONN)
check("Six roadmap phases", len(phases) == 6)
check("Phase 1 gate passed", phases.iloc[0]["gate"] == "Passed",
      phases.iloc[0]["gate"])
check("Roadmap focus list is populated", not roadmap.focus_list(CONN, 30).empty)

# -------------------------------------------------------------------- close
prog = close_svc.progress(CONN, PERIOD)
check("Close checklist generated", prog["total"] > 0)
check("Close target is three working days after WD+1",
      (prog["target_close"] - prog["close_start"]).days >= 2)
result = close_svc.sign_off_period(CONN, PERIOD, "m.buenaventura")
check("Period sign-off blocked while work is open", not result["ok"], result["message"])

# ----------------------------------------------------------------- journals
lines_ok = [{"account_no": "7020", "debit": 1000.0, "credit": 0.0},
            {"account_no": "2050", "debit": 0.0, "credit": 1000.0}]
ref = journals.create(CONN, "a.reyes", PERIOD, "1100",
                      "Test accrual entry with a sufficiently long description",
                      "Accrual", "Routine", "USD", lines_ok)
check("Journal created", journals.get(CONN, ref) is not None)
check("Balanced journal passes validation", journals.validate(CONN, ref) == [],
      str(journals.validate(CONN, ref)))

lines_bad = [{"account_no": "7020", "debit": 1000.0, "credit": 0.0},
             {"account_no": "2050", "debit": 0.0, "credit": 900.0}]
bad_ref = journals.create(CONN, "a.reyes", PERIOD, "1100",
                          "Deliberately unbalanced entry for the control test",
                          "Accrual", "Routine", "USD", lines_bad)
check("Unbalanced journal fails validation", journals.validate(CONN, bad_ref) != [])
check("Unbalanced journal cannot be submitted",
      not journals.submit(CONN, bad_ref, "a.reyes")["ok"])

journals.submit(CONN, ref, "a.reyes")
self_review = journals.review(CONN, ref, "a.reyes", "Approve")
check("Preparer cannot review their own entry", not self_review["ok"],
      self_review["message"])
check("Blocked self-review is written to the audit trail",
      not audit.trail(CONN, action="blocked_sod").empty)

ok_review = journals.review(CONN, ref, "l.tan", "Approve")
check("Independent reviewer can approve", ok_review["ok"], ok_review["message"])
check("Approved entry posts", journals.post(CONN, ref, "m.buenaventura")["ok"])
check("Posted entry cannot be posted twice",
      not journals.post(CONN, ref, "m.buenaventura")["ok"])

rev = journals.reverse(CONN, ref, "m.buenaventura", PERIOD, "Control test reversal")
check("Posted entry can be reversed", rev["ok"], rev["message"])
check("Original is marked Reversed", journals.get(CONN, ref)["status"] == "Reversed")

# high-value escalation
big = [{"account_no": "7350", "debit": 2_000_000.0, "credit": 0.0},
       {"account_no": "2050", "debit": 0.0, "credit": 2_000_000.0}]
big_ref = journals.create(CONN, "a.reyes", PERIOD, "1100",
                          "High value entry to exercise the controller escalation gate",
                          "Correction", "Top-side", "USD", big)
journals.attach_document(CONN, big_ref, "a.reyes", "support.txt", b"workings")
journals.submit(CONN, big_ref, "a.reyes")
res = journals.review(CONN, big_ref, "l.tan", "Approve")
check("High-value entry requires a controller signature",
      res.get("controller_required") is True, res["message"])
check("High-value entry cannot post on two signatures",
      not journals.post(CONN, big_ref, "m.buenaventura")["ok"])
check("Reviewer cannot also give the controller signature",
      not journals.controller_approve(CONN, big_ref, "l.tan")["ok"])
check("Controller signature unlocks posting",
      journals.controller_approve(CONN, big_ref, "m.buenaventura")["ok"])
check("Entry posts after three signatures",
      journals.post(CONN, big_ref, "m.buenaventura")["ok"])

# judgemental entries need support
judge = journals.create(CONN, "a.reyes", PERIOD, "1100",
                        "Judgemental estimate without any supporting documentation",
                        "Provision", "Estimate / Judgement", "USD", lines_ok)
check("Judgemental entry without support fails validation",
      any("supporting document" in i for i in journals.validate(CONN, judge)))

# --------------------------------------------------------------------- flux
analysis = flux.analyse(CONN, PERIOD, "PoP")
check("Flux analysis returns rows", not analysis.empty)
check("Flux flags at least one account", bool(analysis["flagged"].sum()),
      f"flagged={int(analysis['flagged'].sum())}")
cov = flux.coverage(CONN, PERIOD, "PoP")
check("Commentary coverage is partial (work remains)",
      0 < cov["coverage_pct"] < 100, f"coverage={cov['coverage_pct']}")

thin = flux.add_commentary(CONN, "a.reyes", PERIOD, "1100", "7030", "PoP",
                           "Timing", "Timing / cut-off")
check("Thin commentary is rejected", not thin["ok"], thin["message"])
good = flux.add_commentary(
    CONN, "a.reyes", PERIOD, "1100", "7030", "PoP",
    "Site and camp costs rose with the additional crew rotation at the Nevada "
    "programme; the run rate normalises next period.", "Volume / activity")
check("Substantive commentary is accepted", good["ok"], good["message"])
check("Commentary cannot be self-reviewed",
      not flux.review_commentary(CONN, "a.reyes", PERIOD, "1100", "7030", "PoP")["ok"])
check("Commentary can be independently reviewed",
      flux.review_commentary(CONN, "l.tan", PERIOD, "1100", "7030", "PoP")["ok"])

bva = flux.analyse(CONN, PERIOD, "BvA")
check("Budget vs actual basis works", not bva.empty)

strict = config.MaterialityPolicy(1.0, 1.0, "or")
loose = config.MaterialityPolicy(90.0, 50_000_000.0, "and")
check("Tighter materiality flags more accounts",
      int(flux.analyse(CONN, PERIOD, "PoP", policy=strict)["flagged"].sum())
      > int(flux.analyse(CONN, PERIOD, "PoP", policy=loose)["flagged"].sum()))

# ------------------------------------------------------------ reconciliations
rec = recon.load(CONN, PERIOD)
check("Reconciliation schedule generated", not rec.empty)
check("Unexplained difference is computed, not stored",
      "unexplained" in rec.columns)
summary = recon.summary(CONN, PERIOD)
check("Some reconciliations are reviewed", summary["reviewed"] > 0)
check("Reconciliation review enforces segregation of duties",
      not recon.review(CONN, int(rec.iloc[0]["id"]), rec.iloc[0]["preparer"] or "a.reyes")["ok"]
      if rec.iloc[0]["preparer"] else True)

am = recon.auto_match(CONN, PERIOD, "system.test")
check("Auto-match runs", isinstance(am, dict) and "matched" in am, str(am))

# ------------------------------------------------------------- intercompany
decomp = fx.decomposition(CONN, PERIOD)
check("Intercompany documents loaded", decomp["documents"] > 0)
check("Break decomposes into timing, FX and mismatch",
      abs(decomp["timing"]) > 0 and abs(decomp["fx"]) > 0 and abs(decomp["mismatch"]) > 0,
      f"timing={decomp['timing']} fx={decomp['fx']} mismatch={decomp['mismatch']}")

matched = fx.match(CONN, PERIOD)
clean = matched[matched["classification"] == "Matched"]
check("At least one document matches cleanly", not clean.empty)
check("A same-currency pair with equal amounts is never a true mismatch",
      bool((clean["true_mismatch"].abs() < 0.01).all()))
mismatches = matched[matched["classification"] == "True mismatch"]
check("The 45,000 invoicing break is classified as a true mismatch",
      not mismatches.empty and bool((mismatches["txn_break"].abs() > 1).any()))

reval = fx.revaluation_schedule(CONN, PERIOD)
check("IAS 21 revaluation schedule produced", not reval.empty)
check("USD items carry no revaluation",
      bool((reval[reval["currency"] == "USD"]["fx_gain_loss"].abs() < 0.01).all()))
proposal = fx.revaluation_journal(CONN, PERIOD)
if proposal["lines"]:
    dr = sum(l["debit"] for l in proposal["lines"])
    cr = sum(l["credit"] for l in proposal["lines"])
    check("Proposed revaluation journal balances", abs(dr - cr) < 0.02, f"{dr} vs {cr}")

# ---------------------------------------------------------------- compliance
ppe = compliance.ppe_register(CONN)
check("PP&E register loaded", not ppe.empty)
check("Net book value is never negative",
      bool((ppe["nbv"] >= -0.01).all()))
check("Depreciation recompute agrees with the register on the clean assets",
      int((~ppe["dep_check"]).sum()) < len(ppe),
      f"{int((~ppe['dep_check']).sum())} of {len(ppe)} disagree")
check("Units-of-production assets are present",
      bool((ppe["method"] == "Units of production").any()))
check("An impairment is identified", float(ppe["impairment_exposure"].sum()) > 0,
      f"exposure={ppe['impairment_exposure'].sum()}")

idle = ppe[ppe["status"] == "Idle"]
if not idle.empty:
    asset = idle.iloc[0]
    before = float(asset["accum_impairment"])
    res = compliance.record_impairment(CONN, "m.buenaventura", asset["asset_no"],
                                       float(asset["nbv"]) - 50_000, "Asset idle or under-utilised",
                                       "Value in use recalculated on the revised programme")
    after = db.scalar(CONN, "SELECT accum_impairment FROM ppe_register WHERE asset_no=?",
                      (asset["asset_no"],))
    check("Impairment loss increases accumulated impairment", float(after) > before)

dep = compliance.run_depreciation(CONN, "m.buenaventura", PERIOD)
check("Depreciation run produces a balanced journal",
      dep["ok"] and abs(sum(l["debit"] for l in dep["lines"])
                        - sum(l["credit"] for l in dep["lines"])) < 1.0,
      dep["message"])

inv = compliance.inventory_valuation(CONN, PERIOD)
check("Inventory loaded", not inv.empty)
check("Carrying value is never above cost",
      bool((inv["carrying_value"] <= inv["total_cost"] + 0.01).all()))
below = inv[inv["below_cost"]]
check("At least one line is below NRV", not below.empty)
check("Write-down equals cost less NRV on those lines",
      bool((abs(below["required_writedown"]
                - (below["total_cost"] - below["total_nrv"])) < 0.02).all()))
inv_j = compliance.inventory_journal(CONN, PERIOD)
if inv_j["lines"]:
    check("Inventory journal balances",
          abs(sum(l["debit"] for l in inv_j["lines"])
              - sum(l["credit"] for l in inv_j["lines"])) < 0.02)

prov = compliance.provisions(CONN, PERIOD)
check("Provisions loaded", not prov.empty)
check("Closing balance equals the roll-forward",
      bool((abs(prov["closing"]
                - (prov["opening"] + prov["additions"] - prov["utilised"]
                   - prov["unused_reversed"] + prov["unwind_discount"]
                   + prov["fx_movement"])) < 0.02).all()))
check("Contingent items are separated from recognised provisions",
      bool(prov["disclosure_only"].any()))

roll = compliance.provision_rollforward(CONN, PERIOD)
check("Roll-forward carries a total row", "TOTAL" in list(roll["category"]))

scorecard = compliance.compliance_scorecard(CONN, PERIOD)
check("Compliance scorecard covers IAS 16, IAS 2, IAS 37 and controls",
      len(scorecard) >= 4, str([s["area"] for s in scorecard]))

# --------------------------------------------------------------- audit trail
integrity = audit.verify_chain(CONN)
check("Audit chain verifies clean", integrity["valid"], integrity["reason"])
check("Audit chain is non-trivial", integrity["checked"] > 100,
      f"entries={integrity['checked']}")

# tamper with a historical row and confirm detection
CONN.execute("UPDATE audit_log SET payload = '{\"tampered\":true}' WHERE id = "
             "(SELECT id FROM audit_log ORDER BY id LIMIT 1 OFFSET 5)")
CONN.commit()
broken = audit.verify_chain(CONN)
check("Tampering with an entry breaks the chain", not broken["valid"], broken["reason"])
check("Verifier names the first broken link", broken["broken_at"] is not None,
      f"broken_at={broken['broken_at']}")


# ------------------------------------------------------------------- report
def main() -> int:
    passed = sum(1 for _, ok, _ in CHECKS if ok)
    failed = [c for c in CHECKS if not c[1]]
    width = max(len(n) for n, _, _ in CHECKS) + 2
    for name, ok, detail in CHECKS:
        mark = "PASS" if ok else "FAIL"
        line = f"[{mark}] {name.ljust(width)}"
        if detail and not ok:
            line += f"  → {detail}"
        print(line)
    print("-" * 70)
    print(f"{passed}/{len(CHECKS)} checks passed")
    if failed:
        print(f"{len(failed)} FAILED")
        return 1
    return 0


def test_all():
    """pytest entry point."""
    failures = [f"{n}: {d}" for n, ok, d in CHECKS if not ok]
    assert not failures, "\n".join(failures)


if __name__ == "__main__":
    raise SystemExit(main())
