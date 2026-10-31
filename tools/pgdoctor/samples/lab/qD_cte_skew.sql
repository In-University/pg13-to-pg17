WITH open_o AS MATERIALIZED (SELECT cust, amount FROM ord WHERE status = 'OPEN')
SELECT o.cust, count(*), sum(o.amount)
FROM open_o o JOIN cmp c ON c.cust = o.cust
GROUP BY o.cust
