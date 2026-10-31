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
  AND NOT EXISTS (
        SELECT 1 FROM current_brand_code cbc
        WHERE cbc.region = 'VN' AND cbc.brand_code = p.brand_code
      )
