-- Đối tượng để kiểm tra các rule HEALTH-* (chạy sau fx_setup.sql)
\set ON_ERROR_STOP on
CREATE TABLE status_ranks(status text PRIMARY KEY, rank int);
INSERT INTO status_ranks VALUES ('OPEN', 1), ('CLOSED', 2);
-- Anti-pattern phổ biến: hàm "IMMUTABLE" đọc bảng, tham chiếu không ghi schema, dùng trong expression index.
-- Tạo kèm search_path để CREATE INDEX chạy được trên PG17, rồi RESET để mô phỏng đối tượng được pg_upgrade mang sang từ PG13.
CREATE FUNCTION status_rank(s text) RETURNS int LANGUAGE sql IMMUTABLE
  SET search_path = public AS $$ SELECT rank FROM status_ranks WHERE status = s $$;
CREATE INDEX ord_status_rank_idx ON ord (status_rank(status));
ALTER FUNCTION status_rank(text) RESET search_path;
CREATE STATISTICS ord_dep (dependencies, ndistinct) ON cust, status FROM ord;
CREATE TABLE legacy_log (id int, msg text) WITH (autovacuum_enabled = false);
INSERT INTO legacy_log SELECT i, repeat('m', 80) FROM generate_series(1, 50000) i;
