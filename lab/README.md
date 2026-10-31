# Lab tái hiện regression PostgreSQL 13 → 17 (mô phỏng Cloud SQL in-place upgrade)

> Mọi lệnh dưới đây chạy **từ thư mục `lab/`**. `fixtures/` chứa script tạo dữ liệu mẫu cho `tests/` (test của skill pgdoctor); `results/` là plan nguyên văn đã chụp và report pgdoctor của 4 case.

Lab này dựng **PG13 thật** → chạy **`pg_upgrade` thật** (giống Cloud SQL in-place upgrade) → **PG17** trên cùng dữ liệu, cùng cấu hình,
để so sánh plan và thời gian chạy của các câu `UPDATE` có CTE / MATERIALIZED / join nhiều bảng / `NOT IN`.

Phân tích chi tiết và kết quả đo nằm trong [`pg13-vs-pg17-query-regression.md`](../docs/pg13-vs-pg17-query-regression.md) (Mục 7).
Giải thích EXPLAIN từng dòng cho từng case: [`explain-walkthrough.md`](../docs/explain-walkthrough.md). Plan nguyên văn: `results/`.

## 1. Cấu trúc

```
docker-compose.yml          pg13 (5413), upgrade (tianon/postgres-upgrade:13-to-17), pg17 (5417); cùng tham số -c
init/01_schema_data.sql     dữ liệu DB "shop" (nạp vào pg13 khi khởi tạo lần đầu)
scripts/repro.sh            dựng lại toàn bộ: pg13 -> stop -> pg_upgrade -> pg17 (stats rỗng, giống ngay sau upgrade)
sql/case1_update.sql        CASE 1 (DB shop) – CTE + join, so sánh ước lượng (không phải regression)
sql/case2_update_nostats.sql CASE 2 (DB shop) – PG17 ngay sau pg_upgrade, CHƯA ANALYZE
sql/case3_setup.sql         CASE 3 (DB lab)  – dữ liệu tương quan
sql/case3_update.sql        CASE 3 – CTE dùng 2 lần + điều kiện lọc trên cột CTE   (regression SAU ANALYZE)
sql/case4_setup.sql         CASE 4 (DB lab4) – stores / brands / brand_assignments / products
sql/case4_update.sql        CASE 4 – ★ MẪU CÂU CỦA DỰ ÁN ★: CTE a NOT MATERIALIZED + current_brand_code + UPDATE … NOT IN (…)
```

Tham số giống nhau ở cả hai bản: `shared_buffers=1GB, work_mem=64MB, random_page_cost=1.1, effective_cache_size=4GB, max_parallel_workers_per_gather=2`
(CASE 4 tự `SET work_mem='4MB'`, là mặc định của PostgreSQL / nhiều tier Cloud SQL). Riêng `hash_mem_multiplier` để mặc định của từng phiên bản (PG13 = 1, PG17 = 2), giống thực tế.

## 2. Chạy

Yêu cầu: Docker Desktop (~3 GB đĩa), Git Bash/WSL để chạy `.sh`.

```bash
# 1) Dựng lab: pg13 nạp dữ liệu -> pg_upgrade -> pg17 (CHƯA có stats)   ~3 phút
bash scripts/repro.sh

# Hàm tiện ích: chạy 1 file SQL dưới dạng EXPLAIN ANALYZE trong transaction rồi ROLLBACK
run() { c=$1; db=$2; f=$3; (echo "SET statement_timeout='15min'; BEGIN;"; sed 's/^WITH/EXPLAIN (ANALYZE, BUFFERS) WITH/' $f; echo "ROLLBACK;") \
        | docker exec -i $c psql -U postgres -d $db -X; }

# 2) CASE 2 – PG17 ngay sau upgrade (chưa ANALYZE)
run pg13 shop sql/case2_update_nostats.sql
run pg17 shop sql/case2_update_nostats.sql          # chậm, bị timeout 15 phút
docker exec pg17 psql -U postgres -d shop -c "VACUUM" -c "REINDEX DATABASE shop" -c "ANALYZE"
run pg17 shop sql/case2_update_nostats.sql          # nhanh trở lại

# 3) CASE 3 & 4 – regression khi ĐÃ có stats ở cả hai bên
for db in lab lab4; do for c in pg13 pg17; do docker exec $c psql -U postgres -c "DROP DATABASE IF EXISTS $db" -c "CREATE DATABASE $db"; done; done
docker exec -i pg13 psql -U postgres -d lab  < sql/case3_setup.sql
docker exec -i pg13 psql -U postgres -d lab4 < sql/case4_setup.sql
for db in lab lab4; do docker exec pg13 pg_dump -U postgres -d $db | docker exec -i pg17 psql -U postgres -d $db -q; \
                       docker exec pg17 psql -U postgres -d $db -c "VACUUM ANALYZE"; done
run pg13 lab  sql/case3_update.sql ; run pg17 lab  sql/case3_update.sql
run pg13 lab4 sql/case4_update.sql ; run pg17 lab4 sql/case4_update.sql
```

Kết nối bằng client: `psql -h localhost -p 5413 -U postgres shop` (PG13), `-p 5417` (PG17), mật khẩu `postgres`.

Dừng lab: `docker compose --profile pg17 --profile upgrade down` (thêm `-v` để xóa luôn dữ liệu).

## 3. Đọc kết quả

So sánh hai plan theo thứ tự:
1. **`rows=` (ước lượng) vs `actual rows=`** ở từng node: node đầu tiên lệch ≥ 10–100× là nơi plan bắt đầu đi sai.
2. **Kiểu join / subplan**: `Hash Join`/`Merge Join` ↔ `Nested Loop`; `hashed SubPlan` ↔ `SubPlan` (thiếu chữ `hashed` nghĩa là quét tuyến tính).
3. **`loops=`** và **`Rows Removed by Join Filter`** cực lớn ở nhánh inner: dấu hiệu O(N×M).
4. Thử lại với một trong các cách sửa (Mục 7 của tài liệu) để chứng minh nguyên nhân.

## 4. Dùng dữ liệu thật từ Cloud SQL

> ⚠️ Dữ liệu production: tuân thủ quy định bảo mật nội bộ. Ưu tiên **schema + dữ liệu đã mask/lấy mẫu**. Không commit dump vào git.
> Các lệnh dưới đây dùng thông tin kết nối **của bạn** (`<INSTANCE_CONNECTION_NAME>`, `<DB>`, `<USER>`); lab này không kết nối tới Cloud SQL nào.

### Cách A – dump/restore vào lab (nhẹ, linh hoạt)
```bash
# 1) Kết nối tới Cloud SQL PG13 (trước upgrade) hoặc bản clone, qua Cloud SQL Auth Proxy
cloud-sql-proxy <INSTANCE_CONNECTION_NAME> --port 6543 &
#    (hoặc dùng private IP từ máy trong cùng VPC)

# 2a) Chỉ schema (luôn nên có)
pg_dump -h 127.0.0.1 -p 6543 -U <USER> -d <DB> --schema-only -Fc -f schema.dump
# 2b) Dữ liệu của các bảng có trong câu query chậm (toàn bộ hoặc lấy mẫu)
pg_dump -h 127.0.0.1 -p 6543 -U <USER> -d <DB> --data-only -Fc -t 'schema.bang1' -t 'schema.bang2' -f data.dump
#     Lấy mẫu / mask: tạo bảng mẫu trên một bản clone rồi dump, ví dụ
#     CREATE TABLE s.bang1_sample AS SELECT * FROM s.bang1 TABLESAMPLE SYSTEM (10);
#     UPDATE ... SET email = md5(email), phone = NULL ...;   -- mask cột nhạy cảm
#     Lưu ý: lấy mẫu làm thay đổi phân bố/tương quan -> có thể KHÔNG tái hiện được plan xấu. Ưu tiên giữ đủ dữ liệu cho các cột dùng trong JOIN/WHERE.

# 3) Nạp vào pg13 của lab TRƯỚC bước pg_upgrade
docker compose --profile pg17 --profile upgrade down -v
docker compose up -d pg13          # (đổi init/ thành rỗng nếu không muốn dữ liệu mẫu)
docker exec pg13 psql -U postgres -c "CREATE DATABASE app"
docker exec -i pg13 pg_restore -U postgres -d app --no-owner < schema.dump
docker exec -i pg13 pg_restore -U postgres -d app --no-owner --data-only < data.dump
docker exec pg13 psql -U postgres -d app -c "VACUUM ANALYZE"      # giống trạng thái production PG13

# 4) Upgrade như Cloud SQL, rồi làm như dự án đã làm
docker compose stop pg13
docker compose --profile upgrade run --rm upgrade
docker compose --profile pg17 up -d pg17 pg13
docker exec pg17 psql -U postgres -d app -c "VACUUM" -c "REINDEX DATABASE app" -c "ANALYZE"

# 5) So sánh
#    BEGIN; EXPLAIN (ANALYZE, BUFFERS, SETTINGS) <câu UPDATE>; ROLLBACK;   trên cả pg13 và pg17
```
Nhớ áp các database flag của Cloud SQL (`work_mem`, `hash_mem_multiplier`, `random_page_cost`, `jit`, …) bằng `-c` trong `docker-compose.yml`, nếu không thì so sánh sẽ lệch.
Cũng cần cài cùng extension như production (lab dùng image `postgres` chuẩn, có sẵn các contrib extension).

### Cách B – clone Cloud SQL (trung thực nhất)
1. Tạo **clone** của instance PG13 (hoặc restore backup trước upgrade sang instance mới).
2. Trên clone: chạy câu query với `BEGIN; EXPLAIN (ANALYZE, BUFFERS, SETTINGS) …; ROLLBACK;` để lấy plan PG13.
3. Thực hiện in-place upgrade clone lên PG17 → `VACUUM`, `REINDEX`, `ANALYZE` → chạy lại cùng lệnh EXPLAIN.
4. So sánh hai plan theo Mục 3 ở trên. Cách này giữ đúng phần cứng, flag, extension và dữ liệu.
