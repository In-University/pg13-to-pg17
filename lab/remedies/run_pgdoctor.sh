#!/usr/bin/env bash
# Chạy 'pgdoctor run' trên lab (rpg13, rpg17) bằng role ứng dụng app_batch, qua docker exec (máy host không cần psql).
#   bash run_pgdoctor.sh                 # EXPLAIN hai bên + EXPLAIN ANALYZE bên PG13 → ../results/run/
#   bash run_pgdoctor.sh role-hm8        # thêm ALTER ROLE … SET hash_mem_multiplier = 8 trên PG17 rồi chạy lại, sau đó RESET
set -euo pipefail
cd "$(dirname "$0")"
CLI=../../.claude/skills/pgdoctor/pgdoctor_cli.py
OUT=../results/run
args=(--old-psql "docker exec -i -e PGPASSWORD=app rpg13 psql" --new-psql "docker exec -i -e PGPASSWORD=app rpg17 psql"
      --old-dsn "host=127.0.0.1 dbname=lab4 user=app_batch" --new-dsn "host=127.0.0.1 dbname=lab4 user=app_batch"
      --query-file sql/q4.sql --app-name batch-job --analyze old)
if [[ "${1:-}" == "role-hm8" ]]; then
  docker exec rpg17 psql -U postgres -d lab4 -qc "ALTER ROLE app_batch IN DATABASE lab4 SET hash_mem_multiplier = 8"
  trap 'docker exec rpg17 psql -U postgres -d lab4 -qc "ALTER ROLE app_batch IN DATABASE lab4 RESET hash_mem_multiplier"' EXIT
  python3 "$CLI" run "${args[@]}" --analyze both --name case4_role_hm8 --out-dir "$OUT/role_hm8"
else
  python3 "$CLI" run "${args[@]}" --name case4_update --out-dir "$OUT/live"
fi
