"""Tests for the remediation planner (pgdoctor/remedy.py), Query Identifier parsing, version inference and the
PLAN-CTE-COST rule. Plans come from lab/remedies (PG 13.23 vs PG 17.11 + pg_hint_plan 1.7.2, CASE 4 data).

Run from the repo root:  python -m unittest discover -s tests -v"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, ".claude", "skills", "pgdoctor"))

from pgdoctor.cli import main  # noqa: E402
from pgdoctor.compare import compare_plans  # noqa: E402
from pgdoctor.plan import infer_major, load_plan, major_from_label, parse_plan_text  # noqa: E402
from pgdoctor.remedy import RemedyContext, remedies_for_query, render_runbook  # noqa: E402
from pgdoctor.rules_plan import analyze_plan, subplan_info  # noqa: E402

S = os.path.join(HERE, "samples")
R = os.path.join(S, "remedy")
QID = 8512646466599369567          # Query Identifier of lab/remedies/sql/q4.sql (literal 'VN') on the lab PG17


def _sp(plan):
    return next(n for n in plan.nodes if n.parent_rel == "SubPlan" and n.subplan_name)


def _remedies(old, new, name="qF", ctx=None):
    fs = analyze_plan(new, "new", name)
    if old is not None:
        fs += analyze_plan(old, "old", name) + compare_plans(old, new, name).findings
    return remedies_for_query(name, old, new, fs, ctx or RemedyContext(app_role="app_batch", app_db="lab4"))


CORRELATED = """
 Update on products p  (cost=0.00..90000.00 rows=0 width=0)
   CTE current_brand_code
     ->  Seq Scan on brand_assignments ba  (cost=0.00..30000.00 rows=900000 width=10)
   ->  Seq Scan on products p  (cost=0.00..90000.00 rows=5000 width=10)
         Filter: (NOT (ANY (p.brand_code = (SubPlan 2).col1)))
         SubPlan 2
           ->  CTE Scan on current_brand_code cbc  (cost=0.00..20000.00 rows=2000 width=32)
                 Filter: (cbc.region = p.region)
"""


class TestQueryIdAndVersion(unittest.TestCase):
    def test_query_identifier_json_and_text(self):
        self.assertEqual(load_plan(os.path.join(R, "qF_explain_only.pg17.json")).query_id, QID)
        self.assertEqual(load_plan(os.path.join(R, "qF_role_hash_mem8.pg17.txt")).query_id, QID)
        self.assertIsNone(load_plan(os.path.join(S, "plans", "qF_notin_remedy.pg13.json")).query_id)

    def test_major_from_file_label_and_syntax(self):
        self.assertEqual(load_plan(os.path.join(S, "plans", "qF_notin_remedy.pg13.json")).major, 13)
        with open(os.path.join(R, "qF_role_hash_mem8.pg17.txt"), encoding="utf-8") as fh:
            p = parse_plan_text(fh.read())          # no file name -> inferred from '(hashed SubPlan 2).col1'
        self.assertIsNone(p.major_hint)
        self.assertEqual(infer_major(p), 17)
        self.assertEqual(major_from_label("PG17"), 17)
        self.assertEqual(major_from_label("pg13"), 13)
        self.assertIsNone(major_from_label("new"))


class TestHashThreshold(unittest.TestCase):
    """Lab E1 measured the exact switch point: PG17 10381kB -> SubPlan, 10382kB -> hashed; PG13 257kB -> SubPlan, 258kB -> hashed."""

    def test_pg17_min_work_mem_matches_lab(self):
        p = load_plan(os.path.join(R, "qF_explain_only.pg17.json"))
        info = subplan_info(p, _sp(p))
        self.assertFalse(info["hashed"])
        self.assertFalse(info["correlated"])
        self.assertEqual(info["hash_mem_multiplier"], 2.0)      # PG15+ default
        self.assertEqual(info["min_work_mem_kb"], 10382)
        self.assertAlmostEqual(info["min_hash_mem_multiplier"], 5.07, places=2)

    def test_pg13_min_work_mem_matches_lab(self):
        p = load_plan(os.path.join(S, "plans", "qF_notin_remedy.pg13.json"))
        info = subplan_info(p, _sp(p))
        self.assertTrue(info["hashed"])
        self.assertEqual(info["hash_mem_multiplier"], 1.0)      # PG13 default
        self.assertEqual(info["min_work_mem_kb"], 258)

    def test_settings_in_plan_are_used(self):
        p = load_plan(os.path.join(R, "qF_role_hash_mem8.pg17.txt"))
        info = subplan_info(p, _sp(p))
        self.assertTrue(info["hashed"])
        self.assertEqual(info["hash_mem_multiplier"], 8.0)

    def test_correlated_subplan_detected(self):
        p = parse_plan_text(CORRELATED)
        self.assertTrue(subplan_info(p, _sp(p))["correlated"])


class TestRemedies(unittest.TestCase):
    def test_case4_order_no_code_change_first(self):
        old = load_plan(os.path.join(S, "plans", "qF_notin_remedy.pg13.json"))
        new = load_plan(os.path.join(R, "qF_explain_only.pg17.json"))
        rem = _remedies(old, new)
        keys = [r.key for r in rem]
        self.assertEqual(keys[0], "hint-table-hash-mem")
        self.assertLess(keys.index("role-hash-mem"), keys.index("rewrite-not-exists"))
        self.assertFalse(any(r.code_change for r in rem[:4]))
        self.assertTrue(next(r for r in rem if r.key == "rewrite-not-exists").code_change)
        self.assertFalse(rem[-1].effective)                     # "not effective" list (indexes, SET STATISTICS 0...) last
        hint = "\n".join(rem[0].steps)
        self.assertIn(str(QID), hint)
        self.assertIn("Set(hash_mem_multiplier 12)", hint)      # 2 x min 5.07 -> 12
        self.assertIn("SET pg_hint_plan.enable_hint_table = off", hint)

    def test_runbook_contains_alter_role(self):
        new = load_plan(os.path.join(R, "qF_explain_only.pg17.json"))
        ctx = RemedyContext(app_role="batch_user", app_db="appdb", app_name="batch-job")
        txt = render_runbook([{"name": "qF", "remedies": _remedies(None, new, ctx=ctx)}], ctx)
        self.assertIn("ALTER ROLE batch_user IN DATABASE appdb SET hash_mem_multiplier = 12;", txt)
        self.assertIn("VALUES (8512646466599369567, 'batch-job', 'Set(hash_mem_multiplier 12)');", txt)

    def test_fixed_plans_need_no_remedy(self):
        for f in ("qF_hinted.pg17.json", "qF_role_hash_mem8.pg17.txt"):
            new = load_plan(os.path.join(R, f))
            self.assertEqual(_remedies(None, new), [], f)

    def test_correlated_subplan_gets_rewrite_advice_only(self):
        rem = _remedies(None, parse_plan_text(CORRELATED))
        self.assertEqual([r.key for r in rem], ["subplan-correlated"])
        self.assertTrue(rem[0].code_change)

    def test_cli_remedy_writes_runbook(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "fix.sql")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = main(["remedy", "--plan", os.path.join(R, "qF_explain_only.pg17.json"),
                           "--app-role", "app_batch", "--app-db", "lab4", "-o", out])
            self.assertEqual(rc, 0)
            with open(out, encoding="utf-8") as fh:
                txt = fh.read()
            self.assertIn("INSERT INTO hint_plan.hints", txt)
            self.assertIn("NOT EXISTS", txt)
            self.assertIn("không sửa code", buf.getvalue())

    def test_report_has_remedy_section(self):
        from pgdoctor.report import render
        old = load_plan(os.path.join(S, "plans", "qF_notin_remedy.pg13.json"))
        new = load_plan(os.path.join(R, "qF_explain_only.pg17.json"))
        cmp_ = compare_plans(old, new, "qF")
        fs = analyze_plan(old, "old", "qF") + analyze_plan(new, "new", "qF") + cmp_.findings
        rem = remedies_for_query("qF", old, new, fs)
        html = render({"queries": [{"id": "0", "name": "qF", "old": old, "new": new, "cmp": cmp_, "findings": fs,
                                    "remedies": rem}], "findings": fs, "settings_table": None, "statements_rows": None,
                       "labels": ("PG13", "PG17"), "inputs": ["x"]})
        self.assertIn("id='remedies'", html)
        self.assertIn("hint_plan.hints", html)

    def test_cli_work_mem_override(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["remedy", "--plan", os.path.join(R, "qF_explain_only.pg17.json"), "--work-mem", "8MB"])
        self.assertIn("hash_mem_multiplier ≥ 2.53", buf.getvalue())


class TestLabPair(unittest.TestCase):
    """qF = CASE 4 re-measured on lab/remedies (2 vCPU): PG13 4.0 s, PG17 1228 s (analyzed, with Query Identifier)."""

    def test_full_pair(self):
        old = load_plan(os.path.join(S, "plans", "qF_notin_remedy.pg13.json"))
        new = load_plan(os.path.join(S, "plans", "qF_notin_remedy.pg17.json"))
        self.assertGreater(new.execution_time / old.execution_time, 250)
        fs = analyze_plan(old, "old", "qF") + analyze_plan(new, "new", "qF") + compare_plans(old, new, "qF").findings
        r = {f.rule for f in fs if f.severity in ("critical", "high")}
        for expected in ("CMP-REGRESSION", "CMP-SUBPLAN-HASH-LOST", "PLAN-SUBPLAN-NOHASH", "PLAN-CTE-COST"):
            self.assertIn(expected, r)
        rem = remedies_for_query("qF", old, new, fs)
        self.assertEqual(rem[0].key, "hint-table-hash-mem")
        self.assertIn(str(QID), "\n".join(rem[0].steps))

    def test_nestloop_hint_covers_every_set_with_inner(self):
        # lab E9: HashJoin(cu a b) alone was NOT enough (planner moved the Nested Loop to {a, b});
        # NoNestLoop for every alias set containing the re-scanned inner side fixed it (100 s -> 0.84 s)
        old = load_plan(os.path.join(S, "plans_text", "case3_cte_correlation.pg13.txt"))
        new = load_plan(os.path.join(S, "plans_text", "case3_cte_correlation.pg17.txt"))
        rem = _remedies(old, new)
        self.assertEqual(rem[0].key, "hint-table-join")
        steps = "\n".join(rem[0].steps)
        for h in ("NoNestLoop(a b)", "NoNestLoop(cu b)", "NoNestLoop(cu a b)"):
            self.assertIn(h, steps)
        self.assertNotIn("NoNestLoop(cu a)", steps)   # the cu-a join is a fine Hash Join, leave it alone


class TestCteCost(unittest.TestCase):
    def test_pg13_body_dominates(self):
        p = load_plan(os.path.join(S, "plans", "qF_notin_remedy.pg13.json"))
        f = [x for x in analyze_plan(p, "old", "qF") if x.rule == "PLAN-CTE-COST"]
        self.assertTrue(f)
        self.assertIn("thân CTE chiếm", f[0].message)

    def test_pg13_short_statement_is_low(self):
        p = load_plan(os.path.join(S, "plans_text", "case4_notin_full.pg13.txt"))
        f = [x for x in analyze_plan(p, "old", "c4") if x.rule == "PLAN-CTE-COST"]
        self.assertEqual(f[0].severity, "low")

    def test_pg17_rescans_dominate(self):
        # lab 8 vCPU, 257 s: CTE body 410 ms, the CTE Scan inside the non-hashed SubPlan re-read 5000 times
        p = load_plan(os.path.join(S, "plans_text", "case4_notin_full.pg17.txt"))
        f = [x for x in analyze_plan(p, "new", "c4") if x.rule == "PLAN-CTE-COST"]
        self.assertTrue(f)
        self.assertEqual(f[0].severity, "high")
        self.assertIn("QUÉT LẠI", f[0].message)
        self.assertEqual(f[0].data["scan_loops"], 5000)
        self.assertGreater(f[0].data["reread_ms"], 100 * f[0].data["body_ms"])


if __name__ == "__main__":
    unittest.main()
