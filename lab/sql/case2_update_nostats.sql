-- CASE 2: UPDATE + 2 CTE MATERIALIZED (nhiều điều kiện lọc kiểu BETWEEN/LIKE) + join nhiều bảng
-- Khi planner không có stats: mỗi CTE bị ước lượng ~1 dòng -> join 2 CTE bằng Nested Loop
-- (CTE Scan không có index, bị quét lại cho mỗi dòng bên ngoài). Có stats: Hash Join.
WITH open_orders AS MATERIALIZED (
  SELECT o.customer_id, o.cust_code, o.amount
  FROM orders o
  WHERE o.status = 'OPEN'
    AND o.amount BETWEEN 100 AND 100000
    AND o.created_at BETWEEN timestamp '2024-03-01' AND timestamp '2030-01-01'
    AND o.note LIKE 'xx%'
), hot_complaints AS MATERIALIZED (
  SELECT c.customer_id, c.severity
  FROM complaints c
  WHERE c.severity BETWEEN 3 AND 5
    AND c.customer_id BETWEEN 1 AND 1000000
    AND c.body LIKE 'y%'
)
UPDATE customer_stats cs
SET open_pairs = s.n_pairs, open_amount = s.total, updated_at = now()
FROM (
  SELECT oo.customer_id, count(*) AS n_pairs, sum(oo.amount) AS total
  FROM open_orders oo
  JOIN hot_complaints hc ON hc.customer_id = oo.customer_id
  JOIN customers cu      ON cu.name = oo.cust_code
  GROUP BY oo.customer_id
) s
WHERE cs.customer_id = s.customer_id;
