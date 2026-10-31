---
name: pgdoctor
description: Diagnose PostgreSQL queries that got slower after a major-version upgrade (13 → 17, Cloud SQL in-place upgrade / pg_upgrade). Use when the user provides EXPLAIN plans of the same query from the old and new server, asks why a query regressed after upgrading, wants to compare pg_settings / statistics / pg_stat_statements between two servers, or wants a pre-upgrade risk check. Detects offline: lost hashed SubPlan (NOT IN), Nested Loop re-scanning a CTE, missing stats after pg_upgrade, PG17 CTE column-statistics side effects, ANALYZE failing under PG17's safe search_path.
---

# pgdoctor

Offline analyzer (Python ≥ 3.8, standard library only). It reads files: EXPLAIN plans, pg_settings, catalog/statistics snapshots and pg_stat_statements from the old and new server. It never connects to a database. Output: findings (rule, severity, evidence, explanation, diagnostic SQL) as a console summary, JSON, and a self-contained HTML report. Finding texts are in Vietnamese.

## Files

- `pgdoctor_cli.py`: entry point, runnable from any directory.
- `pgdoctor/`: the package (plan parser for JSON/text, rules, old-vs-new comparison, report). `pgdoctor/kb.py` is the rule knowledge base.
- `collect/pgdoctor_collect.sql`: psql collector. It reads only the catalog, needs no superuser, and writes `settings.json`, `health.json` and `statements.json`.

## Workflow

1. **Get the plan of the slow query from both servers:**
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
4. **Interpret the findings:**
   - Start with `critical`/`high` findings of scope `compare`, then plan findings with `side = new`.
   - Findings on the old plan are context only; the tool caps them at `low`.
5. **Verify before concluding.** Each rule ships a diagnostic such as `BEGIN; SET LOCAL ...; EXPLAIN ...; ROLLBACK;`. Ask the user to run it. All rules are heuristics.

## Key patterns (reproduced in this repo's lab)

| Rule | Plan signature | Fix verified in lab |
|---|---|---|
| `CMP-SUBPLAN-HASH-LOST` | Old plan: `NOT (hashed SubPlan N)`. New plan: `NOT (ANY (x = (SubPlan N).col1))`, and the subplan's `loops` equals the number of outer rows | `NOT EXISTS` (mind NULL semantics), or `work_mem` large enough that rows × (width + 24) ≤ work_mem × hash_mem_multiplier |
| `CMP-CTE-CORRELATION` | `CTE Scan … rows=1` with several filters on CTE columns, but actual rows in the thousands. Caused by PG17 borrowing base-table column stats (commit f7816aec2) | `NOT MATERIALIZED`, or a temp table followed by `ANALYZE`. `CREATE STATISTICS` does **not** help |
| `CMP-NOSTATS`, `HEALTH-NEVER-ANALYZED` | `reltuples = -1`; every predicate uses the default selectivity 0.005 | `vacuumdb --all --analyze-in-stages` |
| `HEALTH-SEARCHPATH-FUNC` | A function used by an expression index or materialized view has no pinned `search_path`. On PG17, `ANALYZE` of that table fails | `ALTER FUNCTION … SET search_path = …` |
| `PLAN-NL-RESCAN`, `PLAN-NL-BET` | The inner side of a Nested Loop has no index and is re-run thousands of times (or the outer side is estimated at about 1 row) | Fix the estimate at the node where the misestimate starts (`PLAN-MISEST`) |

## Limits

- Per-node exclusive time is approximate.
- Node matching between the two plans is based on relation and alias sets.
- Lock waits, I/O throttling and HA/replica effects are not visible in plans. Check `pg_stat_activity.wait_event` while the query runs.
