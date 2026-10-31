-- CASE 3 (DB "lab"): dữ liệu TƯƠNG QUAN: đơn OPEN đều là đơn MỚI, ƯU TIÊN CAO và GIÁ TRỊ LỚN (>=900),
-- nhưng trên toàn bảng thì đơn mới / ưu tiên cao là thiểu số.
\set ON_ERROR_STOP on
-- Tham số: rows = tổng số đơn, open_mod = cứ open_mod đơn có 1 đơn OPEN
-- Mặc định (nhỏ): rows=2000000 open_mod=200 -> 10k OPEN.  Lớn: -v rows=7000000 -v open_mod=100 -> 70k OPEN
\if :{?rows} \else \set rows 2000000 \endif
\if :{?open_mod} \else \set open_mod 200 \endif
DROP TABLE IF EXISTS orders2, customers2, customer_stats2;
CREATE TABLE customers2 (id int PRIMARY KEY, name text NOT NULL, segment smallint);
INSERT INTO customers2 SELECT i, 'cust-' || i, i % 50 FROM generate_series(1, 500000) i;

CREATE TABLE orders2 (
  id bigint PRIMARY KEY, customer_id int NOT NULL, cust_code text NOT NULL,
  status text NOT NULL, priority smallint NOT NULL, amount numeric(12,2) NOT NULL,
  created_at timestamp NOT NULL
);
-- 2M đơn; 0.5% OPEN (10k) thuộc 500 key account, tạo trong 15 ngày gần nhất, priority 8..10
-- 99.5% CLOSED: rải 500k khách, 600 ngày, priority 1..7 (vài % là 8..10)
INSERT INTO orders2
SELECT i, cid, 'cust-' || cid,
       CASE WHEN is_open THEN 'OPEN' ELSE 'CLOSED' END,
       CASE WHEN is_open THEN 8 + (i % 3) ELSE CASE WHEN i % 400 = 1 THEN 9 ELSE 1 + (i % 7) END END,
       CASE WHEN is_open THEN 900 + ((i::bigint * 7919) % 10000) / 100.0
            ELSE ((i::bigint * 7919) % 100000) / 100.0 END,
       CASE WHEN is_open THEN timestamp '2026-10-15' + ((i % 15) * interval '1 day')
            ELSE timestamp '2025-01-01' + ((i::bigint * 104729) % 600) * interval '1 day' END
FROM (SELECT i, (i % :open_mod = 0) AS is_open,
             CASE WHEN i % :open_mod = 0 THEN 1 + ((i / :open_mod) % 500) ELSE 1 + ((i::bigint * 2654435761) % 500000)::int END AS cid
      FROM generate_series(1, :rows) i) g;
CREATE INDEX ON orders2(customer_id);
CREATE INDEX ON orders2(status);

CREATE TABLE customer_stats2 (customer_id int PRIMARY KEY, pair_cnt bigint, open_amount numeric, updated_at timestamp);
INSERT INTO customer_stats2 SELECT id, 0, 0, now() FROM customers2;
VACUUM ANALYZE;
