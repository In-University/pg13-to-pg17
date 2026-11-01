-- pgdoctor remedy runbook
-- target=cloudsql  role=app_batch  db=lab4  application_name=batch-job
-- Thứ tự: phương án KHÔNG sửa code, phạm vi hẹp nhất trước. Chạy phần 'verify' sau mỗi bước.
-- KHÔNG chạy cả file một lượt: mỗi phương án là một lựa chọn thay thế.

-- ====================================================================================================
-- Query: case4_update
-- ----------------------------------------------------------------------------------------------------
-- [1] pg_hint_plan hint table: Set(hash_mem_multiplier) CHỈ cho câu lệnh này   (KHÔNG sửa code; phạm vi: 1 câu lệnh (khoá query_id), chỉ application_name='batch-job'; cần restart)
--     áp dụng cho: SubPlan 2: Filter: (NOT (ANY (p.brand_code = (SubPlan 2).col1)))
--     Planner chỉ hash khi rows × (MAXALIGN(width) + 24) ≤ work_mem × hash_mem_multiplier: 381.8k × (32 + 24) B =
--     20.4 MB > 8.0 MB. Tối thiểu: work_mem ≥ 10440 kB (với hash_mem_multiplier 2) hoặc hash_mem_multiplier ≥ 5.10
--     (với work_mem 4.0 MB). Khuyến nghị gấp đôi để chịu được dữ liệu tăng: work_mem = 24MB hoặc hash_mem_multiplier
--     = 12. Hint Set() chỉ đổi tham số trong lúc LẬP PLAN (khi thực thi work_mem vẫn như cũ), nên không ảnh hưởng
--     câu nào khác và không làm sort/hash khác tốn thêm bộ nhớ.
--     Lab: CASE 4 (PG17.11, 2M dòng, 2 vCPU): không hash 1 228 s; hint/role setting → hashed SubPlan 4.1–5.6 s (PG13: 4.0 s). Ngưỡng đo được: 10381kB → SubPlan, 10382kB → hashed (khớp công thức).
--     ! query_id đổi nếu câu SQL đổi cấu trúc, hoặc bảng bị DROP/CREATE lại (query_id dựa trên OID) – kiểm tra lại sau
--       mỗi lần deploy/migration.
--     ! Cloud SQL: bật cloudsql.enable_pg_hint_plan cần restart → làm trong cửa sổ bảo trì.
--     ! Bảng băm của hashed SubPlan KHÔNG bị giới hạn bởi work_mem khi chạy (không spill): bộ nhớ ≈ số dòng THỰC TẾ
--       của subquery × 56 B + overhead hash table (ước lượng hiện tại 20.4 MB).
-- B1. Bật extension (Cloud SQL): flag cloudsql.enable_pg_hint_plan=on – CẦN RESTART instance.
--     gcloud sql instances patch <INSTANCE> --database-flags=cloudsql.enable_pg_hint_plan=on,<TẤT CẢ FLAG ĐANG CÓ>
--     (patch --database-flags GHI ĐÈ toàn bộ flag hiện có – phải liệt kê lại; hoặc sửa trên Console)
-- B2. Trong database lab4 (user thuộc cloudsqlsuperuser / owner):
SET pg_hint_plan.enable_hint_table = off;   -- BẮT BUỘC khi tạo extension, nếu đang on sẽ lỗi
                                            -- 'column "query_id" does not exist' (đã gặp trong lab)
CREATE EXTENSION IF NOT EXISTS pg_hint_plan;
SELECT extversion FROM pg_extension WHERE extname = 'pg_hint_plan';  -- PG17: 1.7.x (hint table khoá theo query_id)
-- B3. Lấy query_id
-- Query Identifier trong plan: 8512646466599369567. CHỈ dùng nếu plan này lấy từ đúng câu ứng dụng gửi:
--   câu dùng literal ('VN') và câu dùng bind parameter ($1, JDBC/prepared) có query_id KHÁC nhau (lab E3).
-- (a) Câu ĐANG chạy (vd. đang treo 2 giờ): pg_stat_activity.query_id (PG14+, cần compute_query_id = auto/on)
SELECT pid, query_id, now() - query_start AS running_for, application_name, usename, left(query, 100) AS query
FROM pg_stat_activity WHERE query ILIKE '%<một đoạn text đặc trưng của câu lệnh>%' AND pid <> pg_backend_pid();
-- (b) Câu đã chạy XONG ít nhất một lần (câu bị huỷ/timeout không được pg_stat_statements ghi lại):
SELECT queryid, calls, round(mean_exec_time) AS mean_ms, left(query, 120) AS query
FROM pg_stat_statements
WHERE query ILIKE '%<một đoạn text đặc trưng của câu lệnh>%' ORDER BY total_exec_time DESC LIMIT 10;
-- B4. Gắn hint (application_name '' = mọi ứng dụng):
INSERT INTO hint_plan.hints(query_id, application_name, hints)
VALUES (8512646466599369567, 'batch-job', 'Set(hash_mem_multiplier 12)');
-- B5. Bật hint table cho role ứng dụng (áp dụng cho SESSION MỚI – cần recycle connection pool):
ALTER ROLE app_batch IN DATABASE lab4 SET pg_hint_plan.enable_hint_table = on;
--     (hoặc flag instance pg_hint_plan.enable_hint_table=on – không cần restart, áp dụng mọi session)
-- verify:
-- Đăng nhập BẰNG role app_batch với application_name=batch-job trong một session MỚI, rồi:
BEGIN;
EXPLAIN <nguyên văn câu lệnh>;     -- không cần ANALYZE: plan hiện ra ngay
--   kỳ vọng: dòng Filter có chữ 'hashed SubPlan'
ROLLBACK;
-- rollback:
DELETE FROM hint_plan.hints WHERE query_id = 8512646466599369567;

-- ----------------------------------------------------------------------------------------------------
-- [2] ALTER ROLE … IN DATABASE … SET hash_mem_multiplier (không cần extension)   (KHÔNG sửa code; phạm vi: mọi câu của role app_batch trong DB lab4)
--     áp dụng cho: SubPlan 2: Filter: (NOT (ANY (p.brand_code = (SubPlan 2).col1)))
--     Planner chỉ hash khi rows × (MAXALIGN(width) + 24) ≤ work_mem × hash_mem_multiplier: 381.8k × (32 + 24) B =
--     20.4 MB > 8.0 MB. Tối thiểu: work_mem ≥ 10440 kB (với hash_mem_multiplier 2) hoặc hash_mem_multiplier ≥ 5.10
--     (với work_mem 4.0 MB). Khuyến nghị gấp đôi để chịu được dữ liệu tăng: work_mem = 24MB hoặc hash_mem_multiplier
--     = 12. hash_mem_multiplier chỉ nới bộ nhớ cho node dạng hash (Hash Join, HashAgg, hashed SubPlan); sort vẫn
--     dùng work_mem cũ – hẹp hơn tăng work_mem. Tốt nhất khi job batch có role riêng.
--     Lab: E2b: role-in-database hash_mem_multiplier = 8 (work_mem 4MB) → hashed SubPlan, 4.2 s.
--     ! Mỗi node hash của MỌI câu thuộc role này được dùng tới work_mem × hash_mem_multiplier: tính lại tổng RAM khi
--       nhiều connection chạy song song.
--     ! Bảng băm của hashed SubPlan KHÔNG bị giới hạn bởi work_mem khi chạy (không spill): bộ nhớ ≈ số dòng THỰC TẾ
--       của subquery × 56 B + overhead hash table (ước lượng hiện tại 20.4 MB).
ALTER ROLE app_batch IN DATABASE lab4 SET hash_mem_multiplier = 12;
-- Áp dụng cho session MỚI: recycle connection pool (HikariCP maxLifetime / restart pod / pgbouncer RECONNECT).
-- Kiểm tra trong session của ứng dụng: SELECT name, setting, source FROM pg_settings WHERE name = 'hash_mem_multiplier';
--   source phải là 'database user'. Nếu là 'client' => connection string đang gửi options -c ... ghi đè.
-- Nếu báo 'permission denied': PG16+ yêu cầu ADMIN OPTION trên role đó; hoặc để chính role ứng dụng chạy
--   ALTER ROLE CURRENT_USER IN DATABASE lab4 SET hash_mem_multiplier = 12;
-- verify:
-- Đăng nhập BẰNG role app_batch với application_name=batch-job trong một session MỚI, rồi:
BEGIN;
EXPLAIN <nguyên văn câu lệnh>;     -- không cần ANALYZE: plan hiện ra ngay
--   kỳ vọng: dòng Filter có chữ 'hashed SubPlan'
ROLLBACK;
-- rollback:
ALTER ROLE app_batch IN DATABASE lab4 RESET hash_mem_multiplier;

-- ----------------------------------------------------------------------------------------------------
-- [3] ALTER ROLE … IN DATABASE … SET work_mem   (KHÔNG sửa code; phạm vi: mọi câu của role app_batch trong DB lab4)
--     áp dụng cho: SubPlan 2: Filter: (NOT (ANY (p.brand_code = (SubPlan 2).col1)))
--     Planner chỉ hash khi rows × (MAXALIGN(width) + 24) ≤ work_mem × hash_mem_multiplier: 381.8k × (32 + 24) B =
--     20.4 MB > 8.0 MB. Tối thiểu: work_mem ≥ 10440 kB (với hash_mem_multiplier 2) hoặc hash_mem_multiplier ≥ 5.10
--     (với work_mem 4.0 MB). Khuyến nghị gấp đôi để chịu được dữ liệu tăng: work_mem = 24MB hoặc hash_mem_multiplier
--     = 12.
--     Lab: E2a: ALTER ROLE app_batch SET work_mem = '16MB' → hashed SubPlan, 5.6 s.
--     ! work_mem áp dụng cho MỌI sort/hash của role: rộng hơn hash_mem_multiplier.
--     ! Bảng băm của hashed SubPlan KHÔNG bị giới hạn bởi work_mem khi chạy (không spill): bộ nhớ ≈ số dòng THỰC TẾ
--       của subquery × 56 B + overhead hash table (ước lượng hiện tại 20.4 MB).
ALTER ROLE app_batch IN DATABASE lab4 SET work_mem = '24MB';
-- Áp dụng cho session MỚI: recycle connection pool (HikariCP maxLifetime / restart pod / pgbouncer RECONNECT).
-- Kiểm tra trong session của ứng dụng: SELECT name, setting, source FROM pg_settings WHERE name = 'work_mem';
--   source phải là 'database user'. Nếu là 'client' => connection string đang gửi options -c ... ghi đè.
-- Nếu báo 'permission denied': PG16+ yêu cầu ADMIN OPTION trên role đó; hoặc để chính role ứng dụng chạy
--   ALTER ROLE CURRENT_USER IN DATABASE lab4 SET work_mem = '24MB';
-- verify:
-- Đăng nhập BẰNG role app_batch với application_name=batch-job trong một session MỚI, rồi:
BEGIN;
EXPLAIN <nguyên văn câu lệnh>;     -- không cần ANALYZE: plan hiện ra ngay
--   kỳ vọng: dòng Filter có chữ 'hashed SubPlan'
ROLLBACK;
-- rollback:
ALTER ROLE app_batch IN DATABASE lab4 RESET work_mem;

-- ----------------------------------------------------------------------------------------------------
-- [4] Flag instance hash_mem_multiplier / work_mem   (KHÔNG sửa code; phạm vi: toàn instance)
--     áp dụng cho: SubPlan 2: Filter: (NOT (ANY (p.brand_code = (SubPlan 2).col1)))
--     Planner chỉ hash khi rows × (MAXALIGN(width) + 24) ≤ work_mem × hash_mem_multiplier: 381.8k × (32 + 24) B =
--     20.4 MB > 8.0 MB. Tối thiểu: work_mem ≥ 10440 kB (với hash_mem_multiplier 2) hoặc hash_mem_multiplier ≥ 5.10
--     (với work_mem 4.0 MB). Khuyến nghị gấp đôi để chịu được dữ liệu tăng: work_mem = 24MB hoặc hash_mem_multiplier
--     = 12. Cách nhanh nhất nhưng rộng nhất.
--     ! Ảnh hưởng mọi query của mọi ứng dụng; rủi ro OOM khi nhiều connection cùng dùng hash lớn.
gcloud sql instances patch <INSTANCE> --database-flags=hash_mem_multiplier=12,<TẤT CẢ FLAG ĐANG CÓ>
-- Cloud SQL: hash_mem_multiplier (1..1000) và work_mem là flag không cần restart; áp dụng cho session mới.
-- verify:
-- Đăng nhập BẰNG role app_batch với application_name=batch-job trong một session MỚI, rồi:
BEGIN;
EXPLAIN <nguyên văn câu lệnh>;     -- không cần ANALYZE: plan hiện ra ngay
--   kỳ vọng: dòng Filter có chữ 'hashed SubPlan'
ROLLBACK;
-- rollback:
-- đặt lại giá trị cũ của flag (Cloud SQL mặc định: work_mem 4MB, hash_mem_multiplier 2)

-- ----------------------------------------------------------------------------------------------------
-- [5] Sửa code: NOT IN → NOT EXISTS (anti-join) – sửa vĩnh viễn   (CẦN SỬA CODE; phạm vi: 1 câu lệnh)
--     áp dụng cho: SubPlan 2: Filter: (NOT (ANY (p.brand_code = (SubPlan 2).col1)))
--     NOT EXISTS được chuyển thành Hash/Merge Anti Join, không phụ thuộc ngưỡng bộ nhớ của hashed SubPlan. Chú ý ngữ
--     nghĩa NULL: nếu cột hai bên có thể NULL, dùng dạng tương đương đầy đủ bên dưới.
--     Lab: E7: NOT EXISTS → Hash Anti Join trên PG17 4.4 s, dạng null-safe 4.2 s; số dòng UPDATE giống hệt NOT IN (2500).
--     ! Cần build/deploy lại ứng dụng.
--     ! PG19 (chưa phát hành tại thời điểm viết) mới tự chuyển NOT IN → anti-join, và chỉ khi chứng minh được hai vế
--       không NULL.
-- Khi cả <x> và <s.col> đều NOT NULL:
... AND NOT EXISTS (SELECT 1 FROM <cte> s WHERE <điều kiện cũ> AND s.col = t.x)
-- Tương đương đầy đủ với x NOT IN (SELECT col FROM S) trong WHERE (kể cả khi có NULL):
... AND NOT EXISTS (SELECT 1 FROM S WHERE S.col = t.x)
    AND NOT EXISTS (SELECT 1 FROM S WHERE S.col IS NULL)
    AND (t.x IS NOT NULL OR NOT EXISTS (SELECT 1 FROM S))
-- verify:
-- So số dòng bị ảnh hưởng giữa câu cũ và câu mới trên bản clone (UPDATE ... RETURNING trong transaction rồi ROLLBACK)
-- rollback:
-- deploy lại bản cũ

-- ----------------------------------------------------------------------------------------------------
-- [6] KHÔNG hiệu quả với pattern này (đã thử trong lab)   (KHÔNG HIỆU QUẢ; phạm vi: -)
--     áp dụng cho: SubPlan 2: Filter: (NOT (ANY (p.brand_code = (SubPlan 2).col1)))
--     Index: SubPlan duyệt kết quả CTE/Materialize (tuplestore, không có index) – thêm index trên
--     products/brand/stores không đổi plan (lab E4). ANALYZE lại / tăng STATISTICS: ước lượng của PG17 vốn đã gần
--     đúng, vấn đề là ngưỡng. ALTER COLUMN … SET STATISTICS 0: ANALYZE KHÔNG xoá stats cũ (lab E5). CREATE
--     STATISTICS: không áp dụng cho cột CTE. Không có GUC nào tắt việc PG17 lấy stats qua CTE (f7816aec2). Revert
--     code ứng dụng: câu SQL cũ vẫn gặp planner PG17.

