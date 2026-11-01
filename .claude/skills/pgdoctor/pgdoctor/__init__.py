"""pgdoctor - offline analyzer for PostgreSQL major-upgrade query regressions (13 -> 17).

Inputs (all offline files, collected with the scripts in ``collect/``):
  * EXPLAIN plans of the same query on the old and new server (JSON or text format)
  * pg_settings snapshots of both servers
  * catalog/statistics "health" snapshots of both servers
  * pg_stat_statements snapshots of both servers

Live mode (``run`` / ``capture``): runs the statement itself on both servers through ``psql`` and captures the
plan plus the effective configuration of that very session, then lists the parameters that differ AND matter for
that plan (``querycfg.py``).

Output: findings (rule id, severity, evidence, explanation, recommended action) as a
self-contained HTML report and/or JSON.  Standard library only.
"""

__version__ = "0.3.0"
