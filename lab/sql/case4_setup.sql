-- CASE 4 (DB "lab4"): mô phỏng mẫu  WITH a AS NOT MATERIALIZED (...), current_brand_code AS (join nhiều bảng)
--                     UPDATE ... WHERE ... NOT IN (SELECT ... FROM current_brand_code WHERE ...)
\set ON_ERROR_STOP on
CREATE TABLE stores (id int PRIMARY KEY, region text NOT NULL, active boolean NOT NULL);
-- 20k cửa hàng, 40% ở VN, 95% active
INSERT INTO stores SELECT i, CASE WHEN i % 5 < 2 THEN 'VN' WHEN i % 5 = 2 THEN 'TH' ELSE 'ID' END, i % 20 <> 0
FROM generate_series(1, 20000) i;

CREATE TABLE brands (id int PRIMARY KEY, code text NOT NULL);
INSERT INTO brands SELECT i, 'BR-' || i FROM generate_series(1, 5000) i;

-- 2M dòng gán brand cho store; 50% là bản ghi hiện hành (is_current)
CREATE TABLE brand_assignments (
  id bigint PRIMARY KEY, store_id int NOT NULL, brand_id int NOT NULL, is_current boolean NOT NULL, valid_from date NOT NULL
);
INSERT INTO brand_assignments
SELECT i, 1 + ((i::bigint * 7919) % 20000), 1 + ((i::bigint * 104729) % 5000), i % 2 = 0,
       date '2020-01-01' + (i % 2000)
FROM generate_series(1, 2000000) i;
CREATE INDEX ON brand_assignments(store_id);
CREATE INDEX ON brand_assignments(brand_id);

-- bảng đích: 200k sản phẩm; 5k đang PENDING, một nửa trong đó mang mã brand legacy (không còn hiệu lực)
CREATE TABLE products (id int PRIMARY KEY, brand_code text NOT NULL, status text NOT NULL, updated_at timestamp);
INSERT INTO products
SELECT i,
       CASE WHEN i % 40 = 0 AND i % 80 = 0 THEN 'LEGACY-' || i ELSE 'BR-' || (1 + (i % 5000)) END,
       CASE WHEN i % 40 = 0 THEN 'PENDING' ELSE 'ACTIVE' END,
       now()
FROM generate_series(1, 200000) i;
CREATE INDEX ON products(status);
VACUUM ANALYZE;
