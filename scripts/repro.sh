#!/usr/bin/env bash
# Dựng lại toàn bộ lab: pg13 (có stats) -> pg_upgrade -> pg17 (stats rỗng). Chạy bằng Git Bash/WSL ở thư mục gốc dự án.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose --profile pg17 --profile upgrade down -v
docker compose up -d pg13
echo "[1/4] Đợi pg13 nạp dữ liệu (init/*.sql)..."
until docker logs pg13 2>&1 | grep -q "init process complete"; do sleep 5; done
until docker exec pg13 pg_isready -U postgres -q 2>/dev/null && docker logs pg13 2>&1 | tail -1 | grep -q "ready to accept"; do sleep 2; done
echo "[2/4] Dừng pg13 (clean shutdown) và chạy pg_upgrade 13 -> 17"
docker compose stop pg13
docker compose --profile upgrade run --rm upgrade
echo "[3/4] Khởi động pg17 (trên volume đã upgrade) và pg13"
docker compose --profile pg17 up -d pg17 pg13
sleep 8
echo "[4/4] Trạng thái stats:"
docker exec pg17 psql -U postgres -d shop -c "select relname, reltuples from pg_class where relnamespace='public'::regnamespace and relkind='r'" -c "select count(*) as pg_stats_rows from pg_stats where schemaname='public'"
echo "Xong. pg13: localhost:5413  | pg17: localhost:5417 (user postgres / pass postgres / db shop)"
