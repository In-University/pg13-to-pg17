# Lab "giải pháp không sửa code" – CASE 4 (`UPDATE … NOT IN (SELECT … FROM CTE)`)

Lab này kiểm chứng từng phương án khắc phục trong [`docs/solutions.md`](../../docs/solutions.md). Ràng buộc là không được sửa câu SQL của ứng dụng.

Thiết lập lab:
- PG **13.23** và PG **17.11**, cùng image Debian chính thức, cài thêm `pg_hint_plan` (1.3.11 cho PG13, 1.7.2 cho PG17).
- Cùng dữ liệu (`../sql/case4_setup.sql`, 2M dòng) và cùng tham số. Riêng `hash_mem_multiplier` để mặc định của từng bản: PG13 là 1, PG17 là 2.
- `work_mem = 4MB` ở mức instance, giống mặc định của Cloud SQL.
- Ứng dụng được mô phỏng bằng role `app_batch`, đăng nhập qua TCP với `application_name = batch-job`.

Máy đo là 2 vCPU / 2 GB RAM, nhỏ hơn lab gốc (8 vCPU). Vì vậy thời gian tuyệt đối khác lab gốc, nhưng **hệ số** giữa các phương án là điều cần quan sát.

## Chạy

```bash
cd lab/remedies
docker compose up -d --build        # build image postgres:13/17 + pg_hint_plan
bash setup.sh                       # nạp dữ liệu (~1 phút), tạo extension, role app_batch
bash run_remedies.sh                # E0..E9 (~10 phút; E9 tự nạp CASE 3 vào DB lab)
bash run_remedies.sh slow           # plan xấu của PG17 chạy tới khi xong (lâu)
bash run_remedies.sh e3 e3b         # chỉ chạy một vài bước
docker compose down -v              # dọn
```

Kết quả được ghi vào `../results/remedies/`.

## Các thí nghiệm và kết quả

| Bước | Thí nghiệm | Kết quả (PG17 trừ khi ghi khác) | File |
|---|---|---|---|
| E0 | Baseline | PG13 `hashed SubPlan` 4.0–4.2 s. PG17 `SubPlan` (không hash) **1 228 s** (20.5 phút; thân CTE 1.9 s, 83% thời gian là quét lại CTE 5000 lần) | `E0_*` |
| E1 | Ngưỡng `work_mem` | PG17: `10381kB` → SubPlan, `10382kB` → hashed. PG13: `257kB` → SubPlan, `258kB` → hashed. Khớp công thức `rows × (MAXALIGN(width)+24) ≤ work_mem × hash_mem_multiplier` | `E1_threshold.txt` |
| E2 | Thứ tự ưu tiên tham số | `ALTER ROLE … IN DATABASE` > `ALTER ROLE` > `ALTER DATABASE`. `options -c` từ client (PGOPTIONS/JDBC) ghi đè tất cả (`source = client`) | `E2_role_settings.txt` |
| E2a | `ALTER ROLE app_batch SET work_mem = '16MB'` | hashed, 5.6 s | `E2a_*` |
| E2b | `ALTER ROLE app_batch IN DATABASE lab4 SET hash_mem_multiplier = 8` | hashed, 4.2 s. Lần thử với `= 4` (16 MB < 21.3 MB cần) **vẫn không hash**, đúng như công thức dự đoán | `E2b_*` |
| E3 | Hint table `Set(work_mem "16MB")` theo `query_id` | hashed, 4.1 s. Lúc thực thi `work_mem` vẫn là 4MB. Literal và `$1` cho **query_id khác nhau** | `E3_*` |
| E3b | Hint chỉ cho `application_name = 'batch-job'` | `batch-job` nhận hashed, `web-api` (cùng role) vẫn nhận SubPlan | `E3b_*` |
| E4 | Thêm 5 index | Plan **không đổi** (vẫn SubPlan) | `E4_*` |
| E5 | `SET STATISTICS 0` + `ANALYZE` | Stats cũ **vẫn còn**, plan không đổi | `E5_*` |
| E6 | Ngữ nghĩa NULL | Bảng chân trị: NOT IN, NOT EXISTS, dạng null-safe | `E6_*` |
| E7 | Viết lại `NOT EXISTS` (cần build lại) | PG17 Hash Anti Join 4.4 s, null-safe 4.2 s. Số dòng UPDATE giống hệt (2500) | `E7_*` |
| E8 | `pg_stat_activity.query_id` của câu UPDATE đang chạy (role `app_batch`, `batch-job`) | `8512646466599369567`, trùng `Query Identifier` của `EXPLAIN (VERBOSE)`. Dùng được làm khoá hint table khi câu đang treo | `E8_*` |
| E9 | CASE 3 (Nested Loop quét lại CTE, DB `lab`): 3 dạng hint trong hint table | Không hint: 100 s. `HashJoin(cu a b)`: **95 s, không đủ** (hint được "used" nhưng planner dời Nested Loop xuống cặp `{a, b}`). `NoNestLoop(a b) NoNestLoop(b cu) NoNestLoop(a b cu)`: **0.84 s**. `Set(enable_nestloop off)`: 1.0 s | `E9_*` |

## Bẫy đã gặp

- **Tạo extension `pg_hint_plan` 1.7 khi `pg_hint_plan.enable_hint_table = on` sẽ lỗi.**
  - Lỗi báo `column "query_id" does not exist`. Nguyên nhân: hook tra hint table trong lúc script extension đang tạo bảng.
  - Cách xử lý: `SET pg_hint_plan.enable_hint_table = off;` rồi mới `CREATE EXTENSION`.
- **`compute_query_id` không có ở PG13**, nên `docker-compose.yml` chỉ đặt tham số này cho PG17.
- **Join hint cho đúng một tập alias là không đủ.** Planner vẫn có thể đặt Nested Loop ở tập con. Phải cấm cho mọi tập chứa phía bị quét lại; `pgdoctor remedy` sinh sẵn danh sách này. Muốn xem hint nào được dùng: `SET pg_hint_plan.debug_print = verbose; SET pg_hint_plan.message_level = notice;`.
- **`hash_mem_multiplier` phải đủ lớn.** Giá trị cần ≥ `rows × (width+24) / work_mem`. Đặt "gấp đôi mặc định" (4) chưa chắc đã đủ. Dùng `pgdoctor remedy` để tính.
