with my_cte as materialized (select b from t2) select * from t1 where t1.a in (select b from my_cte)
