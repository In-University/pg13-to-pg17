#!/usr/bin/env bash
# Nạp dữ liệu CASE 4 vào rpg13 và rpg17 (DB lab4), tạo role ứng dụng app_batch và extension.
# Lưu ý pg_hint_plan 1.7 (PG17): phải CREATE EXTENSION khi pg_hint_plan.enable_hint_table = off,
# nếu không lệnh lỗi "column query_id does not exist" (hook tra hint table trong lúc bảng đang được tạo).
set -euo pipefail
cd "$(dirname "$0")"
for c in rpg13 rpg17; do
  docker exec $c psql -U postgres -qc "DROP DATABASE IF EXISTS lab4" -c "CREATE DATABASE lab4" 2>&1 | grep -v -e 'hint table' -e '^HINT' || true
  docker exec $c psql -U postgres -d lab4 -q -f /sql/case4_setup.sql 2>&1 | grep -v -e 'hint table' -e '^HINT' || true
  docker exec $c psql -U postgres -d lab4 -q -c "SET pg_hint_plan.enable_hint_table = off" \
    -c "CREATE EXTENSION IF NOT EXISTS pg_hint_plan" -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements" \
    -c "DROP ROLE IF EXISTS app_batch" -c "CREATE ROLE app_batch LOGIN PASSWORD 'app'" \
    -c "GRANT SELECT, UPDATE ON ALL TABLES IN SCHEMA public TO app_batch" 2>&1 | grep -v -e 'hint table' -e '^HINT' || true
done
echo "OK: lab4 sẵn sàng trên rpg13, rpg17"
