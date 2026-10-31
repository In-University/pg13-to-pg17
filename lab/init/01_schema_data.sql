-- Dữ liệu tổng hợp để tái hiện regression PG13 -> PG17 (CTE + MATERIALIZED + UPDATE ... FROM)
-- Đặc điểm cố ý: phân bố LỆCH/TẬP TRUNG. Đơn hàng 'OPEN' chỉ thuộc ~500 khách hàng "key account",
-- trong khi toàn bảng orders có ~500k khách khác nhau (n_distinct toàn bảng >> n_distinct trong tập con OPEN).
\set ON_ERROR_STOP on
\timing on
SET synchronous_commit = off;

CREATE TABLE customers (
  id        int PRIMARY KEY,
  segment   smallint NOT NULL,
  name      text NOT NULL
);
INSERT INTO customers
SELECT i, (random()*50)::int, 'cust-' || i FROM generate_series(1, 500000) i;

CREATE TABLE orders (
  id          bigint PRIMARY KEY,
  customer_id int NOT NULL,
  cust_code   text NOT NULL,        -- khoá chuỗi legacy ('cust-<id>'), KHÔNG có index ở customers.name
  status      text NOT NULL,
  amount      numeric(12,2) NOT NULL,
  created_at  timestamp NOT NULL,
  note        text
);
-- 2M đơn: 2% OPEN thuộc 500 key account (id 1..500); phần còn lại CLOSED rải đều 500k khách
INSERT INTO orders
SELECT i, cid, 'cust-' || cid,
       CASE WHEN i % 50 = 0 THEN 'OPEN' ELSE 'CLOSED' END,
       (random()*1000)::numeric(12,2),
       timestamp '2024-01-01' + (random()*600) * interval '1 day',
       repeat('x', 40)
FROM (SELECT i, CASE WHEN i % 50 = 0 THEN 1 + (random()*499)::int
                     ELSE 1 + (random()*499999)::int END AS cid
      FROM generate_series(1, 2000000) i) g;
CREATE INDEX orders_customer_idx ON orders(customer_id);
CREATE INDEX orders_status_idx   ON orders(status);

-- complaints: 1M dòng chỉ thuộc 500 key account (mỗi khách ~2000 dòng) -> n_distinct(customer_id)=500
CREATE TABLE complaints (
  id          bigint PRIMARY KEY,
  customer_id int NOT NULL,
  severity    smallint NOT NULL,
  body        text
);
INSERT INTO complaints
SELECT i, 1 + (random()*499)::int, (random()*5)::int, repeat('y', 40)
FROM generate_series(1, 1000000) i;
CREATE INDEX complaints_customer_idx ON complaints(customer_id);

-- bảng đích của UPDATE
CREATE TABLE customer_stats (
  customer_id int PRIMARY KEY,
  open_pairs  bigint,
  open_amount numeric,
  updated_at  timestamp
);
INSERT INTO customer_stats SELECT id, 0, 0, now() FROM customers;

-- như môi trường production: vacuum + analyze trước khi upgrade
VACUUM (ANALYZE);
