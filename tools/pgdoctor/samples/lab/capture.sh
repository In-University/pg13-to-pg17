#!/usr/bin/env bash
# Tạo lại plan mẫu từ lab docker (pg13 / pg17). Chạy từ thư mục samples/lab.
set -euo pipefail
OUT=../plans; OUTT=../plans_text; DB=pgdoctor_fx
psqlc(){ docker exec -i "$1" psql -U postgres -d "$DB" -X -q -At "${@:2}"; }
for c in pg13 pg17; do
  docker exec "$c" psql -U postgres -X -q -c "DROP DATABASE IF EXISTS $DB" -c "CREATE DATABASE $DB"
  psqlc "$c" < fx_setup.sql
  [ "$c" = pg13 ] && psqlc pg13 -c "ANALYZE ev, acct_stats"
done
explain(){ # container sqlfile outfile [format]
  local fmt=${4:-JSON}
  { echo "BEGIN;"; echo "EXPLAIN (ANALYZE, BUFFERS, SETTINGS, FORMAT $fmt)"; cat "$2"; echo ";"; echo "ROLLBACK;"; } | psqlc "$1" > "$3"
}
for c in pg13 pg17; do
  v=${c#pg}
  explain $c qA_cte_join_update.sql $OUT/qA_cte_join_update.$c.json
  explain $c qB_bug18466.sql        $OUT/qB_bug18466.$c.json
  explain $c qC_cte_pathkeys.sql    $OUT/qC_cte_pathkeys.$c.json
  explain $c qD_cte_skew.sql        $OUT/qD_cte_skew.$c.json
  explain $c qD_cte_skew.sql        $OUTT/qD_cte_skew.$c.txt TEXT
  explain $c qA_cte_join_update.sql $OUTT/qA_cte_join_update.$c.txt TEXT
done
# PG17 sau khi ANALYZE (trạng thái dự án đã làm)
psqlc pg17 -c "ANALYZE ev, acct_stats"
cp $OUT/qA_cte_join_update.pg13.json $OUT/qA_after_analyze.pg13.json
explain pg17 qA_cte_join_update.sql $OUT/qA_after_analyze.pg17.json
# Mẫu qE (NOT IN / hashed SubPlan) – từ database lab4 của lab (sql/case4_setup.sql), plan PG17 chỉ EXPLAIN
if docker exec pg13 psql -U postgres -X -At -c "select 1 from pg_database where datname='lab4'" | grep -q 1; then
  exE(){ { echo "BEGIN; SET LOCAL work_mem = '4MB';"; echo "EXPLAIN ($2)"; cat qE_notin_subplan.sql; echo "ROLLBACK;"; }          | docker exec -i "$1" psql -U postgres -d lab4 -X -q -At > "$3"; }
  exE pg13 "ANALYZE, BUFFERS, SETTINGS, FORMAT JSON" $OUT/qE_notin_subplan.pg13.json
  exE pg17 "SETTINGS, FORMAT JSON"                   $OUT/qE_notin_subplan.pg17.json
  exE pg13 "ANALYZE, BUFFERS, SETTINGS"              $OUTT/qE_notin_subplan.pg13.txt
  exE pg17 "SETTINGS"                                $OUTT/qE_notin_subplan.pg17.txt
fi
# Đối tượng cho rule HEALTH-*, rồi chạy collector trên cả hai bản
for c in pg13 pg17; do psqlc "$c" < fx_health_extra.sql; done
psqlc pg13 -c "ANALYZE"
psqlc pg17 -c "ANALYZE ord" || echo "(dự kiến) ANALYZE ord lỗi trên PG17 vì search_path an toàn"
export MSYS_NO_PATHCONV=1
for c in pg13 pg17; do
  mkdir -p ../collected/$c
  docker exec $c sh -c "rm -rf /tmp/pgdoctor && mkdir -p /tmp/pgdoctor && chmod 777 /tmp/pgdoctor"
  docker cp ../../collect/pgdoctor_collect.sql $c:/tmp/pgdoctor/collect.sql >/dev/null
  docker exec -u postgres $c sh -c "cd /tmp/pgdoctor && psql -d $DB -X -q -v ON_ERROR_STOP=1 -f collect.sql"
  for f in settings.json health.json; do docker cp $c:/tmp/pgdoctor/$f ../collected/$c/$f >/dev/null; done
done
echo done
