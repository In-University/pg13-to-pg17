-- E1: tìm ngưỡng work_mem để PG chọn lại hashed SubPlan, đối chiếu công thức
--     rows × (MAXALIGN(width) + MAXALIGN(SizeofHeapTupleHeader=23 → 24)) ≤ work_mem × hash_mem_multiplier
\set QUIET on
\pset tuples_only on
CREATE OR REPLACE FUNCTION pg_temp.is_hashed(wm text) RETURNS text LANGUAGE plpgsql AS $f$
DECLARE l text; h boolean := false;
BEGIN
  EXECUTE format('SET LOCAL work_mem = %L', wm);
  FOR l IN EXECUTE $q$EXPLAIN WITH a AS NOT MATERIALIZED (
      SELECT s.id, s.region FROM stores s WHERE s.active
    ), current_brand_code AS MATERIALIZED (
      SELECT b.code AS brand_code, a.region FROM brand_assignments ba
      JOIN brands b ON b.id = ba.brand_id JOIN a ON a.id = ba.store_id WHERE ba.is_current)
    UPDATE products p SET status = 'ORPHAN', updated_at = now()
    WHERE p.status = 'PENDING' AND p.brand_code NOT IN (
      SELECT cbc.brand_code FROM current_brand_code cbc WHERE cbc.region = 'VN')$q$ LOOP
    IF l ~ 'hashed SubPlan' THEN h := true; END IF;
  END LOOP;
  RETURN wm || ' -> ' || CASE WHEN h THEN 'hashed SubPlan' ELSE 'SubPlan (linear)' END;
END $f$;
BEGIN;
SELECT 'version: ' || current_setting('server_version') || ', hash_mem_multiplier=' || current_setting('hash_mem_multiplier');
SELECT pg_temp.is_hashed(x) FROM unnest(:'WMS'::text[]) x;
ROLLBACK;
