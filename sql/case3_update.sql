-- CASE 3: CTE dùng 2 lần (tự động MATERIALIZED) + điều kiện lọc ÁP LÊN CỘT CỦA CTE ở query ngoài
-- PG13: cột của CTE Scan không có stats -> selectivity mặc định (0.333 cho >=)
-- PG17: lấy stats của orders2 TOÀN BẢNG (chưa lọc status='OPEN') -> priority>=8, created_at>=..., amount>=900 trông rất hiếm
WITH open_orders AS MATERIALIZED (
  SELECT id, customer_id, cust_code, amount, priority, created_at
  FROM orders2 WHERE status = 'OPEN'
)
UPDATE customer_stats2 cs
SET pair_cnt = s.n, open_amount = s.total, updated_at = now()
FROM (
  SELECT a.customer_id, count(*) AS n, sum(a.amount) AS total
  FROM open_orders a
  JOIN open_orders b ON b.customer_id = a.customer_id AND b.id <> a.id
  JOIN customers2 cu ON cu.name = a.cust_code
  WHERE a.priority >= 8 AND a.created_at >= timestamp '2026-10-01' AND a.amount >= 900
    AND b.priority >= 8 AND b.created_at >= timestamp '2026-10-01' AND b.amount >= 900
  GROUP BY a.customer_id
) s
WHERE cs.customer_id = s.customer_id;
