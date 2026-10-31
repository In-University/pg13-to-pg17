-- pgdoctor collector – chạy trên TỪNG server (PG13 trước upgrade / clone, PG17 sau upgrade).
-- Chỉ đọc catalog/thống kê; KHÔNG đọc dữ liệu bảng; KHÔNG xuất giá trị MCV/histogram.
-- Ghi 3 file JSON vào thư mục hiện tại:  settings.json, health.json, statements.json (nếu có pg_stat_statements)
--
--   mkdir -p out/pg13 && cd out/pg13
--   psql "host=... dbname=<db> user=<user>" -X -v ON_ERROR_STOP=1 -f ../../collect/pgdoctor_collect.sql
--
-- Tương thích PG13 → PG18. Không cần superuser (dùng pg_stats / pg_stats_ext thay vì pg_statistic).
\set QUIET on
\pset format unaligned
\pset tuples_only on
\pset pager off

SELECT current_setting('server_version_num')::int >= 150000 AS v15 \gset
SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements') AS has_pgss \gset

-- ----------------------------------------------------------------- settings
\o settings.json
SELECT json_build_object(
  'server_version_num', current_setting('server_version_num')::int,
  'collected_at', now(),
  'settings', (SELECT json_agg(json_build_object(
       'name', name, 'setting', setting, 'unit', unit, 'source', source,
       'boot_val', boot_val, 'reset_val', reset_val, 'vartype', vartype) ORDER BY name)
     FROM pg_settings));
\o

-- ------------------------------------------------------------------- health
\if :v15
SELECT json_build_object('datcollversion', datcollversion,
                         'actual', pg_database_collation_actual_version(oid))::text AS dbcoll
FROM pg_database WHERE datname = current_database() \gset
\else
SELECT '{}'::text AS dbcoll \gset
\endif

\o health.json
SELECT json_build_object(
  'server_version_num', current_setting('server_version_num')::int,
  'server_version', version(),
  'database', current_database(),
  'collected_at', now(),
  'default_statistics_target', current_setting('default_statistics_target'),
  'tables', (SELECT coalesce(json_agg(t ORDER BY t.size_bytes DESC), '[]'::json) FROM (
      SELECT n.nspname AS schema, c.relname AS name, c.relkind, c.reltuples, c.relpages,
             pg_total_relation_size(c.oid) AS size_bytes, c.relispartition,
             s.last_analyze, s.last_autoanalyze, s.last_vacuum, s.last_autovacuum,
             s.n_live_tup, s.n_dead_tup, s.n_mod_since_analyze,
             EXISTS (SELECT 1 FROM pg_stats ps WHERE ps.schemaname = n.nspname AND ps.tablename = c.relname) AS has_stats
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
      LEFT JOIN pg_stat_all_tables s ON s.relid = c.oid
      WHERE c.relkind IN ('r','p','m')
        AND n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%'
        AND n.nspname NOT LIKE 'pg_temp%') t),
  'ext_stats', (SELECT coalesce(json_agg(x), '[]'::json) FROM (
      SELECT sn.nspname AS schema, s.stxname AS name, s.stxrelid::regclass::text AS table, s.stxkind::text AS kinds,
             EXISTS (SELECT 1 FROM pg_stats_ext e WHERE e.statistics_schemaname = sn.nspname
                                                    AND e.statistics_name = s.stxname) AS has_data
      FROM pg_statistic_ext s JOIN pg_namespace sn ON sn.oid = s.stxnamespace) x),
  'index_functions', (SELECT coalesce(json_agg(x), '[]'::json) FROM (
      SELECT DISTINCT 'index' AS kind, i.indexrelid::regclass::text AS object, i.indrelid::regclass::text AS table,
             p.oid::regprocedure::text AS func,
             l.lanname AS lang, p.proconfig,
             EXISTS (SELECT 1 FROM pg_depend e WHERE e.classid = 'pg_proc'::regclass AND e.objid = p.oid AND e.deptype = 'e') AS is_extension
      FROM pg_index i
      JOIN pg_depend d ON d.classid = 'pg_class'::regclass AND d.objid = i.indexrelid AND d.refclassid = 'pg_proc'::regclass
      JOIN pg_proc p ON p.oid = d.refobjid
      JOIN pg_namespace pn ON pn.oid = p.pronamespace
      JOIN pg_language l ON l.oid = p.prolang
      WHERE pn.nspname NOT IN ('pg_catalog','information_schema')
      UNION
      SELECT DISTINCT 'matview', c.oid::regclass::text, c.oid::regclass::text, p.oid::regprocedure::text, l.lanname, p.proconfig,
             EXISTS (SELECT 1 FROM pg_depend e WHERE e.classid = 'pg_proc'::regclass AND e.objid = p.oid AND e.deptype = 'e')
      FROM pg_class c
      JOIN pg_rewrite r ON r.ev_class = c.oid
      JOIN pg_depend d ON d.classid = 'pg_rewrite'::regclass AND d.objid = r.oid AND d.refclassid = 'pg_proc'::regclass
      JOIN pg_proc p ON p.oid = d.refobjid
      JOIN pg_namespace pn ON pn.oid = p.pronamespace
      JOIN pg_language l ON l.oid = p.prolang
      WHERE c.relkind = 'm' AND pn.nspname NOT IN ('pg_catalog','information_schema')) x),
  'extensions', (SELECT coalesce(json_agg(x), '[]'::json) FROM (
      SELECT e.extname AS name, e.extversion AS installed, a.default_version
      FROM pg_extension e LEFT JOIN pg_available_extensions a ON a.name = e.extname) x),
  'invalid_indexes', (SELECT coalesce(json_agg(json_build_object('index', indexrelid::regclass::text,
                                                                 'table', indrelid::regclass::text)), '[]'::json)
                      FROM pg_index WHERE NOT indisvalid),
  'collation', (SELECT (:'dbcoll')::jsonb || jsonb_build_object('mismatched', coalesce(jsonb_agg(jsonb_build_object(
                    'collname', collname, 'collversion', collversion,
                    'actual', pg_collation_actual_version(oid))), '[]'::jsonb))
                FROM pg_collation
                WHERE collversion IS NOT NULL AND collversion IS DISTINCT FROM pg_collation_actual_version(oid)),
  'column_stats', (SELECT coalesce(json_agg(x), '[]'::json) FROM (
      SELECT schemaname, tablename, attname, inherited, null_frac, n_distinct, correlation,
             coalesce(array_length(most_common_freqs, 1), 0) AS mcv_count
      FROM pg_stats
      WHERE schemaname NOT IN ('pg_catalog','information_schema') AND schemaname NOT LIKE 'pg_toast%') x),
  'column_targets', (SELECT coalesce(json_agg(x), '[]'::json) FROM (
      SELECT a.attrelid::regclass::text AS table, a.attname AS column, a.attstattarget AS target
      FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE a.attnum > 0 AND NOT a.attisdropped AND coalesce(a.attstattarget, -1) >= 0
        AND c.relkind IN ('r','p','m') AND n.nspname NOT IN ('pg_catalog','information_schema')) x)
);
\o

-- --------------------------------------------------------- pg_stat_statements
\if :has_pgss
\o statements.json
SELECT json_build_object(
  'server_version_num', current_setting('server_version_num')::int,
  'collected_at', now(),
  'statements', (SELECT coalesce(json_agg(x), '[]'::json) FROM (
      SELECT queryid, query, calls, total_exec_time, mean_exec_time, rows,
             shared_blks_hit, shared_blks_read, temp_blks_written
      FROM pg_stat_statements
      WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
      ORDER BY total_exec_time DESC
      LIMIT 3000) x));
\o
\else
\echo 'pg_stat_statements chưa được cài – bỏ qua statements.json'
\endif
\echo 'pgdoctor: đã ghi settings.json, health.json (và statements.json nếu có) vào thư mục hiện tại'
