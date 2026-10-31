-- E2: đặt tham số theo ROLE / DATABASE / ROLE IN DATABASE – không sửa code ứng dụng.
-- Ứng dụng đăng nhập bằng role app_batch. Setting chỉ áp dụng cho SESSION MỚI.
\set QUIET on
DROP ROLE IF EXISTS app_batch;
CREATE ROLE app_batch LOGIN PASSWORD 'app';
GRANT SELECT, UPDATE ON ALL TABLES IN SCHEMA public TO app_batch;
GRANT USAGE ON SCHEMA hint_plan TO app_batch;
GRANT SELECT ON hint_plan.hints TO app_batch;
\set QUIET off
-- (1) chỉ role: ảnh hưởng mọi database mà role này kết nối
ALTER ROLE app_batch SET work_mem = '16MB';
-- (2) database: ảnh hưởng mọi role trong lab4 (bị (1) và (3) ghi đè)
ALTER DATABASE lab4 SET work_mem = '8MB';
-- (3) role trong database cụ thể: hẹp nhất, ưu tiên cao nhất trong 3 loại
ALTER ROLE app_batch IN DATABASE lab4 SET hash_mem_multiplier = 4;
SELECT coalesce(r.rolname, '(mọi role)') AS role, coalesce(d.datname, '(mọi db)') AS db, s.setconfig
FROM pg_db_role_setting s LEFT JOIN pg_roles r ON r.oid = s.setrole LEFT JOIN pg_database d ON d.oid = s.setdatabase;
