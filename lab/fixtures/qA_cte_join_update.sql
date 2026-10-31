WITH refunds AS MATERIALIZED (
  SELECT cust, sum(amount) AS amt FROM ev
  WHERE kind = 'refund' AND region = 'APAC' AND flag AND created >= date '2024-03-01' AND amount > 10
  GROUP BY cust
), sales AS MATERIALIZED (
  SELECT cust, sum(amount) AS amt FROM ev
  WHERE kind = 'sale' AND region = 'APAC' AND NOT flag AND created >= date '2024-03-01'
  GROUP BY cust
)
UPDATE acct_stats s SET score = r.amt / NULLIF(x.amt, 0)
FROM refunds r JOIN sales x ON x.cust = r.cust
WHERE s.cust = r.cust
