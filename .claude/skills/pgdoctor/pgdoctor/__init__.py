"""pgdoctor - offline analyzer for PostgreSQL major-upgrade query regressions (13 -> 17).

Inputs (all offline files, collected with the scripts in ``collect/``):
  * EXPLAIN plans of the same query on the old and new server (JSON or text format)
  * pg_settings snapshots of both servers
  * catalog/statistics "health" snapshots of both servers
  * pg_stat_statements snapshots of both servers

Output: findings (rule id, severity, evidence, explanation, recommended action) as a
self-contained HTML report and/or JSON.  Standard library only.
"""

__version__ = "0.2.0"
