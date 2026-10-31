"""Regression tests for pgdoctor, using real plans captured from the PG13/PG17 docker lab (samples/)."""
import os
import sys
import unittest
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pgdoctor.compare import compare_plans  # noqa: E402
from pgdoctor.findings import SEV_RANK  # noqa: E402
from pgdoctor.health import analyze_health, compare_health, load_health  # noqa: E402
from pgdoctor.plan import load_plan, parse_plan_text  # noqa: E402
from pgdoctor.report import render  # noqa: E402
from pgdoctor.rules_plan import analyze_plan  # noqa: E402
from pgdoctor.settings import compare_settings, load_settings  # noqa: E402
from pgdoctor.statements import compare_statements, load_statements  # noqa: E402

S = os.path.join(ROOT, "samples")


def plans(name, sub="plans", ext="json"):
    return (load_plan(os.path.join(S, sub, f"{name}.pg13.{ext}")),
            load_plan(os.path.join(S, sub, f"{name}.pg17.{ext}")))


def all_findings(name, sub="plans", ext="json"):
    o, n = plans(name, sub, ext)
    fs = analyze_plan(o, "old", name) + analyze_plan(n, "new", name)
    fs += compare_plans(o, n, name).findings
    return fs


def rules(fs, min_sev="info"):
    return {f.rule for f in fs if SEV_RANK[f.severity] <= SEV_RANK[min_sev]}


class TestParsing(unittest.TestCase):
    def test_text_and_json_same_shape(self):
        for q in ("qA_cte_join_update", "qD_cte_skew", "qE_notin_subplan"):
            j = load_plan(os.path.join(S, "plans", f"{q}.pg13.json"))
            t = load_plan(os.path.join(S, "plans_text", f"{q}.pg13.txt"))
            self.assertEqual([n.label for n in j.nodes], [n.label for n in t.nodes], q)
            self.assertEqual([n.subplan_name for n in j.nodes], [n.subplan_name for n in t.nodes], q)

    def test_psql_decoration_is_stripped(self):
        txt = (" QUERY PLAN\n-----------------------------\n"
               " Seq Scan on t  (cost=0.00..35.50 rows=2550 width=4) (actual time=0.01..0.20 rows=2550 loops=1)+\n"
               "   Filter: (a > 1)                                                                            +\n"
               " Planning Time: 0.05 ms                                                                     +\n"
               " Execution Time: 0.30 ms\n(4 rows)\n")
        p = parse_plan_text(txt)
        self.assertEqual(p.root.node_type, "Seq Scan")
        self.assertEqual(p.root.relation, "t")
        self.assertEqual(p.root.get("Filter"), "(a > 1)")
        self.assertAlmostEqual(p.execution_time, 0.30)

    def test_estimate_only_plan(self):
        _, n = plans("qE_notin_subplan")
        self.assertFalse(n.analyzed)
        self.assertIsNone(n.execution_time)


class TestPlanRules(unittest.TestCase):
    def test_nostats_cte_join_regression(self):
        fs = all_findings("qA_cte_join_update")
        r = rules(fs, "high")
        for expected in ("CMP-REGRESSION", "PLAN-NL-RESCAN", "PLAN-MISEST", "CMP-NOSTATS", "CMP-JOIN-METHOD"):
            self.assertIn(expected, r)

    def test_same_result_from_text_plans(self):
        r = rules(all_findings("qA_cte_join_update", "plans_text", "txt"), "high")
        self.assertIn("PLAN-NL-RESCAN", r)
        self.assertIn("CMP-NOSTATS", r)

    def test_after_analyze_is_not_a_regression(self):
        fs = all_findings("qA_after_analyze")
        self.assertFalse([f for f in fs if f.rule == "CMP-REGRESSION" and f.severity != "info"])
        self.assertNotIn("PLAN-NL-RESCAN", rules(fs))

    def test_hashed_subplan_lost(self):
        for sub, ext in (("plans", "json"), ("plans_text", "txt")):
            fs = all_findings("qE_notin_subplan", sub, ext)
            crit = [f for f in fs if f.rule == "CMP-SUBPLAN-HASH-LOST"]
            self.assertTrue(crit, sub)
            self.assertEqual(crit[0].severity, "critical")
            self.assertIn("PLAN-SUBPLAN-NOHASH", rules(fs, "high"))

    def test_merge_join_inner_rows_not_flagged(self):
        o, _ = plans("qD_cte_skew")
        fs = analyze_plan(o, "old", "qD")
        self.assertFalse([f for f in fs if f.rule == "PLAN-MISEST" and "Sort" in f.message])

    def test_modifytable_not_misestimate(self):
        o, _ = plans("qE_notin_subplan")
        fs = analyze_plan(o, "old", "qE")
        self.assertFalse([f for f in fs if f.rule == "PLAN-MISEST" and f.message.startswith("Update")])

    def test_pg17_cte_pathkeys(self):
        self.assertIn("CMP-CTE-PATHKEYS", rules(all_findings("qC_cte_pathkeys")))

    def test_default_ndistinct_on_pg13_cte(self):
        o, _ = plans("qC_cte_pathkeys")
        self.assertIn("PLAN-DEFAULT-NDISTINCT", rules(analyze_plan(o, "old", "qC")))


class TestLabCases(unittest.TestCase):
    """Full plans of the lab cases (sql/case*.sql) captured on PG 13.23 / PG 17.11."""

    def test_case3_is_cte_correlation_not_missing_stats(self):
        fs = all_findings("case3_cte_correlation", "plans_text", "txt")
        r = rules(fs, "high")
        self.assertIn("CMP-CTE-CORRELATION", r)
        self.assertIn("PLAN-NL-RESCAN", r)
        self.assertNotIn("CMP-NOSTATS", rules(fs))

    def test_case4_full_run(self):
        fs = all_findings("case4_notin_full", "plans_text", "txt")
        r = rules(fs, "high")
        for expected in ("CMP-REGRESSION", "CMP-SUBPLAN-HASH-LOST", "PLAN-SUBPLAN-NOHASH", "PLAN-TEMP-IO"):
            self.assertIn(expected, r)

    def test_case2_nostats_estimate_only(self):
        fs = all_findings("case2_nostats")
        r = rules(fs, "high")
        self.assertIn("CMP-NOSTATS", r)
        self.assertIn("PLAN-NL-BET", r)


class TestSnapshots(unittest.TestCase):
    def test_settings(self):
        fs, table = compare_settings(load_settings(os.path.join(S, "collected/pg13/settings.json")),
                                     load_settings(os.path.join(S, "collected/pg17/settings.json")))
        names = {f.subject for f in fs if f.rule == "SET-DIFF"}
        self.assertIn("hash_mem_multiplier", names)
        # enable_partitionwise_* are off by default – must not be reported
        self.assertFalse([f for f in fs if f.rule == "SET-ENABLE-OFF"])
        self.assertIn("old_snapshot_threshold", {f.subject for f in fs if f.rule == "SET-REMOVED"})

    def test_health(self):
        old = load_health(os.path.join(S, "collected/pg13/health.json"))
        new = load_health(os.path.join(S, "collected/pg17/health.json"))
        fs = analyze_health(new, "new") + compare_health(old, new)
        r = {f.rule: f for f in fs}
        self.assertEqual(r["HEALTH-SEARCHPATH-FUNC"].severity, "critical")
        self.assertIn("HEALTH-EXTSTATS-NODATA", r)
        self.assertIn("HEALTH-NEVER-ANALYZED", r)
        self.assertIn("HEALTH-STATS-MISSING-COLS", r)

    def test_statements_multiline_csv(self):
        fs, rows = compare_statements(load_statements(os.path.join(S, "synthetic/statements.pg13.json")),
                                      load_statements(os.path.join(S, "synthetic/statements.pg17.csv")))
        top = [f for f in fs if f.rule == "PGSS-REGRESSION" and f.severity == "critical"]
        self.assertTrue(top)
        self.assertIn("60.0×", top[0].message)


class TestReport(unittest.TestCase):
    def test_render_is_wellformed(self):
        o, n = plans("qE_notin_subplan")
        cmp_ = compare_plans(o, n, "qE")
        fs = analyze_plan(o, "old", "qE") + analyze_plan(n, "new", "qE") + cmp_.findings
        html = render({"queries": [{"id": "0", "name": "qE", "old": o, "new": n, "cmp": cmp_, "findings": fs}],
                       "findings": fs, "settings_table": [], "statements_rows": [], "labels": ("PG13", "PG17"),
                       "inputs": ["x"]})
        self.assertIn("CMP-SUBPLAN-HASH-LOST", html)
        stack = []

        class P(HTMLParser):
            VOID = {"meta", "br", "link", "img", "input", "hr"}

            def handle_starttag(self, tag, attrs):
                if tag not in self.VOID:
                    stack.append(tag)

            def handle_endtag(self, tag):
                self_ = stack.pop()
                assert self_ == tag, (self_, tag)
        P().feed(html)
        self.assertEqual(stack, [])


if __name__ == "__main__":
    unittest.main()
