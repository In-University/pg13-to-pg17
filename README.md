# Upgrade PostgreSQL 13 → 17 (Cloud SQL in-place upgrade) – điều tra regression

Bối cảnh: cùng một câu `UPDATE` (CTE, MATERIALIZED, join nhiều bảng…) chạy khoảng 2 phút trên PG13 nhưng khoảng 2 giờ trên PG17, kể cả sau khi đã `VACUUM`, `REINDEX`, `ANALYZE` (xem `context.md`).

## Cấu trúc

```
docs/
  solutions.md                       ★ Xử lý incident: giải pháp KHÔNG sửa code (hint table, ALTER ROLE, flag) + sửa code; đã đo trong lab
  pg13-vs-pg17-query-regression.md   Nghiên cứu: khác biệt PG13 → PG17, nguyên nhân, cách chẩn đoán, kết quả lab (Mục 7)
  explain-walkthrough.md             Giải thích EXPLAIN từng dòng của từng case, tái tạo chính xác các con số ước lượng của planner
  pgdoctor.md                        Tài liệu đầy đủ của tool pgdoctor: các rule, cách thu thập dữ liệu, cách đọc report

.claude/skills/pgdoctor/             Skill (tối giản, tiếng Anh): SKILL.md + tool Python + collector SQL
  SKILL.md  pgdoctor_cli.py  pgdoctor/  collect/pgdoctor_collect.sql

lab/                                 Lab docker: PG13 → pg_upgrade thật → PG17
  docker-compose.yml  init/  scripts/repro.sh
  sql/case1..4*.sql                  Các case tái hiện
  remedies/                          Lab kiểm chứng giải pháp không sửa code (PG13/PG17 + pg_hint_plan), script E0..E8
  results/                           Plan nguyên văn đã chụp + report pgdoctor của 4 case; results/remedies/ = kết quả E0..E8
  fixtures/                          Script tạo dữ liệu mẫu cho tests/
  README.md                          Cách chạy lab, cách dump dữ liệu thật từ Cloud SQL

tests/                               Test hồi quy của pgdoctor
  test_pgdoctor.py  samples/         Plan thật chụp từ lab, snapshot collector, dữ liệu pg_stat_statements giả lập
```

## Bắt đầu nhanh

```bash
# Phân tích plan của câu thật (PG13 vs PG17)
python .claude/skills/pgdoctor/pgdoctor_cli.py analyze --old-plan q.pg13.json --new-plan q.pg17.json -o report.html

# Sinh runbook khắc phục (ưu tiên không sửa code) cho câu chậm trên PG17 – EXPLAIN là đủ, không cần chờ 2 giờ
python .claude/skills/pgdoctor/pgdoctor_cli.py remedy --plan q.pg17.json --app-role <role> --app-db <db> -o fix.sql

# Chạy test của tool
python -m unittest discover -s tests -v

# Dựng lab (khoảng 3 phút)
cd lab && bash scripts/repro.sh
```
