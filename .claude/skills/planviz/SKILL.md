---
name: planviz
description: Analyse and visualise PostgreSQL EXPLAIN plans. Use when the user shares EXPLAIN / EXPLAIN ANALYZE output (JSON or text, psql copy, auto_explain log, pgdoctor capture directory) and wants to know where the time goes, how far the planner's row estimates are from reality (×N factors), which node the misestimate is born at, or wants to compare two or more plans of the same statement (e.g. PG13 vs PG17, before/after a fix). Produces a console summary for the agent and one self-contained offline HTML page (plan diagram with pan/zoom where edge width is the share of runtime and the top-3 hot spots are numbered, depesz-style tree table, estimate-vs-actual log-log chart, time icicle, plan comparison, node inspector) that also accepts pasted or dropped plans in the browser.
---

# planviz

Python ≥ 3.8, standard library only. The HTML page embeds its own JavaScript port of the analysis
(`planviz/assets/core.js`), so it works offline and analyses plans pasted into it; nothing is sent anywhere.
Texts shown to the user are in Vietnamese (full documentation: `docs/planviz.md`).

## Commands

```bash
CLI=".claude/skills/planviz/planviz_cli.py"
python $CLI summary plan.json                       # console only: hot spots, misestimates, findings
python $CLI report a.pg13.json b.pg17.txt -o planviz.html [--labels PG13,PG17] [--json analysis.json]
python $CLI app -o planviz.html                     # the empty tool page (with an example) to paste plans into
python $CLI explain --dsn "<conninfo>" --query-file q.sql [--analyze] [--timeout 30min] -o plan.json --html p.html
```

A PLAN argument may be: EXPLAIN (FORMAT JSON) output, text EXPLAIN (raw or copied from psql with its decoration),
an auto_explain log entry, a directory written by `pgdoctor capture`/`run` (uses plan_analyze.json, else plan.json),
or `-` for stdin. Labels default to `.pgNN.` in the file name or the captured server version.

## Workflow

1. Ask for `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` when possible (text is fine). For DML, ANALYZE must run in
   `BEGIN … ROLLBACK`; `explain --analyze` does that with `SET LOCAL statement_timeout` and warns that row locks are
   held while it runs. Without `--analyze` the statement is never executed.
2. Run `summary` (or `report`, which also prints it) and read: the top exclusive-time nodes, the nodes where the
   estimation error is *born* (local error), and the findings (rule id, severity, why, fix).
3. With two or more plans of the same statement, the summary ends with a comparison: execution-time ratio, matched
   nodes ordered by change of exclusive time (with estimate → actual and loops on both sides), and nodes that exist in
   only one plan (shape change).
4. Give the user the HTML path; mention that the page works offline and accepts more plans by paste / drag-and-drop.

## What the numbers mean

- Rows are per loop, as EXPLAIN prints them; `q` = actual/estimate per loop, shown as `↑N×` (under-estimate) or
  `↓N×` (over-estimate).
- Exclusive time = inclusive time (× loops; parallel workers counted once) minus children. CTE plans are charged to
  the CTE Scan that runs them; InitPlans to the node that evaluates their output (`(InitPlan n)` / `$n`).
- Local error = q of the node ÷ what its inputs carry (join: outer × inner; semi/anti: outer; CTE Scan: the CTE's
  own q; plain Aggregate: nothing). A large local error marks the predicate / join condition whose selectivity the
  planner got wrong; MISEST is raised only where the node's own estimate is also ≥10× off (not where it cancels an
  inherited error).
- Diagram: edge width = share of runtime in the child's subtree (or rows, log scale); bar under each node = exclusive
  (strong) and inclusive (faint) share; ①②③ = the three largest exclusive-time nodes (≥5%); "Đến điểm nóng" centres
  on ①. Branches under 1% of the runtime fold into "+N node" stubs (automatic above 30 nodes).
- Findings: SUBPLAN_RESCAN (non-hashed NOT IN / correlated SubPlan, with the hash-memory threshold), RESCAN, MISEST,
  HOTSPOT, SORT_SPILL, HASH_SPILL, HASHAGG_SPILL, TEMP_IO, FILTER_WASTE, LOSSY_BITMAP, HEAP_FETCHES, WORKERS, MEMOIZE,
  JIT, TRIGGERS, PLANNING, NO_ANALYZE, NO_TIMING, NEVER_EXECUTED. Texts live in `planviz/assets/rules.json`.

For upgrade regressions that need configuration diffs, statistics and remediation SQL, use the `pgdoctor` skill;
planviz is the plan-level microscope.

## Files

- `planviz/loader.py`, `textplan.py`: input detection and the text EXPLAIN parser (vendored from pgdoctor).
- `planviz/analyze.py`: metrics, findings, plan matching. `assets/core.js` is its JavaScript port: change both;
  `tests/test_planviz.py` checks they agree (needs `node`).
- `planviz/render.py` + `assets/shell.html`, `style.css`, `ui.js`, `rules.json`: the page. `assets/demo/`: example plans.
- `planviz/explain.py`: live mode through psql.
