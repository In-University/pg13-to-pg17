"""Tests for the live mode (pgdoctor run / capture / qdiff): psql script generation, DSN handling, output parsing and
the per-statement effective-configuration diff (querycfg.py).

Fixtures in samples/run/ were captured from the lab (lab/remedies, PG 13.23 vs PG 17.11, role app_batch,
application_name batch-job):
  case4.pg13            EXPLAIN ANALYZE of lab/remedies/sql/q4.sql  (hashed SubPlan, 3.8 s)
  case4.pg17            EXPLAIN only                                (SubPlan not hashed)
  case4_role_hm8.pg17   EXPLAIN ANALYZE after ALTER ROLE app_batch IN DATABASE lab4 SET hash_mem_multiplier = 8
tests/fake_psql.py replays them in psql's output format so 'run' is tested end to end without a server."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, ".claude", "skills", "pgdoctor"))

from pgdoctor.cli import main  # noqa: E402
from pgdoctor.compare import compare_plans  # noqa: E402
from pgdoctor.plan import load_plan  # noqa: E402
from pgdoctor.querycfg import config_findings, query_config_diff  # noqa: E402
from pgdoctor.rules_plan import analyze_plan  # noqa: E402
from pgdoctor.runner import (augment_dsn, capture_script, context_script, load_capture, parse_sections,  # noqa: E402
                             plan_relations, read_query, redact_dsn)
from pgdoctor.settings import load_settings  # noqa: E402

RUN = os.path.join(HERE, "samples", "run")
Q4 = os.path.join(REPO, "lab", "remedies", "sql", "q4.sql")
FAKE = f'"{sys.executable}" "{os.path.join(HERE, "fake_psql.py")}"'


def _diff(old_dir, new_dir):
    co, cn = load_capture(old_dir), load_capture(new_dir)
    old, new = load_plan(co["plan_path"], 13), load_plan(cn["plan_path"], 17)
    fs = analyze_plan(old, "old", "q") + analyze_plan(new, "new", "q") + compare_plans(old, new, "q").findings
    d = query_config_diff(load_settings(co["settings_path"]), load_settings(cn["settings_path"]), old, new, fs)
    return d, fs


class TestScripts(unittest.TestCase):
    def test_explain_only_has_no_analyze_and_no_timeout(self):
        s = capture_script("UPDATE t SET x = 1")
        self.assertIn("EXPLAIN (VERBOSE, SETTINGS, FORMAT JSON) UPDATE t SET x = 1;", s)
        self.assertNotIn("ANALYZE", s)
        self.assertNotIn("statement_timeout", s)
        self.assertLess(s.index("BEGIN;"), s.index("EXPLAIN"))
        self.assertLess(s.index("EXPLAIN"), s.index("ROLLBACK;"))
        self.assertLess(s.index("@@PGDOCTOR settings@@"), s.index("BEGIN;"))   # settings of the session as the app gets it

    def test_analyze_runs_under_timeout_inside_rollback(self):
        s = capture_script("UPDATE t SET x = 1", analyze=True, timeout="10min")
        self.assertIn("SET LOCAL statement_timeout = '10min';", s)
        self.assertLess(s.index("SET LOCAL statement_timeout"), s.index("EXPLAIN (ANALYZE"))
        self.assertLess(s.index("EXPLAIN (ANALYZE"), s.index("ROLLBACK;"))
        with self.assertRaises(ValueError):
            capture_script("SELECT 1", analyze=True, timeout="10 minutes; DROP TABLE x")

    def test_params_use_prepare_execute_and_generic_plan(self):
        s = capture_script("SELECT * FROM t WHERE a = $1 AND b = $2", params=["O'Brien", "VN"], generic_plan=True)
        self.assertIn("PREPARE pgdoctor_q AS SELECT * FROM t WHERE a = $1 AND b = $2;", s)
        self.assertIn("EXECUTE pgdoctor_q('O''Brien', 'VN')", s)
        self.assertIn("SET LOCAL plan_cache_mode = force_generic_plan;", s)

    def test_context_script_filters_plan_tables_and_query_id(self):
        s = context_script([("public", "products"), ("public", "brands")], 8512646466599369567)
        self.assertIn("('public', 'products'), ('public', 'brands')", s)
        self.assertIn("WHERE queryid = 8512646466599369567", s)
        self.assertIn("\\if :pgd_has_hints", s)
        self.assertNotIn("statements@@", context_script([], None))

    def test_read_query(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "q.sql")
            with open(p, "w") as fh:
                fh.write("  UPDATE t SET x = 1 ;;\n")
            self.assertEqual(read_query(p), "UPDATE t SET x = 1")
            for bad in ("EXPLAIN ANALYZE UPDATE t SET x = 1",          # pgdoctor adds EXPLAIN itself
                        "UPDATE t SET x = 1; DROP TABLE t",             # two statements
                        "SELECT 1\n\\! rm -rf /tmp/x",                 # psql meta-command on its own line
                        "SELECT 1 \\gset"):                             # ... or mid-line
                with open(p, "w") as fh:
                    fh.write(bad)
                with self.assertRaises(ValueError, msg=bad):
                    read_query(p)
            ok = "SELECT 'a;b', E'it\\'s;', $f$ x;y $f$ FROM t -- c;\nWHERE a = $1 /* ; */"
            with open(p, "w") as fh:
                fh.write(ok)
            self.assertEqual(read_query(p), ok)


class TestDsn(unittest.TestCase):
    def test_conninfo_gets_client_settings(self):
        self.assertEqual(augment_dsn("host=h dbname=db", "batch-job", "-c work_mem=64MB"),
                         "host=h dbname=db application_name='batch-job' options='-c work_mem=64MB'")
        self.assertEqual(augment_dsn("host=h", None, None), "host=h")

    def test_uri_encodes_space_as_percent20(self):
        u = augment_dsn("postgresql://u:p@h/db?sslmode=require", "batch-job", "-c random_page_cost=2")
        self.assertIn("sslmode=require", u)
        self.assertIn("application_name=batch-job", u)
        self.assertIn("options=-c%20random_page_cost%3D2", u)   # libpq rejects '+' ("+random_page_cost")

    def test_redact(self):
        self.assertEqual(redact_dsn("host=h password='a b' user=x"), "host=h password=*** user=x")
        self.assertEqual(redact_dsn("postgresql://u:secret@h/db?password=x"), "postgresql://u:***@h/db?password=***")


class TestParse(unittest.TestCase):
    def test_sections(self):
        out = "noise\n@@PGDOCTOR meta@@\n{\"a\": 1}\n@@PGDOCTOR explain@@\n[{\"Plan\": {}}]\n@@PGDOCTOR hints@@\n\n@@PGDOCTOR end@@\n"
        s = parse_sections(out)
        self.assertEqual(s["meta"], {"a": 1})
        self.assertEqual(s["explain"], [{"Plan": {}}])
        self.assertIsNone(s["hints"])

    def test_plan_relations(self):
        with open(os.path.join(RUN, "case4.pg17", "plan.json")) as fh:
            rels = plan_relations(json.load(fh))
        self.assertEqual(set(rels), {("public", "products"), ("public", "brand_assignments"), ("public", "stores"),
                                     ("public", "brands")})


class TestQueryConfig(unittest.TestCase):
    def test_case4_config_is_not_the_cause(self):
        d, fs = _diff(os.path.join(RUN, "case4.pg13"), os.path.join(RUN, "case4.pg17"))
        key = {k["name"]: k for k in d["key"]}
        self.assertEqual(set(key), {"work_mem", "hash_mem_multiplier"})
        self.assertTrue(key["work_mem"]["same"])
        self.assertFalse(key["hash_mem_multiplier"]["same"])
        row = next(r for r in d["rows"] if r["name"] == "hash_mem_multiplier")
        self.assertEqual((row["relevance"], row["default_changed"], row["old"], row["new"]), ("critical", True, "1", "2"))
        # EXPLAIN (SETTINGS) cannot show it: 2 is PG17's own default
        self.assertNotIn("hash_mem_multiplier", d["explain_settings"]["PG17"])
        t13, t17 = d["thresholds"]
        self.assertTrue(t13["fits"] and t13["hashed"])
        self.assertFalse(t17["fits"] or t17["hashed"])
        self.assertIn("cần work_mem ≥", t17["text"])
        self.assertTrue(any("ƯỚC LƯỢNG" in v for v in d["verdict"]))
        rules = {f.rule for f in config_findings(d, ("PG13", "PG17"))}
        self.assertEqual(rules, {"QCFG-DIFF", "QCFG-VERDICT"})

    def test_one_sided_parameters_are_not_noise(self):
        d, _ = _diff(os.path.join(RUN, "case4.pg13"), os.path.join(RUN, "case4.pg17"))
        rel = {r["name"]: r["relevance"] for r in d["rows"]}
        self.assertEqual(rel.get("enable_memoize"), "other")        # no Memoize node in either plan
        self.assertEqual(rel.get("old_snapshot_threshold"), "other")
        self.assertNotIn("application_name", rel)

    def test_role_override_is_reported_with_its_source(self):
        d, fs = _diff(os.path.join(RUN, "case4.pg13"), os.path.join(RUN, "case4_role_hm8.pg17"))
        self.assertNotIn("CMP-SUBPLAN-HASH-LOST", {f.rule for f in fs})
        row = next(r for r in d["rows"] if r["name"] == "hash_mem_multiplier")
        self.assertEqual((row["relevance"], row["new"], row["new_src"]), ("plan", "8", "database user"))
        self.assertTrue(all(t["hashed"] for t in d["thresholds"]))
        self.assertTrue(any("ALTER ROLE" in v for v in d["verdict"]))


class TestCli(unittest.TestCase):
    def test_run_end_to_end_with_fake_psql(self):
        with tempfile.TemporaryDirectory() as d:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = main(["run", "--old-psql", f"{FAKE} {os.path.join(RUN, 'case4.pg13')}",
                           "--new-psql", f"{FAKE} {os.path.join(RUN, 'case4.pg17')}",
                           "--old-dsn", "host=old", "--new-dsn", "host=new", "--query-file", Q4,
                           "--name", "case4", "--analyze", "both", "--timeout", "10s", "--out-dir", d])
            self.assertEqual(rc, 0, err.getvalue())
            text = out.getvalue()
            self.assertIn("Cấu hình hiệu lực khi chạy câu này: PG13 → PG17", text)
            self.assertIn("ƯỚC LƯỢNG", text)
            self.assertTrue(os.path.isfile(os.path.join(d, "old", "plan_analyze.json")))
            self.assertFalse(os.path.isfile(os.path.join(d, "new", "plan_analyze.json")))   # 'timed out' -> EXPLAIN plan
            with open(os.path.join(d, "new", "meta.json")) as fh:
                self.assertIn("statement timeout", json.load(fh)["analyze_error"])
            with open(os.path.join(d, "report.html"), encoding="utf-8") as fh:
                html = fh.read()
            self.assertIn("id='qconfig'", html)
            self.assertIn("mặc định đổi theo phiên bản", html)
            with open(os.path.join(d, "remedies.sql"), encoding="utf-8") as fh:
                sql = fh.read()
            self.assertIn("ALTER ROLE app_batch IN DATABASE lab4", sql)         # role/db taken from the captured session
            self.assertIn("8512646466599369567", sql)
            with open(os.path.join(d, "findings.json"), encoding="utf-8") as fh:
                js = json.load(fh)
            self.assertEqual(js["labels"], ["PG13", "PG17"])
            self.assertTrue(js["query_config"]["verdict"])

    def test_qdiff_offline(self):
        with tempfile.TemporaryDirectory() as d:
            out = io.StringIO()
            with redirect_stdout(out):
                rc = main(["qdiff", "--old-dir", os.path.join(RUN, "case4.pg13"),
                           "--new-dir", os.path.join(RUN, "case4_role_hm8.pg17"), "-o", os.path.join(d, "r.html")])
            self.assertEqual(rc, 0)
            self.assertIn("database user", out.getvalue())
            self.assertTrue(os.path.isfile(os.path.join(d, "r.html")))

    def test_capture_reports_missing_psql(self):
        err = io.StringIO()
        with redirect_stderr(err), tempfile.TemporaryDirectory() as d:
            rc = main(["capture", "--psql", "/nonexistent/psql", "--dsn", "host=x", "--query-file", Q4,
                       "--out-dir", d])
        self.assertEqual(rc, 1)
        self.assertIn("psql", err.getvalue())


if __name__ == "__main__":
    unittest.main()
