with x as materialized (select unique1 from tenk b order by unique1) select count(*) from tenk a where unique1 in (select * from x)
