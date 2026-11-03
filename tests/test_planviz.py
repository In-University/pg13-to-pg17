"""Tests for planviz (.claude/skills/planviz): input loading, text parser, per-node analysis (exclusive time with CTE /
InitPlan / parallel rules, local estimation error), findings, plan matching, HTML output, CLI, live 'explain' mode,
and parity between the Python analysis and its JavaScript port (assets/core.js, run through node when available).

Plans used: the lab outputs in lab/results (PG 13.23 vs 17.11, UPDATE … NOT IN (CTE)), the bundled demo plans and
small synthetic plans for the rules that the lab does not trigger."""
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SKILL = os.path.join(REPO, ".claude", "skills", "planviz")
sys.path.insert(0, SKILL)

from planviz import explain as ex  # noqa: E402
from planviz.analyze import analyze, fmt_factor, fmt_ms, load_rules, match, render_insight  # noqa: E402
from planviz.cli import main  # noqa: E402
from planviz.loader import load, parse_any  # noqa: E402
from planviz.render import build_html, demo_plans  # noqa: E402

DEMO = os.path.join(SKILL, "planviz", "assets", "demo")
LAB = os.path.join(REPO, "lab", "results")
PG13 = os.path.join(DEMO, "pg13_baseline.json")
PG17 = os.path.join(DEMO, "pg17_baseline.json")
PG17_HINT = os.path.join(DEMO, "pg17_hint_work_mem.json")
PG17_NE = os.path.join(DEMO, "pg17_not_exists.txt")
NODE = shutil.which("node")


def rd(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def wr(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def A(path, label=None):
    lp = load(path, label)
    return analyze(lp.obj, lp.label, lp.meta)


def node_by(a, pred):
    return next(n for n in a["nodes"] if pred(n))


def scan(rel=None, est=100, rows=100, loops=1, t=1.0, **kw):
    d = {"Node Type": "Seq Scan", "Relation Name": rel or "t", "Alias": rel or "t", "Plan Rows": est, "Plan Width": 8,
         "Startup Cost": 0, "Total Cost": 10, "Actual Rows": rows, "Actual Loops": loops, "Actual Startup Time": 0.0,
         "Actual Total Time": t}
    d.update(kw)
    return d


def plan(root, **meta):
    obj = {"Plan": root}
    obj.update(meta)
    return obj


def cancelling_plan():
    """Plain Aggregate over a Hash Join whose ↓100× selectivity error cancels the ↑100× its outer input carries."""
    outer = scan("a", 10, 1000, t=5.0, **{"Parent Relationship": "Outer", "Filter": "(a.k = 1)"})
    inner = {"Node Type": "Hash", "Parent Relationship": "Inner", "Plan Rows": 100, "Plan Width": 8, "Total Cost": 1,
             "Actual Rows": 100, "Actual Loops": 1, "Actual Total Time": 2.0, "Plans": [scan("b", 100, 100, t=1.0, **{"Parent Relationship": "Outer"})]}
    join = {"Node Type": "Hash Join", "Join Type": "Inner", "Parent Relationship": "Outer", "Plan Rows": 1000, "Plan Width": 8,
            "Total Cost": 1, "Actual Rows": 1000, "Actual Loops": 1, "Actual Total Time": 20.0, "Plans": [outer, inner]}
    root = {"Node Type": "Aggregate", "Strategy": "Plain", "Plan Rows": 1, "Plan Width": 8, "Total Cost": 1,
            "Actual Rows": 1, "Actual Loops": 1, "Actual Total Time": 25.0, "Plans": [join]}
    return plan(root, **{"Execution Time": 25.0, "Planning Time": 0.5})


def partitioned_plan(parts=60):
    """Append over many small partitions next to a hot Nested Loop (the shape the diagram folds)."""
    kids = [scan("p%03d" % i, 1000, 1000, t=0.5, **{"Parent Relationship": "Member"}) for i in range(parts)]
    append = {"Node Type": "Append", "Parent Relationship": "Outer", "Plan Rows": 1000 * parts, "Plan Width": 8, "Total Cost": 1,
              "Actual Rows": 1000 * parts, "Actual Loops": 1, "Actual Total Time": 0.6 * parts, "Plans": kids}
    nl = {"Node Type": "Nested Loop", "Join Type": "Inner", "Parent Relationship": "Inner", "Plan Rows": 900, "Plan Width": 8,
          "Total Cost": 1, "Actual Rows": 52000, "Actual Loops": 1, "Actual Total Time": 9000.0,
          "Plans": [scan("c", 900, 52000, t=90.0, **{"Parent Relationship": "Outer"}),
                    dict(scan("x", 1, 1, loops=52000, t=0.17, **{"Parent Relationship": "Inner"}), **{"Node Type": "Index Scan", "Index Name": "x_pkey"})]}
    root = {"Node Type": "Hash Join", "Join Type": "Inner", "Plan Rows": 40000, "Plan Width": 8, "Total Cost": 1,
            "Actual Rows": 52000, "Actual Loops": 1, "Actual Total Time": 9500.0, "Plans": [append, nl]}
    return plan(root, **{"Execution Time": 9500.0, "Planning Time": 2.0})


class LoaderTests(unittest.TestCase):
    def test_psql_decorated_json(self):
        lp = load(os.path.join(LAB, "remedies", "E0_pg17_baseline.json"))
        self.assertEqual(lp.fmt, "json")
        self.assertEqual(lp.obj["Plan"]["Node Type"], "ModifyTable")
        self.assertAlmostEqual(lp.obj["Execution Time"], 1227990.82, places=1)

    def test_text_with_psql_noise_and_verbose_output(self):
        lp = load(PG17_NE)               # starts with 'Timing is on.' / BEGIN / Time: lines
        self.assertEqual(lp.fmt, "text")
        self.assertEqual(lp.label, "PG17")
        self.assertEqual(lp.obj["Plan"]["Operation"], "Update")
        self.assertEqual(lp.obj["Query Identifier"], -161997702680696319)
        lp = load(os.path.join(LAB, "remedies", "E0_pg13_baseline.txt"))
        cte = lp.obj["Plan"]["Plans"][0]
        self.assertEqual(cte["Subplan Name"], "CTE current_brand_code")
        self.assertEqual(cte["Output"], ["b.code", "s.region"])            # VERBOSE output as a list

    def test_planning_block_does_not_overwrite_root_buffers(self):
        lp = load(os.path.join(LAB, "case1_pg17.txt"))
        self.assertEqual(lp.obj["Plan"]["Shared Hit Blocks"], 37637)
        self.assertEqual(lp.obj["Planning"]["Shared Hit Blocks"], 207)
        self.assertIn("JIT", lp.obj)

    def test_capture_directory_and_labels(self):
        lp = load(os.path.join(REPO, "tests", "samples", "run", "case4.pg13"))
        self.assertEqual(lp.label, "PG 13.23")
        self.assertEqual(lp.meta["capture"], "plan_analyze.json")
        self.assertIn("NOT IN", lp.meta["query"])
        lp = load(os.path.join(REPO, "tests", "samples", "run", "case4.pg17"))
        self.assertEqual(lp.meta["capture"], "plan.json")              # no ANALYZE captured -> EXPLAIN plan

    def test_auto_explain_text_and_json(self):
        log = ("2026-11-01 10:00:00 UTC LOG:  duration: 12.345 ms  plan:\n"
               "\tQuery Text: select * from t where a = 1\n"
               "\tSeq Scan on t  (cost=0.00..35.50 rows=10 width=8) (actual time=0.010..0.900 rows=7 loops=1)\n"
               "\t  Filter: (a = 1)\n"
               "\t  Rows Removed by Filter: 2543\n")
        obj, fmt = parse_any(log)
        self.assertEqual(fmt, "text")
        self.assertEqual(obj["Plan"]["Relation Name"], "t")
        self.assertEqual(obj["Plan"]["Rows Removed by Filter"], 2543)
        obj, fmt = parse_any('LOG:  duration: 1 ms  plan:\n{"Query Text": "select 1", "Plan": {"Node Type": "Result", '
                             '"Plan Rows": 1, "Plan Width": 4, "Actual Rows": 1, "Actual Loops": 1}}')
        self.assertEqual((fmt, obj["Plan"]["Node Type"]), ("json", "Result"))

    def test_rejects_non_plans(self):
        with self.assertRaises(ValueError):
            parse_any("hello world")


class AnalysisTests(unittest.TestCase):
    def test_exclusive_times_add_up(self):
        for p in (PG13, PG17, PG17_HINT, PG17_NE, os.path.join(LAB, "case2_pg13.txt"), os.path.join(LAB, "case1_pg17.txt")):
            a = A(p)
            total = sum(n["excl"] or 0 for n in a["nodes"])
            root = a["nodes"][0]["incl"]
            self.assertLess(abs(total - root) / root, 0.01, p)

    def test_cte_time_charged_to_cte_scan(self):
        a = A(os.path.join(LAB, "remedies", "E0_pg13_baseline.txt"))
        cs = node_by(a, lambda n: n["type"] == "CTE Scan")
        self.assertAlmostEqual(cs["excl"], 3834.219 - 2705.632, places=2)
        self.assertAlmostEqual(a["nodes"][0]["excl"], 4235.182 - 4182.199, places=2)   # CTE not subtracted twice
        cte = node_by(a, lambda n: n["sub"] == "CTE current_brand_code")
        self.assertEqual(cte["time_parent"], cs["id"])

    def test_lazy_initplan_charged_to_evaluating_node(self):
        a = A(PG17_NE)
        res = node_by(a, lambda n: n["type"] == "Result")
        ip3 = node_by(a, lambda n: n["sub"] == "InitPlan 3")
        self.assertEqual(ip3["time_parent"], res["id"])
        self.assertLess(res["excl"], 5)                                   # not 3.1 s
        self.assertAlmostEqual(a["nodes"][0]["excl"], 4203.790 - 4103.860, places=2)
        self.assertNotIn("HOTSPOT", {i["rule"] for i in a["insights"] if i["node"] == res["id"]})

    def test_local_error_on_cte_scan(self):
        a = A(PG13)
        cs = node_by(a, lambda n: n["type"] == "CTE Scan")
        self.assertEqual(fmt_factor(cs["lq"]), "↑85×")
        self.assertLess(abs(cs["base_lq"]), 0.1)
        self.assertGreater(cs["local_lq"], 1.8)

    def test_local_error_on_joins(self):
        inner = scan("b", 50, 50)
        outer = scan("a", 1000, 1000)
        root = {"Node Type": "Hash Join", "Join Type": "Inner", "Plan Rows": 100, "Plan Width": 8, "Total Cost": 1,
                "Actual Rows": 10000, "Actual Loops": 1, "Actual Total Time": 10.0, "Hash Cond": "(a.x = b.x)",
                "Plans": [dict(outer, **{"Parent Relationship": "Outer"}),
                          {"Node Type": "Hash", "Parent Relationship": "Inner", "Plan Rows": 50, "Plan Width": 8,
                           "Total Cost": 1, "Actual Rows": 500, "Actual Loops": 1, "Actual Total Time": 2.0,
                           "Plans": [dict(inner, **{"Parent Relationship": "Outer", "Actual Rows": 500})]}]}
        a = analyze(plan(root, **{"Execution Time": 10.0}))
        hj = a["nodes"][0]
        self.assertAlmostEqual(hj["lq"], 2.0)
        self.assertAlmostEqual(hj["base_lq"], 1.0)                          # the inner side is 10× off
        self.assertAlmostEqual(hj["local_lq"], 1.0)                         # join selectivity adds another 10×
        root["Join Type"] = "Anti"
        a = analyze(plan(root))
        self.assertAlmostEqual(a["nodes"][0]["base_lq"], 0.0)              # anti join: only the outer side counts

    def test_parallel_time_not_multiplied_by_workers(self):
        root = {"Node Type": "Gather", "Plan Rows": 300, "Plan Width": 8, "Total Cost": 1, "Workers Planned": 2,
                "Workers Launched": 2, "Actual Rows": 300, "Actual Loops": 1, "Actual Total Time": 110.0,
                "Plans": [scan("t", 100, 100, loops=3, t=100.0, **{"Parallel Aware": True, "Parent Relationship": "Outer"})]}
        a = analyze(plan(root, **{"Execution Time": 111.0}))
        self.assertAlmostEqual(a["nodes"][1]["incl"], 100.0)
        self.assertAlmostEqual(a["nodes"][0]["excl"], 10.0)
        self.assertEqual(a["nodes"][1]["label"], "Parallel Seq Scan")

    def test_buffers_exclusive(self):
        root = {"Node Type": "Sort", "Plan Rows": 10, "Plan Width": 8, "Total Cost": 1, "Actual Rows": 10, "Actual Loops": 1,
                "Actual Total Time": 5.0, "Shared Hit Blocks": 120, "Temp Written Blocks": 40, "Sort Space Type": "Disk",
                "Sort Space Used": 320, "Sort Method": "external merge",
                "Plans": [scan("t", 10, 10, t=1.0, **{"Shared Hit Blocks": 100, "Parent Relationship": "Outer"})]}
        a = analyze(plan(root, **{"Execution Time": 5.0}))
        self.assertEqual(a["nodes"][0]["buf_excl"]["Shared Hit Blocks"], 20)
        self.assertEqual(a["nodes"][0]["buf_excl"]["Temp Written Blocks"], 40)
        self.assertIn("SORT_SPILL", [i["rule"] for i in a["insights"]])

    def test_hot_path_goes_through_cte(self):
        a = A(PG13)
        hot = [n["id"] for n in a["nodes"] if n["hot"]]
        self.assertIn(node_by(a, lambda n: n["type"] == "CTE Scan")["id"], hot)
        self.assertIn(node_by(a, lambda n: n["object"].startswith("brand_assignments"))["id"], hot)


class InsightTests(unittest.TestCase):
    def rules(self, a):
        return [(i["rule"], i["sev"], i["node"]) for i in a["insights"]]

    def test_pg17_not_in_subplan_rescan(self):
        a = A(PG17)
        ins = next(i for i in a["insights"] if i["rule"] == "SUBPLAN_RESCAN")
        self.assertEqual(ins["sev"], "critical")
        self.assertTrue(ins["p"]["notin"])
        self.assertEqual(ins["p"]["loops"], "5.0K")
        self.assertEqual(ins["p"]["budget"], "8.0 MB")                     # 4MB × 2 (PG15+ default, PG17 inferred)
        self.assertEqual(ins["p"]["need"], "20.3 MB")
        self.assertFalse(ins["p"]["fits"])
        r = render_insight(ins, load_rules())
        self.assertIn("NOT EXISTS", r["fix"])
        self.assertNotIn("{", r["detail"] + r["fix"])

    def test_hashed_subplans_are_fine(self):
        for p in (PG13, PG17_HINT):
            self.assertNotIn("SUBPLAN_RESCAN", [i["rule"] for i in A(p)["insights"]], p)

    def test_pg13_misestimate_origin(self):
        a = A(PG13)
        ins = next(i for i in a["insights"] if i["rule"] == "MISEST")
        self.assertEqual(a["nodes"][ins["node"]]["type"], "CTE Scan")
        self.assertTrue(ins["p"]["cte"])
        self.assertEqual(ins["p"]["dir"], "thiếu")

    def test_not_exists_plan(self):
        rules = self.rules(A(PG17_NE))
        self.assertIn("HASH_SPILL", [r[0] for r in rules])
        self.assertIn("NEVER_EXECUTED", [r[0] for r in rules])

    def test_explain_only(self):
        a = A(os.path.join(REPO, "tests", "samples", "run", "case4.pg17"))
        self.assertEqual(self.rules(a)[0][0], "NO_ANALYZE")
        self.assertIsNone(a["summary"]["max_abs_lq"])

    def test_synthetic_rules(self):
        root = {"Node Type": "Nested Loop", "Join Type": "Inner", "Plan Rows": 10, "Plan Width": 8, "Total Cost": 1,
                "Actual Rows": 5000, "Actual Loops": 1, "Actual Total Time": 900.0,
                "Plans": [scan("a", 10, 5000, t=5.0, **{"Parent Relationship": "Outer"}),
                          scan("b", 1, 1, loops=5000, t=0.15, **{"Parent Relationship": "Inner", "Rows Removed by Filter": 400})]}
        obj = plan(root, **{"Execution Time": 1000.0, "Planning Time": 1.0,
                            "Triggers": [{"Trigger Name": "RI_ConstraintTrigger_a_1", "Relation": "c", "Time": 300.0, "Calls": 5000}],
                            "JIT": {"Timing": {"Total": 150.0}}})
        got = {i["rule"] for i in analyze(obj)["insights"]}
        self.assertTrue({"RESCAN", "FILTER_WASTE", "TRIGGERS", "JIT", "MISEST", "HOTSPOT"} <= got, got)

    def test_misestimate_reported_where_born_not_where_cancelled(self):
        a = analyze(cancelling_plan())
        agg, join = a["nodes"][0], a["nodes"][1]
        self.assertEqual(agg["local_lq"], 0.0)                     # plain aggregate: one row whatever the input
        self.assertAlmostEqual(join["local_lq"], -2.0)              # ↓100× here, but the estimate itself is right
        mis = [i["node"] for i in a["insights"] if i["rule"] == "MISEST"]
        scan_a = node_by(a, lambda n: n["object"].startswith("a"))
        self.assertEqual(mis, [scan_a["id"]])

    def test_every_rule_has_templates(self):
        rules = load_rules()
        src = rd(os.path.join(SKILL, "planviz", "analyze.py"))
        for rid in set(re.findall(r'add\("([A-Z_]+)"', src)):
            self.assertIn(rid, rules)
            self.assertTrue(rules[rid]["title"])


class MatchTests(unittest.TestCase):
    def test_same_shape(self):
        a, b = A(PG13), A(PG17)
        self.assertEqual(match(a, b), [[i, i] for i in range(10)])

    def test_different_shape_and_subplan_numbers(self):
        a, b = A(PG13), A(PG17_NE)
        pairs = dict(match(a, b))
        cte13 = node_by(a, lambda n: n["sub"] == "CTE current_brand_code")["id"]
        cte17 = node_by(b, lambda n: n["sub"] == "CTE current_brand_code")["id"]
        self.assertEqual(pairs[cte13], cte17)
        p13 = node_by(a, lambda n: n["object"] == "products p" and n["type"] == "Index Scan")["id"]
        self.assertEqual(b["nodes"][pairs[p13]]["object"], "products p")
        self.assertNotIn(node_by(a, lambda n: n["type"] == "CTE Scan")["id"], pairs)   # SubPlan scan vanished


class HtmlAndCliTests(unittest.TestCase):
    def test_html_is_self_contained(self):
        html = build_html([load(PG13), load(PG17)])
        self.assertNotRegex(html, r'(?i)(src|href)\s*=\s*["\']https?:')
        self.assertNotIn("@import", html)
        m = re.search(r'<script id="planviz-data" type="application/json">(.*?)</script>', html, re.S)
        data = json.loads(m.group(1))
        self.assertEqual([p["label"] for p in data["plans"]], ["PG13", "PG17"])
        self.assertIsInstance(data["plans"][1]["plan"]["Query Identifier"], str)   # 64-bit id kept exact for JS
        self.assertIn("SUBPLAN_RESCAN", data["rules"])

    def test_script_breakout_is_escaped(self):
        obj = plan(scan("t", **{"Filter": "(name = '</script><script>alert(1)</script>')"}))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.json")
            wr(p, json.dumps(obj))
            html = build_html([load(p)])
        self.assertEqual(html.count("</script>"), 3)                         # data + core + ui only

    def test_report_summary_app(self):
        with tempfile.TemporaryDirectory() as d:
            out, js = os.path.join(d, "r.html"), os.path.join(d, "r.json")
            buf, err = io.StringIO(), io.StringIO()
            with redirect_stdout(buf), redirect_stderr(err):
                rc = main(["report", PG13, PG17, "--labels", "PG13,PG17", "-o", out, "--json", js])
            self.assertEqual(rc, 0)
            text = buf.getvalue()
            self.assertIn("SUBPLAN_RESCAN", text)
            self.assertIn("So sánh PG13 → PG17", text)
            self.assertIn("×305 chậm hơn", text)
            data = json.loads(rd(js))
            self.assertEqual(data["matches"]["0:1"][9], [9, 9])
            self.assertEqual(data["plans"][1]["insights"][0]["rule"], "SUBPLAN_RESCAN")
            app = os.path.join(d, "app.html")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(["app", "-o", app]), 0)
            self.assertIn('"example":true', rd(app))

    def test_bad_input_exit_code(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bad.txt")
            wr(p, "not a plan")
            with redirect_stderr(io.StringIO()) as err:
                self.assertEqual(main(["summary", p]), 2)
            self.assertIn("planviz:", err.getvalue())


class ExplainTests(unittest.TestCase):
    def test_read_statement_guards(self):
        with tempfile.TemporaryDirectory() as d:
            def chk(text):
                p = os.path.join(d, "q.sql")
                wr(p, text)
                return ex.read_statement(p)
            self.assertEqual(chk("update t set a = 'x;y' where b = $$;$$ and c = E'\\';' -- ; c\n;\n"),
                             "update t set a = 'x;y' where b = $$;$$ and c = E'\\';' -- ; c")
            self.assertEqual(chk("select 1 /* ; */"), "select 1 /* ; */")
            for bad in ("select 1; select 2", "select 1 \\gset", "EXPLAIN select 1", "  "):
                with self.assertRaises(ValueError, msg=bad):
                    chk(bad)

    def test_script(self):
        s = ex.build_script("update t set a = 1", analyze=True, timeout="5min")
        self.assertEqual(s.splitlines()[0], "BEGIN;")
        self.assertIn("SET LOCAL statement_timeout = '5min';", s)
        self.assertIn("EXPLAIN (ANALYZE, BUFFERS, SETTINGS, FORMAT JSON)", s)
        self.assertEqual(s.strip().splitlines()[-1], "ROLLBACK;")
        self.assertNotIn("ANALYZE", ex.build_script("select 1", analyze=False, timeout="30s"))
        with self.assertRaises(ValueError):
            ex.build_script("select 1", analyze=False, timeout="1; drop table x")

    def test_explain_end_to_end_with_fake_psql(self):
        fake = os.path.join(HERE, "fake_psql_plan.py")
        with tempfile.TemporaryDirectory() as d:
            q = os.path.join(d, "q.sql")
            wr(q, "update products set status = 'X' where status = 'PENDING';")
            plan_src = os.path.join(d, "p.json")
            wr(plan_src, json.dumps(load(PG13).obj))
            dump = os.path.join(d, "script.sql")
            os.environ["PLANVIZ_FAKE_SCRIPT"] = dump
            try:
                out_json, out_html = os.path.join(d, "o.json"), os.path.join(d, "o.html")
                buf = io.StringIO()
                with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                    rc = main(["explain", "--psql", f"{sys.executable} {fake} {plan_src}", "--dsn",
                               "host=x user=app password=secret", "--query-file", q, "--analyze", "--label", "PG13",
                               "-o", out_json, "--html", out_html])
                self.assertEqual(rc, 0)
                self.assertIn("CTE Scan", buf.getvalue())
                script = rd(dump)
                self.assertIn("EXPLAIN (ANALYZE, BUFFERS, SETTINGS, FORMAT JSON)", script)
                self.assertIn('"host=x user=app password=secret"', script)          # dsn passed as last argv
                html = rd(out_html)
                self.assertNotIn("secret", html)                                       # redacted in the page
                self.assertEqual(json.loads(rd(out_json))[0]["Plan"]["Node Type"], "ModifyTable")
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                    rc = main(["explain", "--psql", f"{sys.executable} {fake} {os.path.join(d, 'missing.json')}",
                               "--query-file", q, "-q"])
                self.assertEqual(rc, 2)
                self.assertIn("statement timeout", err.getvalue())
            finally:
                os.environ.pop("PLANVIZ_FAKE_SCRIPT", None)

    def test_redact(self):
        self.assertEqual(ex.redact("host=a password=s3cr3t user=b"), "host=a password=*** user=b")
        self.assertEqual(ex.redact("postgresql://u:pw@h/db"), "postgresql://u:***@h/db")


@unittest.skipUnless(NODE, "node not installed: JavaScript parity not checked")
class JsParityTests(unittest.TestCase):
    """assets/core.js must give the same answers as analyze.py / textplan.py."""
    FILES = None

    @classmethod
    def setUpClass(cls):
        files = [PG13, PG17, PG17_HINT, PG17_NE]
        for d in (LAB, os.path.join(LAB, "remedies"), os.path.join(REPO, "tests", "samples", "plans"),
                  os.path.join(REPO, "tests", "samples", "plans_text")):
            for f in sorted(os.listdir(d)):
                if f.endswith((".json", ".txt")) and f not in ("pgdoctor-findings.json",):
                    p = os.path.join(d, f)
                    try:
                        load(p)
                    except ValueError:
                        continue                                                  # not a plan (notes, counts)
                    files.append(p)
        cls.FILES = files

    def run_node(self, *args):
        out = subprocess.run([NODE, os.path.join(HERE, "planviz_node.js")] + list(args), capture_output=True, text=True,
                             check=True)
        return json.loads(out.stdout)

    def close(self, x, y, path, what):
        if x is None or y is None:
            self.assertEqual(x, y, f"{path}: {what}")
        else:
            self.assertLessEqual(abs(x - y), 1e-6 * max(1.0, abs(x)), f"{path}: {what} {x} != {y}")

    def test_text_parser_parity(self):
        for p in self.FILES:
            if not p.endswith(".txt"):
                continue
            py = load(p).obj
            js = self.run_node("parse", p)
            if "Query Identifier" in py:
                py["Query Identifier"] = str(py["Query Identifier"])
            self.assertEqual(json.loads(json.dumps(py)), js, p)

    def test_analysis_parity(self):
        self.assertGreater(len(self.FILES), 20)
        for p in self.FILES:
            self.check_analysis(p)

    def test_synthetic_parity(self):
        with tempfile.TemporaryDirectory() as d:
            for name, obj in (("cancel.json", cancelling_plan()), ("parts.json", partitioned_plan())):
                p = os.path.join(d, name)
                wr(p, json.dumps(obj))
                self.check_analysis(p)

    def check_analysis(self, p):
        py = A(p)
        js = self.run_node("analyze", p)["plans"][0]
        self.assertEqual(len(py["nodes"]), len(js["nodes"]), p)
        for a, b in zip(py["nodes"], js["nodes"]):
            for k in ("incl", "excl", "lq", "base_lq", "local_lq", "est_total", "act_total", "removed"):
                self.close(a[k], b[k], p, f"#{a['id']} {k}")
            for k in ("label", "object", "time_parent", "hot", "scope", "parent", "children", "loops"):
                self.assertEqual(a[k], b[k], f"{p}: #{a['id']} {k}")
            self.assertEqual(set(a["buf_excl"]), set(b["buf_excl"]), p)
            for k, v in a["buf_excl"].items():
                self.close(v, b["buf_excl"][k], p, f"#{a['id']} {k}")
        self.assertEqual([(i["rule"], i["sev"], i["node"]) for i in py["insights"]],
                         [(i["rule"], i["sev"], i["node"]) for i in js["insights"]], p)
        rules = load_rules()
        for i, j in zip(py["insights"], js["insights"]):
            self.assertEqual(render_insight(i, rules)["title"], render_insight(j, rules)["title"], p)

    def test_match_parity(self):
        pairs = [(PG13, PG17), (PG13, PG17_NE), (PG17, PG17_HINT)]
        names = sorted(f for f in os.listdir(os.path.join(REPO, "tests", "samples", "plans")) if f.endswith(".pg13.json"))
        for f in names:
            pairs.append((os.path.join(REPO, "tests", "samples", "plans", f),
                          os.path.join(REPO, "tests", "samples", "plans", f.replace(".pg13.", ".pg17."))))
        for x, y in pairs:
            self.assertEqual(match(A(x), A(y)), self.run_node("analyze", x, y)["pairs"], (x, y))


if __name__ == "__main__":
    unittest.main()
