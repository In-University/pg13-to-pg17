-- Dữ liệu mẫu nhỏ cho pgdoctor (database pgdoctor_fx). Chạy giống nhau trên PG13 và PG17.
-- Index tạo TRƯỚC khi nạp dữ liệu để không cập nhật reltuples; autovacuum tắt cho ev/acct_stats
-- => trên PG17 hai bảng này ở đúng trạng thái "vừa pg_upgrade, chưa ANALYZE" cho tới khi ta ANALYZE.
\set ON_ERROR_STOP on
DROP TABLE IF EXISTS ev, acct_stats, t1, t2, tenk, ord, cmp CASCADE;

-- A: hai CTE MATERIALIZED join nhau + UPDATE (pattern trong thread pgsql-hackers 2023)
CREATE TABLE ev (id int PRIMARY KEY, cust int NOT NULL, kind text NOT NULL, region text NOT NULL,
                 amount numeric(10,2) NOT NULL, flag bool NOT NULL, created date NOT NULL) WITH (autovacuum_enabled = false);
CREATE TABLE acct_stats (cust int PRIMARY KEY, score numeric) WITH (autovacuum_enabled = false);
INSERT INTO ev SELECT i, (random()*20000)::int, CASE WHEN i % 10 = 0 THEN 'refund' ELSE 'sale' END,
       CASE WHEN i % 4 = 0 THEN 'APAC' ELSE 'EU' END, (random()*1000)::numeric(10,2), (i % 3 = 0),
       date '2024-01-01' + (i % 500)
FROM generate_series(1, 300000) i;
INSERT INTO acct_stats SELECT g, 0 FROM generate_series(0, 20000) g;

-- B: BUG #18466 (ước lượng nhóm trên cột CTE)
CREATE TABLE t1(a int); CREATE TABLE t2(b int); CREATE INDEX t1_a_idx ON t1(a);
INSERT INTO t1 SELECT generate_series(1, 100000) FROM generate_series(1, 3);
INSERT INTO t2 SELECT generate_series(1, 100) FROM generate_series(1, 10);

-- C: pathkeys của CTE (PG17 a65724dfa)
CREATE TABLE tenk (unique1 int, two int, ten int, filler text);
CREATE INDEX tenk_unique1 ON tenk(unique1);
INSERT INTO tenk SELECT i, i % 2, i % 10, repeat('z', 50) FROM generate_series(0, 9999) i;

-- D: tập con tập trung (skew) – PG17 lấy stats toàn bảng cho cột CTE
CREATE TABLE ord (id int PRIMARY KEY, cust int NOT NULL, status text NOT NULL, amount numeric(10,2) NOT NULL);
CREATE TABLE cmp (id int PRIMARY KEY, cust int NOT NULL, sev int NOT NULL);
CREATE INDEX cmp_cust_idx ON cmp(cust);
INSERT INTO ord SELECT i, CASE WHEN i % 50 = 0 THEN 1 + (random()*49)::int ELSE 1 + (random()*99999)::int END,
       CASE WHEN i % 50 = 0 THEN 'OPEN' ELSE 'CLOSED' END, (random()*1000)::numeric(10,2)
FROM generate_series(1, 200000) i;
INSERT INTO cmp SELECT i, 1 + (random()*49)::int, (random()*5)::int FROM generate_series(1, 100000) i;

ANALYZE t1, t2, tenk, ord, cmp;
