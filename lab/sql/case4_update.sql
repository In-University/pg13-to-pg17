-- CASE 4: mẫu câu của dự án: CTE a NOT MATERIALIZED + CTE current_brand_code (join nhiều bảng) + UPDATE ... NOT IN (subquery)
-- current_brand_code được MATERIALIZED (giống khi CTE được tham chiếu >1 lần hoặc ghi rõ MATERIALIZED)
SET work_mem = '4MB';   -- mặc định của PostgreSQL / nhiều tier Cloud SQL
WITH a AS NOT MATERIALIZED (
  SELECT s.id, s.region FROM stores s WHERE s.active
), current_brand_code AS MATERIALIZED (
  SELECT b.code AS brand_code, a.region
  FROM brand_assignments ba
  JOIN brands b ON b.id = ba.brand_id
  JOIN a        ON a.id = ba.store_id
  WHERE ba.is_current
)
UPDATE products p
SET status = 'ORPHAN', updated_at = now()
WHERE p.status = 'PENDING'
  AND p.brand_code NOT IN (
        SELECT cbc.brand_code FROM current_brand_code cbc WHERE cbc.region = 'VN'
      );
