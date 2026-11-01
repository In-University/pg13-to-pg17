# Xử lý incident PG13 → PG17: các giải pháp đã kiểm chứng (ưu tiên KHÔNG sửa code)

> Bối cảnh: Cloud SQL in-place upgrade 13 → 17. Câu `UPDATE` dạng
> `WITH a AS NOT MATERIALIZED (…), current_brand_code AS (… inner join nhiều bảng …) UPDATE … WHERE … NOT IN (SELECT … FROM current_brand_code WHERE …)`
> chạy 2 phút trên PG13 nhưng 2 giờ trên PG17. `SELECT` từ `current_brand_code` rất lâu. Đã `VACUUM`/`REINDEX`/`ANALYZE`. Revert code vẫn chậm. Sửa SQL rồi build lại thì làm được nhưng khó.
>
> Tài liệu này trả lời: **làm gì ngay khi chưa thể build lại**, vì sao revert không giúp, đánh index có giúp không, và cách sửa bền vững.
> Mọi con số trong tài liệu đều đo trong lab [`lab/remedies/`](../lab/remedies/README.md) (PG 13.23 và PG 17.11 + pg_hint_plan 1.7.2, cùng dữ liệu CASE 4, máy 2 vCPU / 2 GB). Kết quả nguyên văn: [`lab/results/remedies/`](../lab/results/remedies/).
> Nguyên nhân gốc và kết quả lab của các case khác: [`pg13-vs-pg17-query-regression.md`](pg13-vs-pg17-query-regression.md). Giải thích EXPLAIN từng dòng: [`explain-walkthrough.md`](explain-walkthrough.md).

---

## 0. TL;DR

| Khi nào | Làm gì | Sửa code? | Phạm vi ảnh hưởng | Đã kiểm chứng |
|---|---|---|---|---|
| **5 phút đầu** | `EXPLAIN` (không ANALYZE) câu lệnh **bằng role của ứng dụng**. Tìm chữ `hashed SubPlan`. Chạy `pgdoctor remedy` để tính ngưỡng (Mục 2) | Không | — | E0, E1 |
| **Cứu ngay, không restart** | `ALTER ROLE <role batch> IN DATABASE <db> SET hash_mem_multiplier = <N>` rồi recycle connection pool (Mục 3.2) | Không | Mọi câu của role đó trong DB đó | E2, E2b: 4.2 s |
| **Hẹp nhất (khuyến nghị)** | pg_hint_plan **hint table**: `Set(hash_mem_multiplier N)` chỉ cho đúng câu này, có thể chỉ cho `application_name` của job (Mục 3.1) | Không | 1 câu lệnh | E3, E3b, E8: 4.1 s |
| Nếu không đổi được role | Flag instance `hash_mem_multiplier` / `work_mem` (Mục 3.3) | Không | Toàn instance | (giống E2) |
| **Bền vững** | Viết lại `NOT IN` → `NOT EXISTS` (anti-join) rồi build lại (Mục 6) | Có | 1 câu lệnh | E6, E7: 4.2–4.4 s, số dòng giống hệt |
| Đừng làm | Thêm index, `ANALYZE` lại, `SET STATISTICS 0`, `CREATE STATISTICS`, revert code, tắt JIT trên Cloud SQL (Mục 5) | — | — | E4, E5 |

Trong lab, plan xấu của PG17 chạy **1 228 s (20.5 phút)** trên 2 vCPU (lab 8 vCPU trước đây đo 257–289 s). Bất kỳ phương án nào ở trên cũng đưa nó về **~4–6 s**, bằng PG13 (4.0–4.2 s trên cùng máy).

---

## 1. Vì sao revert code không giúp

1. **Câu SQL không đổi, planner đổi.** PG17 (commit `f7816aec2`) lấy thống kê của **cột bảng gốc** cho các cột của CTE đã materialize. PG13 không có thống kê cho cột CTE nên dùng selectivity mặc định 0.5%.
   - Trong lab: điều kiện `cbc.region = 'VN'`, PG13 ước lượng **4 717** dòng, PG17 ước lượng **379 670** dòng (thực tế 400 000).
2. **Ước lượng đúng hơn lại vượt ngưỡng hash.** `NOT IN (subquery)` chỉ được hash khi:

   ```
   rows × (MAXALIGN(width) + 24) ≤ work_mem × hash_mem_multiplier
   ```

   Công thức này là `subplan_is_hashable()` trong `subselect.c`, giống hệt nhau từ PG13 đến PG18.
   - PG17: 379 670 × 56 B = 20.3 MB, lớn hơn 4 MB × 2 = 8 MB, nên planner **không hash**.
   - Khi không hash, mỗi dòng của bảng ngoài phải duyệt **tuyến tính** kết quả CTE. Độ phức tạp là O(N×M).
   - PG13: 4 717 × 56 B = 258 kB, nhỏ hơn 4 MB, nên **hash**.
3. **Không có công tắc nào để tắt hành vi mới.**
   - Không có GUC nào tắt `f7816aec2` (đã kiểm tra `guc_tables.c` và `selfuncs.c` của REL_17).
   - Cộng đồng PostgreSQL coi đây là hành vi đúng. TPC-DS q95 chậm 2× trên PG17 vì commit này và đã được báo lên pgsql-hackers, nhưng không được sửa ([thread](https://www.postgresql.org/message-id/4e14cc4f-edaf-0820-2bdf-db8dd0a9efab%40gmail.com)).
4. **Quay lại PG13 cũng gần như không khả thi.**
   - Cloud SQL chỉ cho rollback bằng cách restore **backup trước upgrade** sang một **instance mới** chạy PG13 ([docs](https://docs.cloud.google.com/sql/docs/postgres/upgrade-major-db-version-inplace)).
   - Ứng dụng phải trỏ sang instance mới.
   - Mọi ghi nhận từ sau thời điểm backup sẽ mất nếu không tự phát lại.

Kết luận: phải can thiệp vào **quyết định của planner trên PG17**. Có hai cách: cho planner thêm "ngân sách" bộ nhớ (không cần sửa code), hoặc đổi câu SQL sang dạng không phụ thuộc ngưỡng (cần sửa code).

> Một trường hợp tương tự đã được báo trên pgsql-performance: nâng cấp 12.15 → 17.2, một câu từ 250 ms lên 377 s, và tăng `work_mem` thì sửa được ([thread](https://www.postgresql.org/message-id/43ed5a72fdfdd44fa046d8911c496364a3eb9231.camel@cybertec.at)). Tom Lane mô tả cơ chế này là: "hashed subplan … không hash thì inner bị quét lại cho mỗi dòng ngoài, O(N^2)" ([mail](https://postgresql.org/message-id/6343.1491401090%40sss.pgh.pa.us)).

---

## 2. Chẩn đoán trong 5 phút, không phải chờ 2 giờ

```sql
-- Đăng nhập BẰNG role của ứng dụng (để có đúng work_mem / hash_mem_multiplier của nó)
BEGIN;
EXPLAIN (VERBOSE, SETTINGS) <nguyên văn câu UPDATE>;   -- KHÔNG có ANALYZE: ra kết quả ngay
ROLLBACK;
```

| Dòng trong plan | Ý nghĩa |
|---|---|
| `Filter: (NOT (ANY (brand_code = (hashed SubPlan 2).col1)))` | Tốt: subquery chạy 1 lần rồi tra bảng băm |
| `Filter: (NOT (ANY (brand_code = (SubPlan 2).col1)))` | **Xấu**: không có chữ `hashed`, duyệt danh sách cho từng dòng. Đây đúng là CASE 4 |
| `CTE Scan on current_brand_code cbc (… rows=379670 width=32)` | Đầu vào để tính ngưỡng: rows × (32 + 24) |
| `Query Identifier: 8512646466599369567` | Khoá cho hint table (Mục 3.1) |

Nếu kết nối được cả PG13 (instance restore từ backup trước upgrade) lẫn PG17 bằng `psql`, một lệnh làm hết các việc trên. Nó chạy `EXPLAIN` ở hai bên **bằng role của ứng dụng**, liệt kê các tham số khác nhau có ảnh hưởng tới plan của chính câu này (kể cả nguồn của giá trị: mặc định, flag, `ALTER ROLE`, client), tính quyết định hash ở hai bên và sinh runbook ([`pgdoctor.md` Mục 1.2](pgdoctor.md#12-chạy-trực-tiếp-trên-hai-server-run-capture-qdiff-từ-030)):

```bash
python .claude/skills/pgdoctor/pgdoctor_cli.py run --old-dsn "<pg13>" --new-dsn "<pg17>" --query-file q.sql \
       --app-name <application_name của job> --analyze old --out-dir run1
```

Tính ngưỡng tự động và sinh sẵn SQL khắc phục từ file plan:

```bash
python .claude/skills/pgdoctor/pgdoctor_cli.py remedy --plan q.pg17.json \
       --app-role <role> --app-db <db> --app-name <application_name của job> --keyword current_brand_code -o fix.sql
```

Với plan của lab, tool tính ra **work_mem ≥ 10382 kB** (hoặc `hash_mem_multiplier ≥ 5.07`). Đo thật trong lab (E1): `10381kB` thì vẫn là `SubPlan`, `10382kB` thì thành `hashed SubPlan`. **Khớp tới từng kB.**

### "SELECT từ current_brand_code rất lâu" nghĩa là gì?

Thời gian hiển thị ở node `CTE Scan on current_brand_code` gồm hai phần khác hẳn nhau:

1. **Thân CTE** (join nhiều bảng): chạy **một lần**.
   - Lab PG17: 1.9 s.
   - Lab PG13: 2.5 s (cache lạnh hơn).
2. **Quét lại** kết quả đã lưu: `loops = số dòng ngoài`.
   - Lab PG17: 5 000 lần, tổng **17 phút**, chiếm **83%** của 1 228 s.

Rule mới `PLAN-CTE-COST` của pgdoctor tách hai phần này từ `EXPLAIN ANALYZE`.

Plan PG17 của lab còn cho thấy **48 GB đọc từ file tạm** (`PLAN-TEMP-IO`):
- kết quả CTE có 1M dòng, khoảng 20 MB, vượt `work_mem` 4MB nên tuplestore bị ghi ra đĩa;
- mỗi lần quét lại đều đọc lại file đó;
- trên Cloud SQL, phần này tiêu tốn IOPS/throughput của disk.

Tăng `work_mem` giúp giữ tuplestore trong RAM, nhưng chỉ gỡ được phần I/O. Cách sửa thật sự là cho SubPlan được hash, để kết quả CTE chỉ được đọc **một lần**.

- Nếu phần **quét lại** áp đảo: sửa nguyên nhân quét lại (Mục 3). Thêm index cho các bảng trong thân CTE **không giúp gì**.
- Nếu phần **thân CTE** áp đảo, nghĩa là bản thân join chậm: xem Mục 7.

Phân biệt "đang tính" với "đang chờ" khi câu đang chạy:

```sql
SELECT pid, query_id, now() - query_start AS running_for, wait_event_type, wait_event, left(query, 80)
FROM pg_stat_activity WHERE state = 'active' AND query ILIKE '%current_brand_code%';
```

Nếu `wait_event` gần như luôn NULL thì câu lệnh đang dùng CPU, tức là vấn đề nằm ở plan. Nếu là `Lock` / `IO` / `WALWrite` thì xem Mục 2.6.4 của tài liệu chính.

---

## 3. Giải pháp KHÔNG sửa code (đã đo trong lab)

### 3.1 pg_hint_plan hint table: chỉ tác động lên đúng câu này (khuyến nghị)

Hint table gắn một hint vào câu lệnh theo **`query_id`** mà không sửa SQL. Hint `Set(...)` chỉ đổi tham số **trong lúc lập plan**. Lab E3 xác nhận: lúc thực thi `work_mem` vẫn là 4MB, nên các câu khác và các sort/hash khác không bị ảnh hưởng.

```sql
-- B1. Cloud SQL: bật flag cloudsql.enable_pg_hint_plan = on  (CẦN RESTART, làm trong cửa sổ bảo trì)
--     gcloud sql instances patch <INSTANCE> --database-flags=cloudsql.enable_pg_hint_plan=on,<TẤT CẢ FLAG ĐANG CÓ>
--     Lưu ý: patch --database-flags GHI ĐÈ toàn bộ flag hiện có.

-- B2. Trong database của ứng dụng (user thuộc cloudsqlsuperuser):
SET pg_hint_plan.enable_hint_table = off;        -- BẮT BUỘC. Nếu đang on, CREATE EXTENSION lỗi
CREATE EXTENSION IF NOT EXISTS pg_hint_plan;     --   'column "query_id" does not exist' (gặp thật trong lab)

-- B3. Lấy query_id của ĐÚNG câu ứng dụng gửi
--   (a) câu đang chạy:  SELECT pid, query_id, left(query,80) FROM pg_stat_activity WHERE query ILIKE '%current_brand_code%';
--   (b) câu đã chạy xong: SELECT queryid, calls, left(query,120) FROM pg_stat_statements WHERE query ILIKE '%current_brand_code%';

-- B4. Gắn hint. Đặt application_name = tên job để chỉ job đó nhận hint ('' = mọi ứng dụng)
INSERT INTO hint_plan.hints(query_id, application_name, hints)
VALUES (<query_id>, '<application_name>', 'Set(hash_mem_multiplier 12)');

-- B5. Bật hint table cho role ứng dụng (áp dụng cho session MỚI)
ALTER ROLE <app_role> IN DATABASE <db> SET pg_hint_plan.enable_hint_table = on;
--     hoặc flag pg_hint_plan.enable_hint_table = on (không cần restart, áp dụng mọi session)

-- Kiểm tra: session MỚI của role ứng dụng, chạy EXPLAIN <câu lệnh>; phải thấy 'hashed SubPlan'
-- Hoàn tác: DELETE FROM hint_plan.hints WHERE query_id = <query_id>;
```

Những điểm đã kiểm chứng trong lab:

- **E3, `query_id` phụ thuộc cách gửi câu lệnh.**
  - Cùng câu, viết literal `'VN'` cho query_id `8512646466599369567`.
  - Viết bằng bind parameter `$1` (JDBC / prepared statement) cho query_id `8959202264040175023`.
  - Phải lấy query_id từ `pg_stat_activity` hoặc `pg_stat_statements` của **chính ứng dụng**, không lấy từ câu tự gõ trong psql.
- **E3b, lọc theo `application_name`.** Hint gắn `application_name = 'batch-job'`:
  - session `batch-job` nhận `hashed SubPlan`;
  - session `web-api` cùng role vẫn nhận `SubPlan`.
- **E8, câu đang chạy.** `pg_stat_activity.query_id` của câu UPDATE đang chạy trùng với `Query Identifier` của `EXPLAIN (VERBOSE)`.
- **pg_hint_plan 1.7 dùng `query_id`.** Trên PG17, pg_hint_plan 1.7.x khoá hint table theo `query_id` (từ 1.7.0). Các bản 1.6.x trở về trước khoá theo text đã chuẩn hoá. Cần `compute_query_id = auto` (mặc định) hoặc `on`; trên Cloud SQL đây không phải flag nên cứ để mặc định.
- **`query_id` có thể đổi.** Nó đổi khi câu SQL đổi cấu trúc, hoặc khi bảng bị `DROP`/`CREATE` lại (query_id dựa trên OID). Cần kiểm tra lại sau mỗi lần migration.

### 3.2 `ALTER ROLE … IN DATABASE … SET`: theo role, không cần extension, không restart

```sql
-- Hẹp hơn work_mem: chỉ nới bộ nhớ cho node dạng hash (Hash Join, HashAgg, hashed SubPlan)
ALTER ROLE <app_role> IN DATABASE <db> SET hash_mem_multiplier = 12;
-- hoặc
ALTER ROLE <app_role> IN DATABASE <db> SET work_mem = '24MB';
```

Lab: E2a `work_mem = 16MB` chạy 5.6 s; E2b `hash_mem_multiplier = 8` (work_mem vẫn 4MB) chạy 4.2 s. Cả hai đều ra `hashed SubPlan`.

Thứ tự ưu tiên (đo trong lab E2, khớp với [docs](https://www.postgresql.org/docs/17/sql-alterrole.html)):

| Nguồn | `pg_settings.source` | Ghi đè được |
|---|---|---|
| Flag instance / `postgresql.conf` | `configuration file` | — |
| `ALTER DATABASE db SET` | `database` | flag |
| `ALTER ROLE r SET` | `user` | database |
| `ALTER ROLE r IN DATABASE db SET` | `database user` | role, database |
| Connection string `options=-c work_mem=…` / `PGOPTIONS` | `client` | **mọi thứ ở trên** |
| `SET` trong session | `session` | tất cả |

Bẫy khi triển khai:

1. **Chỉ áp dụng cho session MỚI.** Phải recycle connection pool (HikariCP `maxLifetime`, restart pod, `RECONNECT` trên pgbouncer).
2. **Connection string có thể ghi đè.** Nếu connection string của ứng dụng gửi `options=-c work_mem=…`, giá trị đó thắng (lab E2: `source = client`). Kiểm tra bằng `SELECT name, setting, source FROM pg_settings WHERE name IN ('work_mem','hash_mem_multiplier');` ngay trong session của ứng dụng.
3. **Có thể bị từ chối quyền (PG16+).** Role có CREATEROLE chỉ đổi được default của role mà nó có **ADMIN OPTION**. Nếu bị `permission denied`, cho chính role ứng dụng chạy `ALTER ROLE CURRENT_USER IN DATABASE <db> SET …`.
4. **Phạm vi rộng.** Thay đổi áp dụng cho **mọi** câu của role đó. Tốt nhất là job batch có role riêng. Đổi role trong cấu hình kết nối là đổi config, không phải đổi code.

### 3.3 Flag instance (rộng nhất)

Trên Cloud SQL, `work_mem` (mặc định 4 MB, mọi tier) và `hash_mem_multiplier` (1..1000, mặc định 2) đều là flag **không cần restart** ([flags](https://docs.cloud.google.com/sql/docs/postgres/flags)).

```bash
gcloud sql instances patch <INSTANCE> --database-flags=hash_mem_multiplier=12,<TẤT CẢ FLAG ĐANG CÓ>
```

Thay đổi ảnh hưởng mọi query của mọi ứng dụng. Mỗi node hash của mỗi connection được phép dùng tới `work_mem × hash_mem_multiplier`, nên phải tính lại RAM khi nhiều connection chạy song song.

### 3.4 Chọn giá trị và đánh giá rủi ro bộ nhớ

- **Giá trị tối thiểu** là `rows × (MAXALIGN(width) + 24) / work_mem`. pgdoctor tính sẵn con số này và khuyến nghị **gấp đôi**, vì ước lượng tăng theo dữ liệu. Nếu dữ liệu tăng mà vượt ngưỡng lần nữa, câu lệnh sẽ âm thầm quay lại plan xấu.
- **Bộ nhớ khi chạy không bị giới hạn.** Bảng băm của hashed SubPlan **không** bị chặn bởi work_mem lúc chạy và không spill ra đĩa (`nodeSubplan.c`). Bộ nhớ thật xấp xỉ số dòng **thực tế** của subquery × (width + 24) cộng overhead của bảng băm. Lab: 400k dòng tức khoảng 20–40 MB. Với subquery hàng chục triệu dòng thì nên dùng NOT EXISTS (Mục 6).
- **Theo dõi sau khi áp dụng:**
  - Query Insights (sample plan);
  - `auto_explain`: flag `cloudsql.enable_auto_explain` (cần restart) và `auto_explain.log_min_duration`;
  - `pg_stat_statements.mean_exec_time` của câu lệnh.

---

## 4. Đánh index có giúp không?

**Với cơ chế này thì không** (lab E4). Đã tạo 5 index trên mọi cột liên quan: `products(brand_code)`, `products(status, brand_code)`, `brand_assignments(store_id, brand_id) WHERE is_current`, `stores(region) WHERE active`, `brands(code)`, rồi `ANALYZE`. Plan PG17 vẫn là `(SubPlan 2)` không hash. Lý do:

- `NOT IN (subquery)` không thể chuyển thành join (PG13–PG18). Planner chỉ có hai lựa chọn: hashed SubPlan hoặc SubPlan tuyến tính.
- SubPlan duyệt **kết quả của CTE** (tuplestore trong bộ nhớ/tệp tạm). Kết quả này không có index và không thể đánh index.
- Quyết định có hash hay không chỉ phụ thuộc vào **ước lượng số dòng × width** và **ngưỡng bộ nhớ**. Index không thay đổi cả hai.

Index **có ích** trong các trường hợp sau:

1. **Thân CTE chậm.** `PLAN-CTE-COST` cho thấy phần thân chiếm phần lớn thời gian. Khi đó index cho khoá join/điều kiện lọc **bên trong** CTE có ích.
2. **Sau khi viết lại NOT EXISTS.** Anti-join có thể dùng index trên khoá nối (`Nested Loop Anti Join` + Index Scan) nếu bảng ngoài nhỏ.
3. **SubPlan tương quan.** Khi subquery tham chiếu cột của query ngoài thì không bao giờ hash được. Index trên cột tương quan bên trong subquery biến mỗi lần chạy lại thành một index lookup.

---

## 5. Những thứ KHÔNG hiệu quả (tránh mất thời gian)

| Thử | Kết quả | Bằng chứng |
|---|---|---|
| Revert code ứng dụng | Câu SQL giống hệt, planner PG17 vẫn chọn như cũ | Mục 1 |
| `ANALYZE` lại / tăng `STATISTICS` | Ước lượng của PG17 vốn đã gần đúng (380k so với 400k thực tế). Vấn đề là ngưỡng, không phải ước lượng | E0 |
| `ALTER TABLE stores ALTER COLUMN region SET STATISTICS 0; ANALYZE` | `ANALYZE` bỏ qua cột nhưng **không xoá** stats cũ, plan không đổi | E5 |
| `CREATE STATISTICS` | Extended stats gắn với bảng gốc, không áp dụng cho cột của CTE Scan | CASE 3 (tài liệu chính) |
| Thêm index | Xem Mục 4 | E4 |
| `SET jit = off` | Cloud SQL **không hỗ trợ** LLVM JIT ([features](https://docs.cloud.google.com/sql/docs/postgres/features)). Phần JIT thấy trong lab/self-hosted không có trên Cloud SQL | — |
| Chờ autovacuum / reindex | Không liên quan đến quyết định hash | — |

Những thủ thuật **có tác dụng nhưng không khuyến nghị**:
- Xoá thống kê cột bằng `ALTER TABLE … ALTER COLUMN … TYPE <cùng kiểu>` (xoá `pg_statistic` của cột) kết hợp `SET STATISTICS 0`, để PG17 quay về selectivity mặc định như PG13. Cách này cần lock ACCESS EXCLUSIVE và làm hỏng ước lượng của mọi query khác dùng cột đó.
- `pg_clear_attribute_stats()` chỉ có từ PG18.

---

## 6. Sửa code (bền vững, khi build lại được)

### 6.1 `NOT IN` khác `NOT EXISTS` ở đâu, và vì sao `NOT EXISTS` nhanh hơn

**Tóm tắt một câu:**
- `NOT IN` có ngữ nghĩa NULL đặc biệt, nên planner **không được** đổi nó thành join.
- Vì vậy `NOT IN` chỉ có hai cách chạy:
  - bảng băm nằm trọn trong RAM;
  - với **mỗi** dòng ngoài, quét lại toàn bộ subquery.
- Ranh giới giữa hai cách đó là một ngưỡng bộ nhớ cứng.
- `NOT EXISTS` thì được đổi thành **anti join**, tức một node join bình thường:
  - planner được chọn Hash, Merge hoặc Nested Loop;
  - Hash Join tràn bộ nhớ thì ghi ra đĩa theo batch và chậm dần, không rơi xuống vực.

Dưới đây là chi tiết. Phần nói về mã nguồn đã đối chiếu với `REL_17_STABLE`; mọi con số lấy từ `lab/results/remedies/`.

#### a. Khác về ngữ nghĩa: logic ba giá trị

`x NOT IN (s1, s2, …, sM)` là cách viết gọn của `x <> s1 AND x <> s2 AND … AND x <> sM`. Mỗi phép `<>` cho TRUE, FALSE hoặc **NULL** (không biết):

| Tình huống | `x NOT IN (S)` | `NOT EXISTS (… WHERE S.v = x)` | Lý do |
|---|---|---|---|
| `x` có trong `S` | FALSE | FALSE | Giống nhau |
| `x` không có trong `S`, `S` không có NULL | TRUE | TRUE | Giống nhau |
| `x` không có trong `S`, nhưng `S` **có một dòng NULL** | **NULL** → dòng **không** được update | TRUE → dòng **được** update | `x <> NULL` là NULL, kéo cả chuỗi `AND` thành NULL |
| `x` là NULL, `S` không rỗng | **NULL** → không update | TRUE → update | `NULL <> s` là NULL; còn `NULL = s` không khớp dòng nào nên "không tồn tại" |
| `S` rỗng | TRUE | TRUE | Giống nhau |

Bảng này đã chạy thật ở lab E6 (`E6_null_semantics.txt`).

`NOT EXISTS` chỉ hỏi "có dòng nào khớp không?", nên luôn trả TRUE hoặc FALSE, không bao giờ trả NULL.

#### b. Khác về cách planner xử lý

1. **Bước kéo subquery lên thành join** (`pull_up_sublinks_qual_recurse()` trong `prepjointree.c`).
   - Ngay dưới một `NOT`, chỉ dạng `EXISTS` được chuyển: `convert_EXISTS_sublink_to_join(…, under_not = true)` sinh ra `JOIN_ANTI`. Mọi dạng khác được trả về nguyên trạng (comment trong code: "Else return it unmodified").
   - `NOT IN` thực chất là `NOT (x = ANY (subquery))`, nên **luôn** ở lại dạng SubPlan.
   - Lý do: anti join chỉ phân biệt được hai trạng thái "có dòng khớp" và "không có dòng khớp". Nó không biểu diễn được trạng thái NULL ở hai dòng giữa của bảng mục a. Nếu đổi `NOT IN` thành anti join thì những dòng đó sẽ bị update sai.
   - So sánh với `IN` (không có `NOT`): `IN` **được** đổi thành semi join, vì trong `WHERE` thì FALSE và NULL đều loại dòng. Thêm `NOT` thì FALSE thành TRUE nhưng NULL vẫn là NULL, nên sự khác biệt đó lộ ra.
2. **`NOT IN` ở lại thành một biểu thức `SubPlan` trong `Filter`** và được tính cho từng dòng ngoài. Executor (`nodeSubplan.c`) chỉ có hai cách chạy nó:

   | | `hashed SubPlan` | `SubPlan` thường (`ExecScanSubPlan`) |
   |---|---|---|
   | Cách chạy | Chạy subquery **1 lần**, nạp vào bảng băm trong RAM. Các dòng có NULL vào một bảng riêng (`hashnulls`) để giữ đúng ngữ nghĩa NULL. Mỗi dòng ngoài tra bảng băm 1 lần | Comment trong code: "default case where we have to **rescan subplan each time**". Mỗi dòng ngoài quét lại subquery từ đầu và dừng ở dòng khớp đầu tiên (`break; /* needn't look at any more rows */`) |
   | Chi phí | ≈ M + N | Dòng **không** có trong tập phải đọc **hết** M dòng, nên ≈ N × M |
   | Khi vượt bộ nhớ | **Không có cơ chế ghi ra đĩa**: `nodeSubplan.c` không hề kiểm tra `work_mem` lúc chạy | Không cần bảng băm |

   Vì bảng băm không thể tràn ra đĩa, planner phải quyết định ngay lúc lập plan xem có dám hash hay không. Đó là `subplan_is_hashable()`: ước lượng `rows × (width + 24)` phải ≤ `work_mem × hash_mem_multiplier`. Chỉ cần vượt 1 kB là rơi thẳng sang cột phải (E1: `10381kB` không hash, `10382kB` hash).
3. **`NOT EXISTS` trở thành node `Anti Join` trong cây join.** Planner chọn cách join theo chi phí như mọi join khác:
   - `Hash Anti Join`;
   - `Merge Anti Join`;
   - `Nested Loop Anti Join` + Index Scan;
   - từ PG16 có thêm `Right Anti Join`, băm phía nhỏ hơn.

   Hash Join **có** cơ chế tràn bộ nhớ: nó chia dữ liệu thành nhiều batch và ghi ra file tạm. Thời gian tăng dần theo lượng dữ liệu chứ không nhảy vọt, nên **không cần** kiểm tra ngưỡng như `subplan_is_hashable()`.

#### c. Hình dung bằng mã giả, với số liệu của lab

```
NOT IN, SubPlan không hash (PG17)              NOT EXISTS → Hash Anti Join (PG17)
---------------------------------              -----------------------------------
for p in products PENDING (5 000 dòng):        h = hash(CTE lọc region='VN')    -- 1 lần, 400k dòng
    for c in CTE (1 000 000 dòng, file tạm):   for p in products PENDING (5 000 dòng):
        if c.region='VN' and c.code=p.code:        if p.brand_code not in h:      -- tra băm O(1)
            break        -- thấy: dừng sớm             update p
    else: update p       -- không thấy: đã đọc hết 1M dòng
```

Đúng 2 500 dòng cần update lại chính là 2 500 dòng **không** tìm thấy. Mỗi dòng đó phải đọc trọn 1 triệu dòng của CTE.

#### d. Đo thực tế trong lab (cùng dữ liệu, `work_mem = 4MB`)

| | `NOT IN`, PG17 (`SubPlan`) | `NOT IN`, PG13 (`hashed SubPlan`) | `NOT EXISTS`, PG17 (`Hash Anti Join`) |
|---|---|---|---|
| Số lần quét CTE (`loops`) | **5 000** | 1 | 1 |
| Tổng số dòng CTE phải đọc | ≈ **2.5 tỷ** (trung bình 500 630 dòng/lần × 5 000) | 1 000 000 | 1 000 000 |
| Đọc file tạm (`temp read`) | 6 348 856 block ≈ **48 GB** | 0 | 2 602 block ≈ 20 MB |
| Bộ nhớ băm | — | 400k dòng, không bị giới hạn | 3 971 kB, **8 batch** (vượt 4 MB nên chia batch, vẫn nhanh) |
| Thời gian | **1 228 s** | 4.0 s | **4.4 s** |

Nguồn: `E0_pg17_baseline.json`, `E0_pg13_baseline.txt`, `E7_pg17_notexists.txt`.

Lượng công việc chênh nhau khoảng 2 500 lần. Thời gian chỉ chênh khoảng 280 lần vì cả hai phương án đều phải trả chi phí cố định 2–3 s để chạy thân CTE.

Dòng `Batches: 8` ở cột `NOT EXISTS` minh hoạ rõ khác biệt:
- cùng một bảng băm vượt `work_mem`;
- với Hash Join, vượt bộ nhớ chỉ có nghĩa là chia batch, tốn thêm vài trăm ms (Hash node 4 220 ms so với CTE Scan 3 888 ms);
- với `NOT IN`, vượt ngưỡng nghĩa là chuyển sang O(N×M).

#### e. Nghịch lý: PG17 ước lượng **đúng hơn** nhưng chạy **chậm hơn**

- **PG13** ước lượng CTE còn 4 717 dòng, sai 85 lần so với thực tế 400 000.
  - Với ước lượng đó, bảng băm "vừa" bộ nhớ nên planner chọn hash.
  - Lúc chạy, bảng băm 400k dòng vẫn được dựng trọn trong RAM, vì không có giới hạn lúc chạy. Kết quả nhanh.
  - PG13 nhanh **nhờ may mắn** do ước lượng sai.
- **PG17** ước lượng 379 670 dòng, gần đúng.
  - 20.3 MB lớn hơn 8 MB, nên planner từ chối hash vì bảng băm này không tràn ra đĩa được.
  - Chỉ còn lại lựa chọn O(N×M).

Gốc của vấn đề là `NOT IN (subquery)` không có đường lùi ở giữa hai cách chạy. `NOT EXISTS` loại bỏ được điểm gãy đó, nên nó là cách sửa **bền vững**: dữ liệu tăng hay ước lượng đổi thì plan chỉ chậm dần chứ không gãy.

#### f. Khi nào `NOT EXISTS` **không** được đổi thành anti join

Nếu subquery thuộc một trong các dạng sau thì `NOT EXISTS` vẫn ở lại thành SubPlan (`convert_EXISTS_sublink_to_join()` và `simplify_EXISTS_query()` trong `subselect.c`):
- có `WITH` **bên trong** subquery (tham chiếu CTE khai báo ở câu ngoài như `current_brand_code` thì vẫn được, E7 đã kiểm chứng);
- có aggregate, window function, `UNION`/`INTERSECT`/`EXCEPT`, `HAVING`, `OFFSET`, `FOR UPDATE`;
- tham chiếu cột của câu ngoài ở chỗ khác ngoài `WHERE` của subquery;
- `WHERE` của subquery chứa hàm volatile;
- `WHERE` của subquery không tham chiếu cột nào của câu ngoài. Khi đó nó thành `InitPlan`, chỉ chạy 1 lần, nên vẫn nhanh.

Kể cả khi vẫn là SubPlan, `EXISTS` dừng ở dòng khớp đầu tiên và tận dụng được index trên cột tương quan. Vì vậy nó vẫn tốt hơn `NOT IN` không hash.

Sau khi viết lại, luôn `EXPLAIN` để chắc chắn plan có `Anti Join`.

### 6.2 Viết lại `NOT IN` → `NOT EXISTS`

`NOT EXISTS` được chuyển thành **Hash/Merge Anti Join**, không phụ thuộc ngưỡng hashed SubPlan:

```sql
-- Khi cả hai vế đều NOT NULL (trường hợp của lab: products.brand_code, brands.code là NOT NULL)
WHERE p.status = 'PENDING'
  AND NOT EXISTS (SELECT 1 FROM current_brand_code cbc
                  WHERE cbc.region = 'VN' AND cbc.brand_code = p.brand_code)
```

Nếu cột **có thể NULL**, `NOT IN` và `NOT EXISTS` cho kết quả khác nhau:
- `NOT IN` cho NULL (không update) khi danh sách chứa NULL hoặc khi vế trái NULL.
- `NOT IN` cho TRUE khi danh sách rỗng.

Dạng tương đương **đầy đủ** trong `WHERE` (đã kiểm bằng bảng chân trị ở E6):

```sql
WHERE NOT EXISTS (SELECT 1 FROM S WHERE S.col = t.x)
  AND NOT EXISTS (SELECT 1 FROM S WHERE S.col IS NULL)          -- InitPlan: chạy 1 lần, dừng ở dòng đầu
  AND (t.x IS NOT NULL OR NOT EXISTS (SELECT 1 FROM S))          -- InitPlan
```

Lab E7 (PG17): `NOT EXISTS` chạy 4.4 s, dạng null-safe 4.2 s (đều là Hash Anti Join). Số dòng bị UPDATE của cả ba dạng đều là **2 500**.

### 6.3 Các thay đổi khác

- **CTE dùng một lần:** dùng `NOT MATERIALIZED` để planner thấy bảng gốc kèm điều kiện.
- **Tập trung gian lớn:** dùng bảng tạm + index + `ANALYZE` để có stats của chính tập con.
- **UPDATE lớn:** chia theo khoảng khoá để giảm WAL/lock, và để một plan xấu không kéo dài vài giờ.
- **Rà soát codebase:** tìm mọi `NOT IN (SELECT` (ví dụ `grep -rniE "not\s+in\s*\(\s*select"`). Mỗi chỗ là một "quả bom hẹn giờ" khi dữ liệu tăng hoặc khi lên version.

### 6.4 Ghi chú cho lần nâng cấp sau (PG18/PG19)

- **PG18:** `pg_upgrade` giữ lại optimizer statistics, nhưng công thức hash của SubPlan **không đổi**.
- **Nhánh PG19 (`REL_19_STABLE`):**
  - `EstimateSubplanHashTableSpace()` (commit `1ea5bdb00`) ước lượng bảng băm **lớn hơn**, nên planner **ít** hash hơn.
  - Có thêm tính năng tự chuyển `NOT IN` thành anti-join (commit `383eb21eb`), nhưng **chỉ khi** chứng minh được hai vế không NULL.
  - Vì vậy viết lại thành `NOT EXISTS` vẫn là cách an toàn nhất.
  - Trạng thái phát hành PG19 cần kiểm tra lại tại thời điểm đọc.

---

## 7. Nếu plan thật KHÁC CASE 4

| Dấu hiệu trong plan PG17 | Rule pgdoctor | Không sửa code | Sửa code |
|---|---|---|---|
| `Nested Loop` + inner `CTE Scan`/`Materialize`, `loops` lớn, outer ước lượng ≈ 1 | `PLAN-NL-RESCAN`, `PLAN-NL-BET`, `CMP-CTE-CORRELATION` | Hint table `NoNestLoop(...)` cho **mọi** tập alias chứa phía bị quét lại (tool sinh sẵn), hoặc `Set(enable_nestloop off)`, chỉ cho đúng câu này. Lab E9 (CASE 3): 100 s → 0.84 s / 1.0 s. Chỉ hint `HashJoin(cu a b)` là **không đủ** (vẫn 95 s), vì planner đổi thứ tự join | `NOT MATERIALIZED` / bảng tạm + `ANALYZE` |
| `Memoize` với Hits thấp / Evictions lớn | `PLAN-MEMOIZE`, `CMP-NEW-NODE` | `Set(enable_memoize off)` hoặc `ALTER ROLE … SET enable_memoize = off` (cũng là flag Cloud SQL) | — |
| SubPlan tương quan (subquery tham chiếu cột ngoài) | `PLAN-SUBPLAN-NOHASH` + remedy `subplan-correlated` | Index trên cột tương quan bên trong subquery | `NOT EXISTS` |
| Thân CTE chậm (`PLAN-CTE-COST` báo phần thân áp đảo) | `PLAN-CTE-COST`, `CMP-JOIN-METHOD`, `PLAN-MISEST` | So plan riêng của `SELECT` trong CTE ở hai bản; index cho join bên trong CTE; hint join cho alias bên trong CTE | Tách CTE, bảng tạm |
| `GROUP BY` đổi thứ tự cột | `CMP-GROUPKEY-ORDER` | `Set(enable_group_by_reordering off)` (không phải flag Cloud SQL, dùng hint hoặc ALTER ROLE) | — |
| Bảng đích là bảng **partitioned** | — | `ANALYZE <bảng cha>` (autovacuum không bao giờ analyze bảng cha); PG14 đổi cách plan UPDATE trên partition | — |

---

## 8. Runbook triển khai trên Cloud SQL

1. **Trên bản clone** (Cloud SQL clone của instance PG17, đúng flag và dữ liệu):
   - chạy `EXPLAIN` câu lệnh bằng role ứng dụng;
   - chạy `pgdoctor remedy`;
   - áp phương án 3.1 hoặc 3.2;
   - `EXPLAIN` lại, phải thấy `hashed`;
   - chạy thật câu lệnh trong `BEGIN … ROLLBACK` để đo thời gian.
2. **Chọn phương án:**
   - cần cứu ngay trong giờ làm việc thì dùng 3.2 (không restart);
   - đã có cửa sổ bảo trì thì bật pg_hint_plan và chuyển sang 3.1, rồi `RESET` cấu hình của 3.2 cho hẹp lại.
3. **Áp dụng trên production:**
   - recycle connection pool;
   - kiểm tra `pg_settings.source` trong session của ứng dụng (phương án 3.2), hoặc `EXPLAIN` có `hashed` (phương án 3.1).
4. **Theo dõi** lần chạy batch tiếp theo:
   - `pg_stat_activity` lúc đang chạy (query_id, wait_event);
   - `pg_stat_statements.mean_exec_time`;
   - bộ nhớ instance (Cloud Monitoring).
5. **Lên lịch sửa code** (Mục 6) rồi gỡ hint/role setting sau khi deploy.
6. **Quét toàn bộ workload:** chạy `pgdoctor analyze` với `pg_stat_statements` của hai bản (PG13 từ backup/clone, PG17 hiện tại) để tìm các câu khác cũng chậm đi.

---

## 9. Nguồn

**Google Cloud SQL** (truy cập 2026-10-31):
- [in-place major version upgrade](https://docs.cloud.google.com/sql/docs/postgres/upgrade-major-db-version-inplace): dùng `pg_upgrade`; ANALYZE là bước **thủ công**; rollback bằng cách restore backup trước upgrade sang instance mới.
- [extensions](https://docs.cloud.google.com/sql/docs/postgres/extensions): pg_hint_plan cho PG17 (`cloudsql.enable_pg_hint_plan`, cần restart); auto_explain.
- [flags](https://docs.cloud.google.com/sql/docs/postgres/flags): work_mem 4 MB; hash_mem_multiplier 1..1000, mặc định 2; `pg_hint_plan.enable_hint_table`; `compute_query_id` không phải flag.
- [features](https://docs.cloud.google.com/sql/docs/postgres/features): LLVM JIT không được hỗ trợ.

**PostgreSQL:**
- Cách planner/executor xử lý sublink (REL_17): [`prepjointree.c`](https://github.com/postgres/postgres/blob/REL_17_STABLE/src/backend/optimizer/prep/prepjointree.c) (`pull_up_sublinks_qual_recurse`: dưới `NOT` chỉ chuyển `EXISTS`), [`subselect.c`](https://github.com/postgres/postgres/blob/REL_17_STABLE/src/backend/optimizer/plan/subselect.c) (`convert_EXISTS_sublink_to_join`, `simplify_EXISTS_query`, `subplan_is_hashable`), [`nodeSubplan.c`](https://github.com/postgres/postgres/blob/REL_17_STABLE/src/backend/executor/nodeSubplan.c) (`ExecHashSubPlan` / `ExecScanSubPlan`, bảng băm không spill).
- `subplan_is_hashable()`: [REL_13](https://github.com/postgres/postgres/blob/REL_13_STABLE/src/backend/optimizer/plan/subselect.c), [REL_17](https://github.com/postgres/postgres/blob/REL_17_STABLE/src/backend/optimizer/plan/subselect.c), [PG19: 1ea5bdb00](https://github.com/postgres/postgres/commit/1ea5bdb00bfbc6f8034859cd19769346bf31dc53).
- [PG17 release notes](https://www.postgresql.org/docs/17/release-17.html) ("improve CTE plans by considering the statistics and sort order of columns…"); [commit f7816aec2](https://github.com/postgres/postgres/commit/f7816aec23eed1dc1da5f9a53cb6507d30b7f0a2).
- [ALTER ROLE](https://www.postgresql.org/docs/17/sql-alterrole.html), [subquery expressions (NOT IN và NULL)](https://www.postgresql.org/docs/17/functions-subquery.html), [wiki: Don't use NOT IN](https://wiki.postgresql.org/wiki/Don%27t_Do_This#Don.27t_use_NOT_IN).
- pgsql-performance [12.15 → 17.2: 250 ms → 377 s, sửa bằng work_mem](https://www.postgresql.org/message-id/43ed5a72fdfdd44fa046d8911c496364a3eb9231.camel@cybertec.at); pgsql-hackers [TPC-DS q95 chậm do f7816aec2](https://www.postgresql.org/message-id/4e14cc4f-edaf-0820-2bdf-db8dd0a9efab%40gmail.com); [Tom Lane về hashed subplan](https://postgresql.org/message-id/6343.1491401090%40sss.pgh.pa.us); [Memoize regression](https://www.postgresql.org/message-id/CAApHDvpBd%2BGb43wmZ94qnKLEEmT0h-S6No4mA2%2BYiqs-%3D0yQHQ%40mail.gmail.com).

**pg_hint_plan:**
- [hint table (PG17 branch)](https://github.com/ossc-db/pg_hint_plan/blob/PG17/docs/hint_table.md): "Hints in the hint table are prioritized over hints in comments"; `Set` hints change GUC parameters "just while planning".
- [v1.7.0 release](https://www.postgresql.org/about/news/pg_hint_plan-v170-released-2924/): hint table dùng query ID.

**Chưa kiểm chứng được nội dung:**
- Microsoft Tech Community, ["When PostgreSQL v17 Chooses the Wrong Plan: A Deep Dive into CTEs with Data Skew"](https://techcommunity.microsoft.com/blog/adforpostgresql/when-postgresql-v17-chooses-the-wrong-plan-a-deep-dive-into-ctes-with-data-skew/4515705). Trang render bằng JavaScript. Theo đoạn trích của công cụ tìm kiếm, bài mô tả đúng cơ chế PG17 lấy stats qua CTE với dữ liệu lệch. Các cách sửa được nêu là: `LIMIT` trong CTE, rewrite bằng LATERAL/`OFFSET 0`, và tắt hash join cho session.
