#!/usr/bin/env bash
# Thí nghiệm "giải pháp KHÔNG sửa code ứng dụng" cho CASE 4 (CTE + UPDATE ... NOT IN) – ghi kết quả vào ../results/remedies/.
#
# Chuẩn bị (một lần, xem README.md cùng thư mục):
#   docker compose up -d --build
#   bash setup.sh
# Dùng:
#   bash run_remedies.sh            # tất cả các bước nhanh (E0..E7, ~5 phút trên máy 2 vCPU)
#   bash run_remedies.sh e2b e3b    # chỉ chạy một số bước
#   bash run_remedies.sh slow       # plan xấu của PG17 chạy tới khi xong (khoảng 10–20 phút)
set -uo pipefail
cd "$(dirname "$0")"
OUT=../results/remedies; mkdir -p "$OUT"

F() { grep -v -e 'cannot use the hint table' -e '^HINT:  Run "CREATE EXTENSION'; }
psql_as() {  # psql_as <container> <user> [appname]: chạy stdin. app_batch đăng nhập qua TCP để mô phỏng ứng dụng
  local c=$1 u=$2 app=${3:-batch-job}
  if [ "$u" = postgres ]; then docker exec -i "$c" psql -U postgres -d lab4 -X 2>&1 | F
  else docker exec -i -e PGPASSWORD=app -e PGAPPNAME="$app" "$c" psql -h localhost -U "$u" -d lab4 -X 2>&1 | F; fi
}
explain() {  # explain <container> <user> <sqlfile> <outfile> [format]
  local c=$1 u=$2 f=$3 o=$4 fmt=${5:-TEXT}
  { echo "BEGIN;"; echo "EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, FORMAT $fmt)"; cat "sql/$f"; echo ";"; echo "ROLLBACK;"; } \
    | psql_as "$c" "$u" | grep -v -e '^BEGIN$' -e '^ROLLBACK$' > "$OUT/$o"
  echo "  -> $OUT/$o: $(grep -Eo 'Execution Time: [0-9.]+ ms|"Execution Time": [0-9.]+' "$OUT/$o" | head -1)" \
       "$(grep -Eo '\((hashed )?SubPlan [0-9]+\)|Anti Join' "$OUT/$o" | head -1)"
}
reset_all() {
  echo "ALTER DATABASE lab4 RESET ALL; ALTER ROLE app_batch RESET ALL; ALTER ROLE app_batch IN DATABASE lab4 RESET ALL;
        DELETE FROM hint_plan.hints;" | psql_as rpg17 postgres >/dev/null
}
query_id() { echo "EXPLAIN (VERBOSE) $(cat sql/q4.sql);" | psql_as rpg17 app_batch | sed -n 's/^ *Query Identifier: //p'; }

e1() {
  echo "[E1] Ngưỡng work_mem để có lại hashed SubPlan (so với công thức)"
  { docker exec rpg17 psql -U postgres -d lab4 -X -v WMS='{4MB,8MB,10381kB,10382kB,11MB,16MB}' -f /rsql/e1_threshold.sql 2>&1 | F
    docker exec rpg13 psql -U postgres -d lab4 -X -v WMS='{64kB,256kB,257kB,258kB,259kB,1MB,4MB}' -f /rsql/e1_threshold.sql 2>&1 | F
  } | grep -v '^$' > "$OUT/E1_threshold.txt"; cat "$OUT/E1_threshold.txt"
}
e0() {
  echo "[E0] Baseline PG13 (text + JSON)"
  reset_all
  explain rpg13 postgres q4.sql E0_pg13_baseline.txt
  explain rpg13 postgres q4.sql E0_pg13_baseline.json JSON
  echo "[E0] PG17: EXPLAIN (không ANALYZE) – thứ có được ngay cả khi câu chạy 2 giờ"
  echo "EXPLAIN (VERBOSE, SETTINGS) $(cat sql/q4.sql);" | psql_as rpg17 postgres > "$OUT/E0_pg17_explain_only.txt"
  echo "EXPLAIN (VERBOSE, SETTINGS, FORMAT JSON) $(cat sql/q4.sql);" | psql_as rpg17 postgres > "$OUT/E0_pg17_explain_only.json"
}
e2() {
  echo "[E2] Thứ tự ưu tiên ROLE / DATABASE / ROLE IN DATABASE / client options"
  { docker exec rpg17 psql -U postgres -d lab4 -X -f /rsql/e2_role_settings.sql 2>&1 | F
    echo "-- session mới của app_batch:"
    echo "SELECT name, setting, source FROM pg_settings WHERE name IN ('work_mem','hash_mem_multiplier');" | psql_as rpg17 app_batch
    echo "-- session mới của postgres (chỉ chịu ALTER DATABASE):"
    echo "SELECT name, setting, source FROM pg_settings WHERE name IN ('work_mem','hash_mem_multiplier');" | psql_as rpg17 postgres
    echo "-- app_batch nhưng client gửi options '-c work_mem=4MB' (PGOPTIONS / JDBC options) -> ghi đè ALTER ROLE:"
    echo "SELECT name, setting, source FROM pg_settings WHERE name IN ('work_mem','hash_mem_multiplier');" \
      | docker exec -i -e PGPASSWORD=app -e PGOPTIONS="-c work_mem=4MB" rpg17 psql -h localhost -U app_batch -d lab4 -X 2>&1 | F
  } > "$OUT/E2_role_settings.txt"; cat "$OUT/E2_role_settings.txt"
  reset_all
}
e2a() {
  echo "[E2a] ALTER ROLE app_batch SET work_mem = '16MB'"
  reset_all
  echo "ALTER ROLE app_batch SET work_mem = '16MB';" | psql_as rpg17 postgres >/dev/null
  explain rpg17 app_batch q4.sql E2a_pg17_role_work_mem16MB.txt
  reset_all
}
e2b() {
  echo "[E2b] ALTER ROLE app_batch IN DATABASE lab4 SET hash_mem_multiplier = 8 (work_mem giữ 4MB)"
  reset_all
  echo "ALTER ROLE app_batch IN DATABASE lab4 SET hash_mem_multiplier = 8;" | psql_as rpg17 postgres >/dev/null
  explain rpg17 app_batch q4.sql E2b_pg17_role_hash_mem_multiplier8.txt
  reset_all
}
e3() {
  echo "[E3] pg_hint_plan hint table: Set(work_mem) chỉ cho đúng câu này (khoá = query_id)"
  reset_all
  local QID; QID=$(query_id)
  echo "INSERT INTO hint_plan.hints(query_id, application_name, hints) VALUES ($QID, '', 'Set(work_mem \"16MB\")');" | psql_as rpg17 postgres >/dev/null
  explain rpg17 app_batch q4.sql E3_pg17_hint_table_set_work_mem.txt
  explain rpg17 app_batch q4.sql E3_pg17_hint_table_set_work_mem.json JSON
  { echo "query_id (literal 'VN') = $QID"
    echo "PREPARE s(text) AS $(sed "s/'VN'/\$1/" sql/q4.sql); EXPLAIN (VERBOSE) EXECUTE s('VN');" | psql_as rpg17 app_batch \
      | sed -n 's/^ *Query Identifier: /query_id (tham số $1, như JDBC\/prepared) = /p'
    echo "SELECT query_id, application_name, hints FROM hint_plan.hints;" | psql_as rpg17 postgres
    echo "-- work_mem lúc THỰC THI vẫn là 4MB (Set() chỉ áp dụng khi lập plan):"
    echo "SELECT current_setting('work_mem') AS work_mem_at_execution;" | psql_as rpg17 app_batch
  } > "$OUT/E3_query_id.txt"; cat "$OUT/E3_query_id.txt"
  reset_all
}
e3b() {
  echo "[E3b] Hint chỉ áp dụng cho application_name = 'batch-job'"
  reset_all
  local QID; QID=$(query_id)
  echo "INSERT INTO hint_plan.hints(query_id, application_name, hints) VALUES ($QID, 'batch-job', 'Set(hash_mem_multiplier 8)');" | psql_as rpg17 postgres >/dev/null
  { echo "-- session application_name=batch-job:"
    echo "EXPLAIN $(cat sql/q4.sql);" | psql_as rpg17 app_batch batch-job | grep -E 'Filter: \(NOT'
    echo "-- cùng role, application_name=web-api:"
    echo "EXPLAIN $(cat sql/q4.sql);" | psql_as rpg17 app_batch web-api | grep -E 'Filter: \(NOT'
  } > "$OUT/E3b_hint_by_application_name.txt"; cat "$OUT/E3b_hint_by_application_name.txt"
  reset_all
}
e4() {
  echo "[E4] Đánh index có cứu được NOT IN không?"
  echo "BEGIN;
    CREATE INDEX ON products(brand_code);
    CREATE INDEX ON products(status, brand_code);
    CREATE INDEX ON brand_assignments(store_id, brand_id) WHERE is_current;
    CREATE INDEX ON stores(region) WHERE active;
    CREATE INDEX ON brands(code);
    ANALYZE products, brand_assignments, stores, brands;
    EXPLAIN $(cat sql/q4.sql);
    ROLLBACK;" | psql_as rpg17 postgres > "$OUT/E4_pg17_indexes_explain.txt"
  grep -E 'Filter: \(NOT|CTE Scan' "$OUT/E4_pg17_indexes_explain.txt"
}
e5() {
  echo "[E5] Thủ thuật thống kê: SET STATISTICS 0 có xoá stats cũ không?"
  echo "BEGIN;
    SELECT 'truoc' AS buoc, attname, n_distinct, most_common_vals FROM pg_stats WHERE tablename='stores' AND attname='region';
    ALTER TABLE stores ALTER COLUMN region SET STATISTICS 0;
    ANALYZE stores;
    SELECT 'sau SET STATISTICS 0 + ANALYZE' AS buoc, attname, n_distinct, most_common_vals FROM pg_stats WHERE tablename='stores' AND attname='region';
    EXPLAIN $(cat sql/q4.sql);
    ROLLBACK;" | psql_as rpg17 postgres > "$OUT/E5_pg17_set_statistics_0.txt"
  grep -E 'truoc|sau|CTE Scan|Filter: \(NOT' "$OUT/E5_pg17_set_statistics_0.txt"
}
e6() {
  echo "[E6] Ngữ nghĩa NULL của NOT IN vs NOT EXISTS"
  docker exec rpg17 psql -U postgres -d lab4 -X -f /rsql/e6_null_semantics.sql 2>&1 | F > "$OUT/E6_null_semantics.txt"
  cat "$OUT/E6_null_semantics.txt"
}
e7() {
  echo "[E7] Viết lại NOT EXISTS (cần build lại) – để so sánh"
  for c in rpg13 rpg17; do
    explain $c postgres q4_notexists.sql "E7_${c#r}_notexists.txt"
    explain $c postgres q4_notexists_nullsafe.sql "E7_${c#r}_notexists_nullsafe.txt"
  done
  { echo "-- số dòng bị UPDATE (RETURNING trong transaction rồi ROLLBACK; chạy trên PG13 với work_mem 32MB cho nhanh)"
    for f in q4.sql q4_notexists.sql q4_notexists_nullsafe.sql; do
      echo "BEGIN; SET LOCAL work_mem='32MB'; WITH u AS ($(cat sql/$f) RETURNING 1) SELECT '$f' AS variant, count(*) FROM u; ROLLBACK;" \
        | psql_as rpg13 postgres | grep -E '\.sql'
    done; } > "$OUT/E7_rowcount_check.txt"; cat "$OUT/E7_rowcount_check.txt"
}
e8() {
  echo "[E8] query_id của câu ĐANG chạy (pg_stat_activity) = khoá hint table?"
  reset_all
  { echo "BEGIN; SET LOCAL statement_timeout = '20s';"; cat sql/q4.sql; echo ";"; echo "ROLLBACK;"; } \
    | psql_as rpg17 app_batch > /dev/null &
  local bg=$! i
  for i in $(seq 1 20); do
    sleep 1
    local row; row=$(echo "SELECT query_id, round(extract(epoch FROM now() - query_start)) AS s, application_name
      FROM pg_stat_activity WHERE usename = 'app_batch' AND state = 'active' AND query LIKE '%current_brand_code%';" \
      | docker exec -i rpg17 psql -U postgres -d lab4 -X -At 2>/dev/null | F)
    [ -n "$row" ] && break
  done
  { echo "EXPLAIN (VERBOSE) Query Identifier : $(query_id)"
    echo "pg_stat_activity (query_id|giây|application_name): $row"; } > "$OUT/E8_running_query_id.txt"
  cat "$OUT/E8_running_query_id.txt"
  wait $bg
}
e9() {
  echo "[E9] CASE 3 (Nested Loop quét lại CTE): so sánh 3 dạng hint trong hint table"
  local P="docker exec -i rpg17 psql -U postgres -X"
  if ! echo "SELECT 1 FROM pg_database WHERE datname='lab'" | $P -At 2>/dev/null | grep -q 1; then
    echo "CREATE DATABASE lab;" | $P >/dev/null 2>&1
    docker exec rpg17 psql -U postgres -d lab -q -f /sql/case3_setup.sql 2>&1 | F
    echo "SET pg_hint_plan.enable_hint_table = off; CREATE EXTENSION IF NOT EXISTS pg_hint_plan;" | $P -d lab 2>&1 | F >/dev/null
  fi
  local Q; Q=$(grep -v '^--' /dev/stdin < ../sql/case3_update.sql)
  local QID; QID=$(echo "EXPLAIN (VERBOSE) $Q;" | $P -d lab 2>&1 | F | sed -n 's/^ *Query Identifier: //p')
  echo "DELETE FROM hint_plan.hints;" | $P -d lab 2>&1 | F >/dev/null
  { echo "BEGIN;"; echo "EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS)"; echo "$Q;"; echo "ROLLBACK;"; } | $P -d lab 2>&1 | F \
    | grep -v -e '^BEGIN$' -e '^ROLLBACK$' > "$OUT/E9_case3_pg17_baseline.txt"
  local n=0 H
  for H in 'HashJoin(cu a b)' 'NoNestLoop(a b) NoNestLoop(b cu) NoNestLoop(a b cu)' 'Set(enable_nestloop off)'; do
    n=$((n+1))
    echo "DELETE FROM hint_plan.hints; INSERT INTO hint_plan.hints(query_id, application_name, hints) VALUES ($QID, '', '$H');" \
      | $P -d lab 2>&1 | F >/dev/null
    { echo "-- hint table: $H"; echo "BEGIN;"; echo "EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS)"; echo "$Q;"; echo "ROLLBACK;"; } \
      | $P -d lab 2>&1 | F | grep -v -e '^BEGIN$' -e '^ROLLBACK$' > "$OUT/E9_case3_pg17_hint$n.txt"
  done
  echo "DELETE FROM hint_plan.hints;" | $P -d lab 2>&1 | F >/dev/null
  for f in E9_case3_pg17_baseline.txt E9_case3_pg17_hint1.txt E9_case3_pg17_hint2.txt E9_case3_pg17_hint3.txt; do
    echo "  -> $OUT/$f: $(head -1 "$OUT/$f" | grep -- '-- hint') $(grep -Eo 'Execution Time: [0-9.]+ ms' "$OUT/$f")" \
         "CTE Scan b $(grep -E 'CTE Scan on open_orders b' "$OUT/$f" | grep -Eo 'loops=[0-9]+' | tail -1)"
  done
}
slow() {
  echo "[E0-slow] PG17 EXPLAIN ANALYZE plan xấu (chạy tới khi xong)"
  reset_all
  explain rpg17 postgres q4.sql E0_pg17_baseline.json JSON
}

steps=("$@"); [ ${#steps[@]} -eq 0 ] && steps=(e1 e0 e2 e2a e2b e3 e3b e4 e5 e6 e7 e8 e9)
for s in "${steps[@]}"; do "$s"; done
