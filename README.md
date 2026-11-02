# Upgrade PostgreSQL 13 → 17 (Cloud SQL in-place upgrade) – điều tra regression

Bối cảnh: cùng một câu `UPDATE` (CTE, MATERIALIZED, join nhiều bảng…) chạy khoảng 2 phút trên PG13 nhưng khoảng 2 giờ trên PG17, kể cả sau khi đã `VACUUM`, `REINDEX`, `ANALYZE` (xem `context.md`).

## Cấu trúc

```
docs/
  solutions.md                       ★ Xử lý incident: giải pháp KHÔNG sửa code (hint table, ALTER ROLE, flag) + sửa code; đã đo trong lab
  pg13-vs-pg17-query-regression.md   Nghiên cứu: khác biệt PG13 → PG17, nguyên nhân, cách chẩn đoán, kết quả lab (Mục 7)
  explain-walkthrough.md             Giải thích EXPLAIN từng dòng của từng case, tái tạo chính xác các con số ước lượng của planner
  pgdoctor.md                        Tài liệu đầy đủ của tool pgdoctor: các rule, cách thu thập dữ liệu, cách đọc report
  planviz.md                         Tool trực quan hoá EXPLAIN: sơ đồ plan, ước lượng ↔ thực tế (×N), sai số sinh ra ở đâu, so sánh plan

.claude/skills/pgdoctor/             Skill (tối giản, tiếng Anh): SKILL.md + tool Python + collector SQL
  SKILL.md  pgdoctor_cli.py  pgdoctor/  collect/pgdoctor_collect.sql
.claude/skills/planviz/              Skill planviz: phân tích + trang HTML tự chứa (chạy offline, dán/kéo-thả plan vào được)
  SKILL.md  planviz_cli.py  planviz/  planviz/assets/{core.js,ui.js,style.css,rules.json,demo/}

lab/                                 Lab docker: PG13 → pg_upgrade thật → PG17
  docker-compose.yml  init/  scripts/repro.sh
  sql/case1..4*.sql                  Các case tái hiện
  remedies/                          Lab kiểm chứng giải pháp không sửa code (PG13/PG17 + pg_hint_plan), script E0..E8
  results/                           Plan nguyên văn đã chụp + report pgdoctor của 4 case; results/remedies/ = kết quả E0..E9;
                                     results/run/ = output của `pgdoctor run` trên lab (cấu hình hiệu lực PG13 vs PG17)
                                     results/planviz/ = trang planviz của case4 (PG13, PG17, PG17 + hint, PG17 NOT EXISTS)
  fixtures/                          Script tạo dữ liệu mẫu cho tests/
  README.md                          Cách chạy lab, cách dump dữ liệu thật từ Cloud SQL

tests/                               Test hồi quy của pgdoctor và planviz
  test_pgdoctor.py  test_remedy.py  test_run.py  fake_psql.py
  test_planviz.py  planviz_node.js  fake_psql_plan.py      (so khớp Python ↔ JavaScript cần node)
  samples/                           Plan thật chụp từ lab, snapshot collector, output 'pgdoctor capture' (samples/run/), dữ liệu giả lập
```

## Bắt đầu nhanh

```bash
# Phân tích plan của câu thật (PG13 vs PG17)
python .claude/skills/pgdoctor/pgdoctor_cli.py analyze --old-plan q.pg13.json --new-plan q.pg17.json -o report.html

# Chạy câu lệnh trên CẢ HAI server (qua psql) và liệt kê khác biệt cấu hình hiệu lực + plan + thống kê của chính câu đó
python .claude/skills/pgdoctor/pgdoctor_cli.py run --old-dsn "<pg13>" --new-dsn "<pg17>" --query-file q.sql \
       --app-name <application_name> --analyze old --out-dir run1        # -> run1/report.html, run1/remedies.sql

# Sinh runbook khắc phục (ưu tiên không sửa code) cho câu chậm trên PG17 – EXPLAIN là đủ, không cần chờ 2 giờ
python .claude/skills/pgdoctor/pgdoctor_cli.py remedy --plan q.pg17.json --app-role <role> --app-db <db> -o fix.sql

# Trực quan hoá plan: sơ đồ, sai số ước lượng ×N và nơi nó sinh ra, thời gian, so sánh nhiều plan (HTML offline)
python .claude/skills/planviz/planviz_cli.py report q.pg13.json q.pg17.json -o planviz.html
python .claude/skills/planviz/planviz_cli.py app -o planviz.html        # trang trống để dán / kéo-thả plan

# Chạy test của tool
python -m unittest discover -s tests -v

# Dựng lab (khoảng 3 phút)
cd lab && bash scripts/repro.sh
```
