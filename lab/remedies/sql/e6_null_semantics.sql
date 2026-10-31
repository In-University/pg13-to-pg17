-- E6: NOT IN và NOT EXISTS khác nhau khi có NULL (bảng nhỏ, chạy ở mọi phiên bản)
WITH t(x) AS (VALUES ('A'), ('B'), (NULL)),
     s_nonull(v) AS (VALUES ('A')),
     s_null(v)   AS (VALUES ('A'), (NULL)),
     s_empty(v)  AS (SELECT 'A' WHERE false)
SELECT x,
       x NOT IN (SELECT v FROM s_nonull)                              AS notin_s,
       NOT EXISTS (SELECT 1 FROM s_nonull WHERE v = x)                AS notexists_s,
       x NOT IN (SELECT v FROM s_null)                                AS notin_s_has_null,
       NOT EXISTS (SELECT 1 FROM s_null WHERE v = x)                  AS notexists_s_has_null,
       (NOT EXISTS (SELECT 1 FROM s_null WHERE v = x)
          AND (x IS NOT NULL OR NOT EXISTS (SELECT 1 FROM s_null))
          AND NOT EXISTS (SELECT 1 FROM s_null WHERE v IS NULL))       AS nullsafe_s_has_null,
       x NOT IN (SELECT v FROM s_empty)                               AS notin_empty,
       (NOT EXISTS (SELECT 1 FROM s_empty WHERE v = x)
          AND (x IS NOT NULL OR NOT EXISTS (SELECT 1 FROM s_empty))
          AND NOT EXISTS (SELECT 1 FROM s_empty WHERE v IS NULL))      AS nullsafe_empty
FROM t;
