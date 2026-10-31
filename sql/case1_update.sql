-- CASE 1: UPDATE ... FROM (CTE MATERIALIZED -> join -> GROUP BY) trên tập con "OPEN" tập trung vào key account
WITH open_orders AS MATERIALIZED (
  SELECT o.customer_id, o.amount
  FROM orders o
  WHERE o.status = 'OPEN'
), per_customer AS MATERIALIZED (
  SELECT oo.customer_id, count(*) AS n_pairs, sum(oo.amount) AS s
  FROM open_orders oo
  JOIN complaints c ON c.customer_id = oo.customer_id
  GROUP BY oo.customer_id
)
UPDATE customer_stats cs
SET open_pairs = p.n_pairs, open_amount = p.s, updated_at = now()
FROM per_customer p
WHERE cs.customer_id = p.customer_id;
