---
name: pgdoctor
description: Diagnose PostgreSQL queries that got slower after a major-version upgrade (13 → 17, Cloud SQL in-place upgrade / pg_upgrade). Use when the user provides EXPLAIN plans of the same query from the old and new server, asks why a query regressed after upgrading, wants to compare pg_settings / statistics / pg_stat_statements between two servers, or wants a pre-upgrade risk check. Detects offline: lost hashed SubPlan (NOT IN), Nested Loop re-scanning a CTE, missing stats after pg_upgrade, PG17 CTE column-statistics side effects, ANALYZE failing under PG17's safe search_path. Generates a remediation runbook that needs no application change (pg_hint_plan hint table, ALTER ROLE … IN DATABASE … SET, Cloud SQL flags) with the exact work_mem / hash_mem_multiplier threshold. Live mode (`run`) executes EXPLAIN of the statement on both servers through psql and lists the effective-configuration differences that matter for that statement's plan.
---

# pgdoctor

Analyzer (Python ≥ 3.8, standard library only). `analyze`, `remedy` and `qdiff` are offline: they read files (EXPLAIN plans, pg_settings, catalog/statistics snapshots, pg_stat_statements) from the old and new server. `run` and `capture` connect only through `psql` (configurable command), and always inside `BEGIN … ROLLBACK`. Output: findings (rule, severity, evidence, explanation, diagnostic SQL) as a console summary, JSON, and a self-contained HTML report. Finding texts are in Vietnamese.

## Files

- `pgdoctor_cli.py`: entry point, runnable from any directory.
- `pgdoctor/`: the package (plan parser for JSON/text, rules, old-vs-new comparison, report). `pgdoctor/kb.py` is the rule knowledge base.
- `collect/pgdoctor_collect.sql`: psql collector. It reads only the catalog, needs no superuser, and writes `settings.json`, `health.json` and `statements.json`.

## Workflow

0. **If both servers are reachable with psql, start here.** One command runs the statement on both servers and compares them:
   ```bash
   python <skill>/pgdoctor_cli.py run --old-dsn "<pg13>" --new-dsn "<pg17>" --query-file q.sql \
          [--app-name <application_name>] [--analyze none|old|new|both] [--timeout 30min] \
          [--param v1 --param v2 [--generic-plan]] [--client-options "-c work_mem=…"] \
          [--psql | --old-psql | --new-psql "docker exec -i <c> psql"] --out-dir run1
   ```
   - It captures `pg_settings` inside the session that runs the statement. Log in as the application role so `ALTER ROLE/DATABASE … SET` and client options are included.
   - The default is EXPLAIN only. `--analyze` runs inside `BEGIN … ROLLBACK` under `statement_timeout`, and falls back to the EXPLAIN plan on timeout. An UPDATE still holds row locks while it runs.
   - It lists the parameters that differ, ranked `critical` (part of a finding's mechanism, also shown when equal) > `plan` (governs a node type in either plan) > `planner` > `other`, each with its `source`.
   - For NOT IN SubPlans it computes the hash decision on both sides with the real session values, and states whether config or estimates caused the change.
   - `EXPLAIN (SETTINGS)` alone cannot do this: it hides version-default changes such as `hash_mem_multiplier` 1 → 2.
   - It writes `run1/report.html`, `run1/remedies.sql` (role/db taken from the session) and `run1/findings.json`.
   - If the servers cannot be reached together, run `capture --dsn … --query-file q.sql --out-dir out/pg13` on each server, then `qdiff --old-dir out/pg13 --new-dir out/pg17`.
1. **Otherwise, get the plan of the slow query from both servers:**
   ```sql
   BEGIN; EXPLAIN (ANALYZE, BUFFERS, SETTINGS, FORMAT JSON) <query>; ROLLBACK;
   ```
   - If the query runs too long on the new server, plain `EXPLAIN (SETTINGS, FORMAT JSON)` is enough. Most regression patterns are visible from estimates alone.
   - Text plans copied from psql also work.
   - Name the files `<name>.pg13.json` and `<name>.pg17.json` (labels `old`/`new` also work).
2. **Optional: collect snapshots** on each server, each into its own directory:
   ```bash
   mkdir -p out/pg13 && cd out/pg13 && psql "<conn>" -X -v ON_ERROR_STOP=1 -f <skill>/collect/pgdoctor_collect.sql
   ```
3. **Analyze:**
   ```bash
   python <skill>/pgdoctor_cli.py analyze --plans-dir plans/ --old-dir out/pg13 --new-dir out/pg17 -o report.html --json findings.json
   ```
   - Single files work too: `--old-plan/--new-plan`, `--old-settings/--new-settings`, `--old-health/--new-health`, `--old-statements/--new-statements`.
   - `--fail-on high` exits with code 2 when a finding of that severity or higher exists (for CI).
   - `python <skill>/pgdoctor_cli.py kb` lists all rules.
4. **Get fixes that need no code change** (the app often cannot be rebuilt quickly):
   ```bash
   python <skill>/pgdoctor_cli.py remedy --plan q.pg17.json [--old-plan q.pg13.json] \
          --app-role <role> --app-db <db> [--app-name <application_name>] [--keyword <text>] -o fix.sql
   ```
   - Options are ordered narrowest first: hint table (one statement, key = `query_id`) → `ALTER ROLE … IN DATABASE … SET` → instance flag → code change (`NOT EXISTS`).
   - For a non-hashed `NOT IN` SubPlan it computes the exact minimum: `rows × (MAXALIGN(width) + 24) ≤ work_mem × hash_mem_multiplier` (matches the lab to the kB) and recommends 2× that.
   - `analyze --remedies fix.sql` writes the same runbook for every query; the HTML report has a *Khắc phục* section.
   - If the plan has no `EXPLAIN (SETTINGS)`, pass the real session values with `--work-mem` / `--hash-mem-multiplier`.
5. **Interpret the findings:**
   - Start with `critical`/`high` findings of scope `compare`, then plan findings with `side = new`.
   - Findings on the old plan are context only; the tool caps them at `low`.
6. **Verify before concluding.** Each rule ships a diagnostic such as `BEGIN; SET LOCAL ...; EXPLAIN ...; ROLLBACK;`. Ask the user to run it. All rules are heuristics.

## Key patterns (reproduced in this repo's lab)

| Rule | Plan signature | Fix verified in lab |
|---|---|---|
| `CMP-SUBPLAN-HASH-LOST` | Old plan: `NOT (hashed SubPlan N)`. New plan: `NOT (ANY (x = (SubPlan N).col1))`, and the subplan's `loops` equals the number of outer rows | `NOT EXISTS` (mind NULL semantics), or `work_mem` large enough that rows × (width + 24) ≤ work_mem × hash_mem_multiplier |
| `CMP-CTE-CORRELATION` | `CTE Scan … rows=1` with several filters on CTE columns, but actual rows in the thousands. Caused by PG17 borrowing base-table column stats (commit f7816aec2) | `NOT MATERIALIZED`, or a temp table followed by `ANALYZE`. `CREATE STATISTICS` does **not** help |
| `CMP-NOSTATS`, `HEALTH-NEVER-ANALYZED` | `reltuples = -1`; every predicate uses the default selectivity 0.005 | `vacuumdb --all --analyze-in-stages` |
| `HEALTH-SEARCHPATH-FUNC` | A function used by an expression index or materialized view has no pinned `search_path`. On PG17, `ANALYZE` of that table fails | `ALTER FUNCTION … SET search_path = …` |
| `PLAN-CTE-COST` | "SELECT from the CTE is slow": splits CTE body time (runs once) from re-reading the stored result (`loops` ≫ 1) | Re-reads dominate → fix the SubPlan / Nested Loop, indexes on the CTE body do not help |
| `PLAN-NL-RESCAN`, `PLAN-NL-BET` | The inner side of a Nested Loop has no index and is re-run thousands of times (or the outer side is estimated at about 1 row) | Fix the estimate at the node where the misestimate starts (`PLAN-MISEST`) |

## Remedies verified in `lab/remedies` (PG 17.11 + pg_hint_plan 1.7.2)

- Hint table `Set(hash_mem_multiplier N)` / `Set(work_mem …)` keyed by `query_id`: plan-time only, can be limited to one `application_name`. `CREATE EXTENSION pg_hint_plan` must run with `pg_hint_plan.enable_hint_table = off`. Literal vs bind-parameter versions of a statement have different `query_id`s: take it from `pg_stat_activity.query_id` / `pg_stat_statements.queryid`.
- `ALTER ROLE … IN DATABASE … SET` beats `ALTER ROLE` beats `ALTER DATABASE`; client `options -c` beats all. New sessions only (recycle the pool).
- Not effective: indexes, re-ANALYZE, `SET STATISTICS 0` (old stats stay), `CREATE STATISTICS`, reverting app code. Cloud SQL has no LLVM JIT.

## Limits

- Per-node exclusive time is approximate.
- Node matching between the two plans is based on relation and alias sets.
- Lock waits, I/O throttling and HA/replica effects are not visible in plans. Check `pg_stat_activity.wait_event` while the query runs.
