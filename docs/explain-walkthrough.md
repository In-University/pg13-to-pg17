# Giải thích EXPLAIN chi tiết từng case: PG13 vs PG17

> Tài liệu đi kèm [`pg13-vs-pg17-query-regression.md`](pg13-vs-pg17-query-regression.md) (Mục 7). Lab: [`../lab/README.md`](../lab/README.md).
> Plan **nguyên văn** nằm trong `../lab/results/`. Dưới đây plan được chép lại đầy đủ, chỉ bỏ dòng `SET/BEGIN/ROLLBACK` của psql, và **đánh số dòng `Lnn`** để tiện giải thích.
> Môi trường: Docker trên Windows (8 vCPU, 8 GB). PG 13.23 → `pg_upgrade` → PG 17.11. Cả hai bản dùng cùng tham số `shared_buffers=1GB, work_mem=64MB, random_page_cost=1.1, effective_cache_size=4GB`. `hash_mem_multiplier` để mặc định của từng bản: PG13 = **1.0**, PG17 = **2.0**. CASE 4 tự đặt `work_mem='4MB'`.
> Mọi plan chụp bằng `BEGIN; EXPLAIN (ANALYZE, BUFFERS[, SETTINGS]) …; ROLLBACK;`, nên không ghi dữ liệu.

**Cách đọc tài liệu.** Với mỗi case: (1) câu query và đặc điểm dữ liệu (đã đếm thật), (2) plan đầy đủ hai bản, (3) giải thích từng dòng, (4) **tái tạo con số `rows=` bằng phép tính** từ `pg_stats`, (5) vì sao planner chọn plan đó (so sánh cost, có thí nghiệm), (6) thời gian thực sự đi đâu, (7) cách sửa đã đo.

**Mức độ chắc chắn.** Mọi con số trong mục "tái tạo ước lượng" đều được đối chiếu với output thật: phép tính ra **đúng** con số planner in (sai lệch chỉ do làm tròn). Những chỗ chỉ là suy luận được ghi rõ là "ước tính".

---

## Mục lục
- [0. Kiến thức nền để đọc plan](#0-kiến-thức-nền-để-đọc-plan)
- [1. ★ CASE 4 – `UPDATE … NOT IN (SELECT … FROM CTE)` – mất `hashed SubPlan`](#1--case-4--update--not-in-select--from-cte--mất-hashed-subplan)
- [2. CASE 3 – lọc trên cột của CTE, dữ liệu tương quan](#2-case-3--lọc-trên-cột-của-cte-dữ-liệu-tương-quan)
- [3. CASE 2 – PG17 ngay sau `pg_upgrade` (chưa ANALYZE)](#3-case-2--pg17-ngay-sau-pg_upgrade-chưa-analyze)
- [4. CASE 1 – đối chứng: cùng plan, ước lượng khác](#4-case-1--đối-chứng-cùng-plan-ước-lượng-khác)
- [5. Bảng tra nhanh và quy trình áp dụng cho câu thật](#5-bảng-tra-nhanh-và-quy-trình-áp-dụng-cho-câu-thật)
- [Phụ lục A. Hằng số và công thức của planner dùng trong tài liệu](#phụ-lục-a-hằng-số-và-công-thức-của-planner-dùng-trong-tài-liệu)

---

## 0. Kiến thức nền để đọc plan

### 0.1 Một dòng node

```
->  CTE Scan on current_brand_code cbc  (cost=0.00..21310.72 rows=4736 width=32) (actual time=5.041..854.093 rows=400000 loops=1)
      Filter: (region = 'VN'::text)
      Rows Removed by Filter: 600000
```

| Phần | Ý nghĩa | Ghi chú khi đọc |
|---|---|---|
| `CTE Scan on current_brand_code cbc` | Loại node, đối tượng đọc và alias | CTE Scan đọc kết quả CTE đã materialize (tuplestore), **không có index** |
| `cost=0.00..21310.72` | Cost **ước lượng**: startup..total | Đơn vị tương đối. `seq_page_cost` = 1 là mốc. Planner chọn plan có **total cost** nhỏ nhất |
| `rows=4736` | Số dòng **ước lượng** mỗi lần chạy node | Mọi quyết định (join method, thứ tự join, có hash hay không) dựa trên số này |
| `width=32` | Độ rộng trung bình mỗi dòng (byte) | Dùng để tính kích thước hash, sort, materialize |
| `actual time=5.041..854.093` | Thời gian thật (ms) tới dòng đầu..dòng cuối, **mỗi lần chạy** | Là thời gian gộp (gồm node con) |
| `rows=400000` | Số dòng **thật**, trung bình mỗi lần chạy | So với `rows=` ước lượng: lệch 10× trở lên là đáng ngờ |
| `loops=1` | Số lần node được chạy | Tổng thời gian ≈ time × loops. Tổng dòng ≈ rows × loops |

**Quy tắc đọc:** đi từ node lá lên trên, so `rows=` (đoán) với `actual rows` (thật). Node **đầu tiên** lệch lớn là nơi planner bắt đầu "mù". Sau đó tìm node có `loops` lớn hoặc `Rows Removed by Join Filter` khổng lồ, vì đó là nơi tốn thời gian.

### 0.2 Các dòng phụ dưới node

| Dòng | Ý nghĩa |
|---|---|
| `Filter: …` / `Rows Removed by Filter: N` | Điều kiện áp lên từng dòng sau khi đọc, và số dòng bị loại (trung bình mỗi loop) |
| `Join Filter: …` / `Rows Removed by Join Filter: N` | Điều kiện join kiểm tra trên **từng cặp** (Nested Loop). N khổng lồ là dấu hiệu O(N×M) |
| `Index Cond`, `Hash Cond`, `Merge Cond` | Điều kiện dùng index / khóa hash / khóa merge |
| `Buckets: 32768  Batches: 1  Memory Usage: 999kB` | Bảng băm: số bucket, số batch (>1 nghĩa là tràn đĩa), bộ nhớ dùng. `(originally 1024)` nghĩa là bảng băm phải nới lúc chạy vì ước lượng quá nhỏ |
| `Sort Method: quicksort  Memory: 1166kB` | Sort trong RAM. `external merge  Disk: …` nghĩa là tràn đĩa |
| `Buffers: shared hit=a read=b dirtied=c written=d, temp read=e written=f` | Đơn vị **block 8 KB**. `hit` = trong shared_buffers, `read` = đọc từ OS/disk, `temp` = file tạm (sort/hash/CTE tràn `work_mem`) |
| `I/O Timings: read=… temp read=…` | Thời gian chờ I/O (cần `track_io_timing=on`) |
| `Planning Time` / `Execution Time` | Thời gian lập plan / thực thi (ms) |
| `JIT: Functions … Options: Inlining …, Optimization … Timing: … Total` | JIT compile. Inlining/Optimization chỉ bật khi total cost > `jit_inline_above_cost` / `jit_optimize_above_cost` (mặc định 500 000) |

> `EXPLAIN ANALYZE` có đo thời gian từng node nên **chậm hơn** chạy thật. Với query xử lý hàng chục triệu dòng, overhead có thể gấp đôi (CASE 2: 12.2 s khi có timing so với 6.0 s với `TIMING OFF`). Hãy so **tỉ lệ** giữa hai bản, đừng so mili-giây tuyệt đối.

### 0.3 Planner ước lượng số dòng như thế nào

- **Scan có điều kiện:** `rows = reltuples × selectivity`.
  - Điều kiện `=`: lấy tần suất từ MCV (`most_common_vals/freqs`), nếu không có thì dùng `(1 − tổng tần suất MCV) / (n_distinct − số MCV)`.
  - Điều kiện `<`, `>=`: tính từ histogram.
  - Nhiều điều kiện: **nhân** selectivity với nhau (giả định độc lập).
- **Khi không có stats:** dùng hằng mặc định.

  | Hằng | Giá trị | Dùng cho |
  |---|---|---|
  | `DEFAULT_EQ_SEL` | 0.005 | Điều kiện `=` |
  | `DEFAULT_INEQ_SEL` | 1/3 | Một vế `<`, `>=` |
  | `DEFAULT_RANGE_INEQ_SEL` | 0.005 | `BETWEEN` / cặp `>= … <=` |
  | `DEFAULT_MATCH_SEL` | 0.005 | `LIKE 'xx%'` |
  | `DEFAULT_NUM_DISTINCT` | 200 | Số giá trị khác nhau của một cột |

- **Join `a.x = b.y` (không có MCV khớp):** `selectivity = 1 / max(nd(a.x), nd(b.y))`. Số nhóm của `GROUP BY x` ≈ `nd(x)`.
- **`n_distinct` âm** nghĩa là *tỉ lệ*: −0.17 tức là số giá trị khác nhau bằng 17% số dòng.
- **Bảng chưa ANALYZE (`reltuples = -1`, PG14+):** số dòng = số block thật × mật độ ước lượng.
  - Mật độ = `(8192 − 24) / (width + 24 + 4)`, lấy phần nguyên.
  - Trong đó 24 là header của tuple, 4 là line pointer.
  - `width` lấy theo kiểu dữ liệu: `int4` = 4, `text` không có stats = 32…

### 0.4 CTE: điểm khác nhau cốt lõi giữa PG13 và PG17

- **PG13:** cột của `CTE Scan` (CTE materialized) **không có thống kê**. Mọi điều kiện hay join trên cột CTE đều dùng hằng mặc định (0.005, 1/3, 200…).
- **PG17 (commit `f7816aec2`):** nếu cột output của CTE là một cột "trơn" của bảng gốc (một Var), planner **mượn `pg_stats` của cột đó ở bảng gốc**.
  - Đây là stats của **toàn bảng**. Planner **không** biết CTE đã lọc gì bên trong.
  - Các giá trị kiểu *tỉ lệ* (`n_distinct` âm, tần suất MCV) bị áp lên số dòng của CTE.
  - Extended statistics **không** được mượn: đã thử ở CASE 3, ước lượng vẫn 1 dòng.
- Kết quả: tuỳ dữ liệu, ước lượng PG17 có thể **đúng hơn** (CASE 4) hoặc **sai hơn** (CASE 3) so với mặc định của PG13. Hai hướng đều có thể đổi plan.

### 0.5 Khi nào `NOT IN (subquery)` được hash

Planner chỉ dùng `hashed SubPlan` khi bảng băm **ước lượng** vừa bộ nhớ:

```
rows_ước_lượng_của_subquery × (MAXALIGN(width) + 24)  ≤  work_mem × hash_mem_multiplier
```

Công thức đã được kiểm chứng bằng thí nghiệm ở mục 1.6: ngưỡng bật/tắt rơi đúng giữa `work_mem` = 10MB và 11MB.

Nếu không hash được, `NOT IN` không có phương án nào khác: mỗi dòng ngoài **quét lại toàn bộ** kết quả subquery (O(N×M)). `NOT IN` không bao giờ được planner đổi thành anti-join vì ngữ nghĩa NULL.

---

## 1. ★ CASE 4 – `UPDATE … NOT IN (SELECT … FROM CTE)` – mất `hashed SubPlan`

**Kết quả:** PG13 **0.95 s**, PG17 **257 s** khi có timing (289 s với `TIMING OFF`), tức **≈ 270×**. Cả hai bản **đã VACUUM ANALYZE**. Đây là mẫu câu gần nhất với câu thật của dự án.

### 1.1 Câu query (`lab/sql/case4_update.sql`)
```sql
SET work_mem = '4MB';                                  -- mặc định PostgreSQL / nhiều tier Cloud SQL
WITH a AS NOT MATERIALIZED (                           -- inline vào chỗ dùng
  SELECT s.id, s.region FROM stores s WHERE s.active
), current_brand_code AS MATERIALIZED (                -- tính 1 lần, lưu vào tuplestore (không index)
  SELECT b.code AS brand_code, a.region                -- a.region chính là stores.region (một Var)
  FROM brand_assignments ba
  JOIN brands b ON b.id = ba.brand_id
  JOIN a        ON a.id = ba.store_id
  WHERE ba.is_current
)
UPDATE products p
SET status = 'ORPHAN', updated_at = now()
WHERE p.status = 'PENDING'
  AND p.brand_code NOT IN (
        SELECT cbc.brand_code FROM current_brand_code cbc
        WHERE cbc.region = 'VN'                        -- lọc trên cột của CTE
      );
```

### 1.2 Dữ liệu (đếm thật) và thống kê

| Bảng | Dòng | Đặc điểm | `pg_stats` (PG17) |
|---|---|---|---|
| `stores` | 20 000 | 95% `active`. `region`: VN 40%, ID 40%, TH 20% | `region` MCV `{ID,VN,TH}` = `{0.4,0.4,0.2}`. `active` `{t,f}` = `{0.95,0.05}` |
| `brands` | 5 000 | `code` duy nhất | `n_distinct = -1` |
| `brand_assignments` | 2 000 000 | 50% `is_current` | `is_current` `{t,f}` = `{0.5002,0.4998}` |
| `products` | 200 000 | 5 000 `PENDING` (2.5%). 2 500 trong số đó mang mã `LEGACY-…` không có trong danh sách | `status` `{ACTIVE,PENDING}` = `{0.9749,0.0251}` |

Kết quả CTE thật: 1 000 000 dòng, trong đó 400 000 dòng `region = 'VN'`.

### 1.3 Plan PG13 – 0.95 s (`lab/results/case4_pg13.txt`)
```
L01  Update on products p  (cost=59856.49..60172.63 rows=2551 width=57) (actual time=948.590..948.597 rows=0 loops=1)
L02    Buffers: shared hit=36881 dirtied=21 written=21, temp written=2536
L03    CTE current_brand_code
L04      ->  Hash Join  (cost=667.00..38533.63 rows=947143 width=10) (actual time=5.034..542.755 rows=1000000 loops=1)
L05            Hash Cond: (ba.brand_id = b.id)
L06            Buffers: shared hit=12859
L07            ->  Hash Join  (cost=526.50..35899.81 rows=949038 width=7) (actual time=3.939..384.578 rows=1000000 loops=1)
L08                  Hash Cond: (ba.store_id = s.id)
L09                  Buffers: shared hit=12828
L10                  ->  Seq Scan on brand_assignments ba  (cost=0.00..32739.00 rows=1003333 width=8) (actual time=0.003..158.041 rows=1000000 loops=1)
L11                        Filter: is_current
L12                        Rows Removed by Filter: 1000000
L13                        Buffers: shared hit=12739
L14                  ->  Hash  (cost=289.00..289.00 rows=19000 width=7) (actual time=3.833..3.834 rows=19000 loops=1)
L15                        Buckets: 32768  Batches: 1  Memory Usage: 999kB
L16                        Buffers: shared hit=89
L17                        ->  Seq Scan on stores s  (cost=0.00..289.00 rows=19000 width=7) (actual time=0.005..1.653 rows=19000 loops=1)
L18                              Filter: active
L19                              Rows Removed by Filter: 1000
L20                              Buffers: shared hit=89
L21            ->  Hash  (cost=78.00..78.00 rows=5000 width=11) (actual time=1.063..1.063 rows=5000 loops=1)
L22                  Buckets: 8192  Batches: 1  Memory Usage: 279kB
L23                  Buffers: shared hit=28
L24                  ->  Seq Scan on brands b  (cost=0.00..78.00 rows=5000 width=11) (actual time=0.005..0.377 rows=5000 loops=1)
L25                        Buffers: shared hit=28
L26    ->  Index Scan using products_status_idx on products p  (cost=21322.85..21639.00 rows=2551 width=57) (actual time=923.060..931.367 rows=2500 loops=1)
L27          Index Cond: (status = 'PENDING'::text)
L28          Filter: (NOT (hashed SubPlan 2))
L29          Rows Removed by Filter: 2500
L30          Buffers: shared hit=14343, temp written=2536
L31          SubPlan 2
L32            ->  CTE Scan on current_brand_code cbc  (cost=0.00..21310.72 rows=4736 width=32) (actual time=5.041..854.093 rows=400000 loops=1)
L33                  Filter: (region = 'VN'::text)
L34                  Rows Removed by Filter: 600000
L35                  Buffers: shared hit=12859, temp written=2536
L36  Planning:
L37    Buffers: shared hit=260
L38  Planning Time: 0.745 ms
L39  Execution Time: 951.028 ms
```

| Dòng | Giải thích |
|---|---|
| L03 | `CTE current_brand_code` là **InitPlan**. Nó chỉ được tính khi có node đọc nó lần đầu (ở đây là L32). Kết quả lưu trong tuplestore |
| L10–L13 | Đọc toàn bộ 2M dòng `brand_assignments` (12 739 block, đều trúng cache), giữ 1M dòng `is_current` |
| L14–L20 | Build bảng băm 19 000 store active: 32 768 bucket, 1 batch, 999 kB, không tràn |
| L07–L09 | Hash Join assignment × store: 1M dòng ra trong 385 ms |
| L21–L25 | Bảng băm 5 000 brand, 279 kB |
| L04 | Join thêm brand: CTE có **1 000 000 dòng** (ước lượng 947 143: chính xác). Mất 543 ms |
| L35, L30 | `temp written=2536`: kết quả CTE ≈ 2 536 × 8 KB ≈ **20 MB** lớn hơn `work_mem` 4 MB, nên tuplestore tràn ra file tạm. Ghi **một lần** |
| L32–L34 | Subquery của `NOT IN`: đọc CTE, giữ 400 000 dòng VN. **`loops=1`**: subquery chạy **một lần duy nhất**. Thời gian 854 ms bao gồm cả 543 ms tính CTE |
| L28 | **`NOT (hashed SubPlan 2)`**: 400k mã brand được đưa vào bảng băm. Mỗi sản phẩm tra O(1) |
| L26–L27 | Lấy 5 000 sản phẩm `PENDING` qua index. Startup 923 ms chính là thời gian dựng bảng băm của SubPlan trước khi trả dòng đầu tiên. Phần quét index và tra băm chỉ mất ~8 ms (931 − 923) |
| L29 | 2 500 sản phẩm có mã nằm trong danh sách bị loại. 2 500 sản phẩm `LEGACY-…` còn lại được UPDATE |
| L01 | `rows=0` thật vì UPDATE không có RETURNING. `rows=2551` là số dòng planner dự kiến *sửa* |

### 1.4 Tái tạo ước lượng của PG13

| Node | `rows=` | Phép tính | Kết quả |
|---|---|---|---|
| L10 `is_current` | 1 003 333 | 2 000 000 × 0.50166667 (MCV `t` lúc ANALYZE ở PG13) | 1 003 333 ✓ |
| L07 join store | 949 038 | 1 003 333 × (19 000 store active / 20 000 store) | 953 166 (≈ 949 038; planner dùng thêm n_distinct của `ba.store_id`) |
| L04 join brand | 947 143 | ≈ 949 038 × 5 000 / nd(`ba.brand_id`) | ≈ ✓ |
| **L32 `region = 'VN'`** | **4 736** | Cột CTE **không có stats** ⇒ `DEFAULT_EQ_SEL` = 0.005. **947 143 × 0.005** | **4 735.7 ⇒ 4 736 ✓** |
| L26 `PENDING` + NOT IN | 2 551 | ước lượng index ~5 102 dòng (MCV `PENDING` 0.0245 × reltuples, được co giãn theo kích thước file hiện tại) × 0.5 (selectivity mặc định của `NOT IN (subquery)`) | 2 551 ✓ |

**Quyết định hash:** 4 736 × (32 + 24) = **265 kB** ≤ `work_mem × hash_mem_multiplier` = 4 MB × 1.0 = 4 MB ⇒ **hash**. Lúc chạy, bảng băm thật chứa 400k mã (khoảng 20 MB). PostgreSQL **không** huỷ phương án đã chọn khi bảng băm SubPlan lớn hơn ước lượng, nên vẫn nhanh.

> **Bản chất:** PG13 đoán sai 85× (4 736 so với 400 000), nhưng sai theo hướng **có lợi**.

### 1.5 Plan PG17 – 257 s (`lab/results/case4_pg17.txt`)
```
L01  Update on products p  (cost=38574.76..55951791.09 rows=0 width=0) (actual time=257005.662..257005.675 rows=0 loops=1)
L02    Buffers: shared hit=39425 dirtied=1934 written=2, temp read=6348856 written=2537
L03    I/O Timings: shared write=0.120, temp read=8624.995 write=66.617
L04    CTE current_brand_code
L05      ->  Hash Join  (cost=667.00..38574.47 rows=949241 width=10) (actual time=7.226..410.082 rows=1000000 loops=1)
L06            Hash Cond: (ba.brand_id = b.id)
L07            ->  Hash Join  (cost=526.50..35937.11 rows=950380 width=7) (actual time=5.719..287.396 rows=1000000 loops=1)
L08                  Hash Cond: (ba.store_id = s.id)
L09                  ->  Seq Scan on brand_assignments ba  (cost=0.00..32784.00 rows=1000400 width=8) (actual time=0.031..100.479 rows=1000000 loops=1)
L10                        Filter: is_current
L11                        Rows Removed by Filter: 1000000
L12                  ->  Hash  (cost=289.00..289.00 rows=19000 width=7) (actual time=5.557..5.558 rows=19000 loops=1)
L13                        Buckets: 32768  Batches: 1  Memory Usage: 999kB
L14                        ->  Seq Scan on stores s  (cost=0.00..289.00 rows=19000 width=7) (actual time=0.022..1.970 rows=19000 loops=1)
L15                              Filter: active
L16                              Rows Removed by Filter: 1000
L17            ->  Hash  (cost=78.00..78.00 rows=5000 width=11) (actual time=1.476..1.476 rows=5000 loops=1)
L18                  Buckets: 8192  Batches: 1  Memory Usage: 279kB
L19                  ->  Seq Scan on brands b  (cost=0.00..78.00 rows=5000 width=11) (actual time=0.045..0.534 rows=5000 loops=1)
L20    ->  Index Scan using products_status_idx on products p  (cost=0.29..55913216.62 rows=2507 width=46) (actual time=963.262..256763.926 rows=2500 loops=1)
L21          Index Cond: (status = 'PENDING'::text)
L22          Filter: (NOT (ANY (brand_code = (SubPlan 2).col1)))
L23          Rows Removed by Filter: 2500
L24          Buffers: shared hit=14385 dirtied=1477, temp read=6348856 written=2537
L25          I/O Timings: temp read=8624.995 write=66.617
L26          SubPlan 2
L27            ->  CTE Scan on current_brand_code cbc  (cost=0.00..21357.92 rows=379696 width=32) (actual time=0.004..41.935 rows=200252 loops=5000)
L28                  Filter: (region = 'VN'::text)
L29                  Rows Removed by Filter: 300378
L30                  Buffers: shared hit=12901, temp read=6348856 written=2537
L31                  I/O Timings: temp read=8624.995 write=66.617
L32  Planning Time: 0.907 ms
L33  JIT:
L34    Functions: 35
L35    Options: Inlining true, Optimization true, Expressions true, Deforming true
L36    Timing: Generation 0.871 ms (Deform 0.404 ms), Inlining 75.950 ms, Optimization 91.316 ms, Emission 79.621 ms, Total 247.758 ms
L37  Execution Time: 257021.845 ms
```
*(Đã bỏ các dòng `Buffers` của node con trong CTE để gọn. Bản đầy đủ nằm trong file kết quả.)*

| Dòng | Giải thích |
|---|---|
| L04–L19 | CTE **giống hệt PG13**: 1M dòng, 410 ms. Phần "join nhiều bảng" **không** phải nguyên nhân |
| **L22** | `NOT (ANY (brand_code = (SubPlan 2).col1))`: **không có chữ `hashed`**. Cú pháp hiển thị của PG17 khác PG13 (`ANY (… = (SubPlan N).col1)`), nhưng điểm mấu chốt là thiếu chữ `hashed` |
| **L27** | **`loops=5000`**: subquery **chạy lại cho từng sản phẩm** PENDING. Mỗi lần trung bình đọc 200 252 + 300 378 = **500 630** dòng CTE, mất 41.9 ms |
| L27 `rows=200252` | Sản phẩm có mã trong danh sách thì dừng khi gặp mã khớp (đọc trung bình một phần). Sản phẩm `LEGACY-…` phải đọc **hết** 1M dòng mới kết luận được "không có" |
| L24, L30 | `temp read=6348856`: CTE nằm trong file tạm 2 537 block (≈ 20 MB). 6 348 856 / 5 000 = **1 270 block mỗi lần**, đúng **≈ 50%** của 2 537: trung bình mỗi lần quét đọc nửa file. Tổng **≈ 48.4 GB** đọc từ file tạm. Ở lab phần lớn trúng OS cache (`temp read=8.6 s`); trên disk Cloud SQL phần này còn nặng hơn |
| L20 | `cost=0.29..55913216.62`: chính planner cũng **biết** plan này đắt (55.9 triệu so với ~60 nghìn ở PG13), nhưng **không còn lựa chọn** (xem 1.6). `actual time=963..256763`: dòng đầu ra sau 1 s, dòng cuối sau 257 s |
| L35–L36 | Cost > 500 000 nên JIT bật cả inlining và optimization: tốn 248 ms, không đáng kể so với 257 s |

### 1.6 Tái tạo ước lượng của PG17 và quyết định "không hash"

| Node | `rows=` | Phép tính (stats PG17) | Kết quả |
|---|---|---|---|
| L09 `is_current` | 1 000 400 | 2 000 000 × 0.5002 | 1 000 400 ✓ |
| L07 join store | 950 380 | 1 000 400 × 0.95 (19 000/20 000 store active) | 950 380 ✓ |
| L05 join brand | 949 241 | ≈ 950 380 × 5 000 / nd(`ba.brand_id`) | ≈ ✓ |
| **L27 `region = 'VN'`** | **379 696** | PG17 mượn MCV của **`stores.region`** qua CTE (VN = 0.4). **949 241 × 0.4** | **379 696.4 ⇒ 379 696 ✓** |
| L20 `PENDING` + NOT IN | 2 507 | 5 013 dòng index (200 000 × 0.0251) × 0.5 | 2 506.5 ⇒ 2 507 ✓ |

**Quyết định hash:** 379 696 × (32 + 24) = **21 262 976 byte ≈ 20.3 MiB** > 4 MB × 2.0 = **8 MiB** ⇒ **không được hash**.

**Thí nghiệm kiểm chứng ngưỡng** (chỉ `EXPLAIN`, đổi `work_mem`):

| `work_mem` | Ngưỡng (×2.0) | PG17 chọn | Total cost |
|---|---|---|---|
| 4MB | 8 MiB | `SubPlan` (không hash) | 55 951 791 |
| 10MB | 20 MiB < 20.3 MiB | `SubPlan` (không hash) | 55 951 791 |
| **11MB** | **22 MiB > 20.3 MiB** | **`hashed SubPlan`** | **61 195** |
| 16MB | 32 MiB | `hashed SubPlan` | 61 195 |

Ngưỡng rơi **đúng** giữa 10MB và 11MB, khớp với công thức ở 0.5. Plan hash rẻ hơn **900 lần**, nhưng ở `work_mem` = 4MB planner bị **cấm** chọn nó.

Cost 55.9M đến từ đâu (ước tính): 5 013 dòng × ~11 150 mỗi dòng. 11 150 ≈ 52% cost của một lần quét CTE (21 358), tức planner giả định mỗi dòng ngoài phải đọc khoảng **nửa** danh sách.

### 1.7 Thời gian 257 s đi đâu

| Thành phần | Tính toán | Thời gian |
|---|---|---|
| Tính CTE (một lần) | L05 | 0.41 s |
| Quét lại CTE 5 000 lần | 5 000 × 41.9 ms (L27) | **≈ 209.7 s** |
| So sánh `brand_code = col1` trên từng dòng trả về | 5 000 × 200 252 ≈ **1.0 tỉ** phép so sánh, nằm trong thời gian riêng của L20: 256.8 − 209.7 | **≈ 47 s** |
| JIT | L36 | 0.25 s |
| **Tổng** | ≈ 2.5 tỉ dòng được đọc hoặc lọc (5 000 × 500 630) | **≈ 257 s** |

### 1.8 So sánh trực tiếp

| | PG13 | PG17 |
|---|---|---|
| Ước lượng subquery `NOT IN` | 4 736 (mặc định 0.5%) | 379 696 (MCV `stores.region` = 0.4) |
| Thực tế | 400 000 | 400 000 |
| Kích thước băm ước lượng | 265 kB | 20.3 MiB |
| Ngưỡng `work_mem × hash_mem_multiplier` | 4 MB × **1.0** | 4 MB × **2.0** = 8 MiB |
| Kiểu subplan | **`hashed SubPlan`**, `loops=1` | **`SubPlan`**, `loops=5000` |
| Temp I/O | ghi 2 536 block, đọc 1 lần | đọc **6.3 triệu block (≈ 48 GB)** |
| Total cost | 60 172 | 55 951 791 |
| Thời gian | 0.95 s | 257 s |

Lưu ý: PG17 có `hash_mem_multiplier` = 2 (PG13 = 1) nên ngưỡng **rộng gấp đôi**, vậy mà vẫn không đủ. Nguyên nhân là **ước lượng tăng 80 lần**, không phải do cấu hình bộ nhớ.

### 1.9 Kiểm chứng nguyên nhân (mỗi lần chỉ đổi một yếu tố)

| Biến thể | PG13 | PG17 | Kết luận |
|---|---|---|---|
| Nguyên bản | `hashed SubPlan` | `SubPlan` | Regression |
| `current_brand_code AS NOT MATERIALIZED` (hai bản cùng thấy stats thật) | **`SubPlan`** | `SubPlan` | Khi PG13 cũng biết sự thật, **PG13 cũng chọn plan xấu**: nguyên nhân là *thông tin planner thấy* |
| `SET work_mem = '11MB'` trở lên | `hashed` | `hashed` (cost 61 195) | Ngưỡng bộ nhớ là "công tắc" |
| `NOT EXISTS (SELECT 1 … WHERE cbc.brand_code = p.brand_code)` | `Merge Anti Join` (cost 60 855) | `Hash Anti Join` (cost 75 462) | Anti-join **không phụ thuộc ngưỡng**: cách sửa bền vững |

> ⚠️ `NOT IN` và `NOT EXISTS` khác nhau khi subquery có `NULL`: `x NOT IN (…, NULL)` không bao giờ đúng. Nếu `brand_code` có thể NULL, thêm `AND cbc.brand_code IS NOT NULL` hoặc xác nhận nghiệp vụ trước khi đổi.

**pgdoctor báo:** `CMP-SUBPLAN-HASH-LOST` (critical), `PLAN-SUBPLAN-NOHASH` (critical, "duyệt lại 5000 lần, 82% thời gian"), `PLAN-TEMP-IO` (≈ 48.4 GB), `PLAN-CTE-RESCAN`. Chỉ cần `EXPLAIN` không ANALYZE của PG17, tool vẫn báo `CMP-SUBPLAN-HASH-LOST` kèm ước lượng ~476 triệu phép so sánh.

---

## 2. CASE 3 – lọc trên cột của CTE, dữ liệu tương quan

**Kết quả:** PG13 **0.57 s**, PG17 **20.6 s** (với `TIMING OFF`: 0.23 s và 16.7 s), tức **≈ 36–72×**. Cả hai **đã ANALYZE**.

### 2.1 Câu query (`lab/sql/case3_update.sql`)
```sql
WITH open_orders AS MATERIALIZED (
  SELECT id, customer_id, cust_code, amount, priority, created_at
  FROM orders2 WHERE status = 'OPEN'
)
UPDATE customer_stats2 cs
SET pair_cnt = s.n, open_amount = s.total, updated_at = now()
FROM (
  SELECT a.customer_id, count(*) AS n, sum(a.amount) AS total
  FROM open_orders a
  JOIN open_orders b ON b.customer_id = a.customer_id AND b.id <> a.id     -- CTE dùng 2 lần ⇒ bắt buộc materialize
  JOIN customers2 cu ON cu.name = a.cust_code
  WHERE a.priority >= 8 AND a.created_at >= timestamp '2026-10-01' AND a.amount >= 900   -- lọc trên cột CTE
    AND b.priority >= 8 AND b.created_at >= timestamp '2026-10-01' AND b.amount >= 900
  GROUP BY a.customer_id
) s
WHERE cs.customer_id = s.customer_id;
```

### 2.2 Dữ liệu (đếm thật trên `orders2`, 2 000 000 dòng)

| Tập | Số dòng | Tỉ lệ toàn bảng |
|---|---|---|
| `status = 'OPEN'` | 10 000 | 0.5% |
| `priority >= 8` | 15 000 | 0.75% |
| `created_at >= '2026-10-01'` | 10 000 | 0.5% |
| `amount >= 900` | 209 000 | 10.45% |
| **cả 3 điều kiện** | **10 000** | 0.5%, và đó **chính là** 10 000 đơn OPEN |

**Tương quan 100%:** mọi đơn OPEN đều mới, ưu tiên cao, giá trị lớn. 10 000 đơn OPEN thuộc 500 khách. Mỗi đơn có 19 đơn "cặp" cùng khách, nên self-join cho 190 000 cặp.

### 2.3 Plan PG13 – 0.57 s (`lab/results/case3_pg13.txt`)
```
L01  Update on customer_stats2 cs  (cost=15484.17..15943.26 rows=170 width=126) (actual time=565.604..565.610 rows=0 loops=1)
L02    CTE open_orders
L03      ->  Index Scan using orders2_status_idx on orders2  (cost=0.43..467.07 rows=10067 width=39) (actual time=0.088..195.823 rows=10000 loops=1)
L04            Index Cond: (status = 'OPEN'::text)
L05    ->  Nested Loop  (cost=15017.10..15476.19 rows=170 width=126) (actual time=506.743..562.111 rows=500 loops=1)
L06          ->  Subquery Scan on s  (cost=15016.68..15039.74 rows=170 width=112) (actual time=506.497..560.496 rows=500 loops=1)
L07                ->  GroupAggregate  (cost=15016.68..15038.04 rows=170 width=44) (actual time=506.455..560.303 rows=500 loops=1)
L08                      Group Key: a.customer_id
L09                      ->  Merge Join  (cost=15016.68..15030.72 rows=692 width=20) (actual time=506.346..540.078 rows=190000 loops=1)
L10                            Merge Cond: (a.customer_id = b.customer_id)
L11                            Join Filter: (b.id <> a.id)
L12                            Rows Removed by Join Filter: 10000
L13                            ->  Sort  (cost=14723.90..14724.84 rows=373 width=28) (actual time=501.241..501.794 rows=10000 loops=1)
L14                                  Sort Key: a.customer_id
L15                                  Sort Method: quicksort  Memory: 1166kB
L16                                  ->  Hash Join  (cost=14426.00..14707.97 rows=373 width=28) (actual time=270.998..493.761 rows=10000 loops=1)
L17                                        Hash Cond: (a.cust_code = cu.name)
L18                                        ->  CTE Scan on open_orders a  (cost=0.00..276.84 rows=373 width=60) (actual time=0.094..211.235 rows=10000 loops=1)
L19                                              Filter: ((priority >= 8) AND (created_at >= '2026-10-01 00:00:00'::timestamp without time zone) AND (amount >= '900'::numeric))
L20                                        ->  Hash  (cost=8176.00..8176.00 rows=500000 width=11) (actual time=269.365..269.366 rows=500000 loops=1)
L21                                              Buckets: 524288  Batches: 1  Memory Usage: 25472kB
L22                                              ->  Seq Scan on customers2 cu  (cost=0.00..8176.00 rows=500000 width=11) (actual time=0.013..119.989 rows=500000 loops=1)
L23                            ->  Sort  (cost=292.78..293.71 rows=373 width=12) (actual time=5.096..12.180 rows=199981 loops=1)
L24                                  Sort Key: b.customer_id
L25                                  Sort Method: quicksort  Memory: 853kB
L26                                  ->  CTE Scan on open_orders b  (cost=0.00..276.84 rows=373 width=12) (actual time=0.004..1.703 rows=10000 loops=1)
L27                                        Filter: ((priority >= 8) AND (created_at >= '2026-10-01 00:00:00'::timestamp without time zone) AND (amount >= '900'::numeric))
L28          ->  Index Scan using customer_stats2_pkey on customer_stats2 cs  (cost=0.42..2.56 rows=1 width=10) (actual time=0.002..0.002 rows=1 loops=500)
L29                Index Cond: (customer_id = s.customer_id)
L30  Execution Time: 566.402 ms
```
*(Đã bỏ các dòng `Buffers`.)*

| Dòng | Giải thích |
|---|---|
| L03 | CTE: 10 000 đơn OPEN qua index (195 ms, cache lạnh) |
| L18–L19 | `CTE Scan a` với 3 điều kiện: ước lượng **373**, thực tế 10 000 (sai 27×, nhưng vẫn ở mức "hàng trăm") |
| L20–L22 | Planner build bảng băm trên **customers2** (500k dòng, 25 MB, 524 288 bucket), vì tin phía CTE chỉ có 373 dòng nên dùng nó làm phía probe |
| L09–L12 | **Merge Join**: sort hai phía rồi trộn. Mỗi phía **chỉ đọc một lần** (`loops=1`). 190 000 cặp + 10 000 cặp bị loại vì `b.id = a.id` (tự ghép với chính mình) |
| L23 `rows=199981` | Sort phía inner trả **199 981** dòng dù CTE chỉ có 10 000: Merge Join **đọc lại** các nhóm khóa trùng (mark/restore), nên con số này không phải lỗi ước lượng |
| L07 | GroupAggregate: 500 khách |
| L28 | 500 lần index lookup vào bảng đích: rất rẻ |

### 2.4 Plan PG17 – 20.6 s (`lab/results/case3_pg17.txt`)
```
L01  Update on customer_stats2 cs  (cost=11089.81..11092.07 rows=0 width=0) (actual time=20625.267..20625.281 rows=0 loops=1)
L02    CTE open_orders
L03      ->  Index Scan using orders2_status_idx on orders2  (cost=0.43..436.02 rows=9933 width=39) (actual time=0.033..12.433 rows=10000 loops=1)
L04            Index Cond: (status = 'OPEN'::text)
L05    ->  Nested Loop  (cost=10653.78..10656.04 rows=1 width=122) (actual time=20585.986..20621.779 rows=500 loops=1)
L06          ->  Subquery Scan on s  (cost=10653.36..10653.40 rows=1 width=112) (actual time=20585.917..20620.515 rows=500 loops=1)
L07                ->  GroupAggregate  (cost=10653.36..10653.39 rows=1 width=44) (actual time=20585.808..20620.247 rows=500 loops=1)
L08                      Group Key: a.customer_id
L09                      ->  Sort  (cost=10653.36..10653.37 rows=1 width=20) (actual time=20585.729..20593.735 rows=190000 loops=1)
L10                            Sort Key: a.customer_id
L11                            Sort Method: quicksort  Memory: 12082kB
L12                            ->  Nested Loop  (cost=273.17..10653.35 rows=1 width=20) (actual time=19.014..20438.410 rows=190000 loops=1)
L13                                  Join Filter: ((b.id <> a.id) AND (a.customer_id = b.customer_id))
L14                                  Rows Removed by Join Filter: 99810000
L15                                  ->  Hash Join  (cost=273.17..10380.18 rows=1 width=28) (actual time=18.844..116.882 rows=10000 loops=1)
L16                                        Hash Cond: (cu.name = a.cust_code)
L17                                        ->  Seq Scan on customers2 cu  (cost=0.00..8232.00 rows=500000 width=11) (actual time=0.010..28.450 rows=500000 loops=1)
L18                                        ->  Hash  (cost=273.16..273.16 rows=1 width=60) (actual time=18.820..18.822 rows=10000 loops=1)
L19                                              Buckets: 16384 (originally 1024)  Batches: 1 (originally 1)  Memory Usage: 761kB
L20                                              ->  CTE Scan on open_orders a  (cost=0.00..273.16 rows=1 width=60) (actual time=0.036..16.738 rows=10000 loops=1)
L21                                                    Filter: ((priority >= 8) AND (created_at >= '2026-10-01 00:00:00'::timestamp without time zone) AND (amount >= '900'::numeric))
L22                                  ->  CTE Scan on open_orders b  (cost=0.00..273.16 rows=1 width=12) (actual time=0.001..1.500 rows=10000 loops=10000)
L23                                        Filter: ((priority >= 8) AND (created_at >= '2026-10-01 00:00:00'::timestamp without time zone) AND (amount >= '900'::numeric))
L24          ->  Index Scan using customer_stats2_pkey on customer_stats2 cs  (cost=0.42..2.64 rows=1 width=10) (actual time=0.002..0.002 rows=1 loops=500)
L25                Index Cond: (customer_id = s.customer_id)
L26  Execution Time: 20625.978 ms
```

| Dòng | Giải thích |
|---|---|
| L20–L21 | Cùng 3 điều kiện, PG17 ước lượng **1 dòng**, thực tế 10 000 (sai **10 000×**) |
| L18–L19 | Build bảng băm từ phía CTE (tin chỉ có 1 dòng). `Buckets: 16384 (originally 1024)`: bảng băm **phải nới 16 lần** lúc chạy, một dấu hiệu nữa của ước lượng quá nhỏ |
| L15–L17 | Hash Join với customers2: chạy tốt (117 ms), vì quét customers2 chỉ một lần |
| **L12–L14** | Vì tin `a` = 1 dòng và `b` = 1 dòng, planner chọn **Nested Loop 1 × 1**, và điều kiện join `a.customer_id = b.customer_id` nằm trong **`Join Filter`**: so sánh **từng cặp** |
| **L22** | `CTE Scan b … loops=10000`: CTE `b` bị **quét lại 10 000 lần**, mỗi lần đọc và lọc 10 000 dòng (1.5 ms) |
| **L14** | `Rows Removed by Join Filter: 99 810 000` = 10 000 × 10 000 − 190 000: **100 triệu cặp** được so sánh để lấy 190 000 cặp đúng. Chữ ký O(N×M) |
| L09–L11 | Sort 190 000 cặp (12 MB, quicksort): ~150 ms, không đáng kể |

### 2.5 Tái tạo ước lượng

**PG13: cột CTE không có stats, mỗi điều kiện `>=` dùng `DEFAULT_INEQ_SEL` = 1/3**

| Node | `rows=` | Phép tính | Kết quả |
|---|---|---|---|
| L03 CTE | 10 067 | 2 000 000 × 0.0050333 (MCV `OPEN`) | 10 067 ✓ |
| **L18 / L26 CTE Scan** | **373** | 10 067 × (1/3)³ | **372.9 ⇒ 373 ✓** |
| L09 Merge Join | 692 | 373 × 373 / 200 (nd mặc định của `customer_id` trong CTE) × ~0.995 (`b.id <> a.id`) | ≈ 692 ✓ |

**PG17: mượn stats của từng cột `orders2` (toàn bảng) rồi nhân lại**

Selectivity từng điều kiện lấy bằng `EXPLAIN` từng điều kiện trên bảng gốc (PG17):

| Điều kiện | `rows=` trên `orders2` | Selectivity |
|---|---|---|
| `priority >= 8` | 15 200 | 0.00760 |
| `created_at >= '2026-10-01'` | 10 434 | 0.00522 |
| `amount >= 900` | 210 810 | 0.10541 |

| Node | `rows=` | Phép tính | Kết quả |
|---|---|---|---|
| L03 CTE | 9 933 | 2 000 000 × 0.0049667 | 9 933 ✓ |
| **L20 / L22 CTE Scan** | **1** | 9 933 × 0.00760 × 0.00522 × 0.10541 = **0.0415** | làm tròn lên tối thiểu **1 ✓** |

Hai lỗi chồng lên nhau ở PG17:
1. **Stats toàn bảng không áp dụng được cho tập con.** Trong CTE, cả 3 điều kiện đều đúng 100%, nhưng stats toàn bảng nói 0.76%, 0.52% và 10.5%.
2. **Giả định độc lập.** Ngay trên bảng gốc, planner cũng ước lượng 3 điều kiện cùng lúc chỉ ra **8 dòng**, trong khi thực tế là 10 000.

**Thử `CREATE STATISTICS (mcv, dependencies) ON priority, created_at, amount`** (trong transaction rồi ROLLBACK): ước lượng trên bảng gốc chỉ đổi từ 8 thành 7, và **CTE Scan vẫn 1 dòng**. Extended statistics **không** cứu được trường hợp này: CTE chỉ mượn stats từng cột, còn `amount` có ~100k giá trị nên danh sách MCV kết hợp không hữu ích.

### 2.6 Vì sao planner chọn Nested Loop: so sánh cost (chỉ `EXPLAIN`)

| Phương án | PG13 total cost | PG17 total cost |
|---|---|---|
| Plan mặc định | 15 943 (Merge Join) | **11 092 (Nested Loop)** |
| `SET enable_nestloop = off` | 25 518 | 21 114 |
| `open_orders AS NOT MATERIALIZED` | 10 602 | 10 626 |

Với ước lượng 1 × 1, PG17 tin Nested Loop rẻ hơn **gần 2 lần** so với phương án không dùng Nested Loop. Thực tế Nested Loop chậm hơn ~70 lần. Planner không "sai logic": nó tính đúng **trên con số sai**.

### 2.7 Thời gian 20.6 s đi đâu (PG17)

| Thành phần | Tính toán | Thời gian |
|---|---|---|
| Quét lại `CTE Scan b` | 10 000 × 1.5 ms (L22) | ≈ 15.0 s |
| So sánh 100M cặp trong Join Filter | Thời gian riêng của L12: 20.44 − 15.0 − 0.12 | ≈ 5.3 s |
| Hash Join với customers2, sort, aggregate | L15, L09, L07 | ≈ 0.3 s |

### 2.8 Cách sửa (đã đo trên PG17)
- `open_orders AS NOT MATERIALIZED`: **0.37 s**. Planner dùng được index `orders2(customer_id)` và tính selectivity trên bảng gốc.
- Bảng tạm + index + `ANALYZE`: planner có stats **của chính tập con**.
- `SET enable_nestloop = off`: 0.28 s, nhưng **chỉ để chẩn đoán**, không dùng trong production.

**pgdoctor báo:** `CMP-CTE-CORRELATION` (critical), `PLAN-NL-RESCAN` ("chạy lại CTE Scan 10 000 lần"), `PLAN-MISEST` (10 000×), `CMP-JOIN-METHOD` (Merge Join → Nested Loop).

---

## 3. CASE 2 – PG17 ngay sau `pg_upgrade` (chưa ANALYZE)

**Kết quả:**
- PG13: 4.7 s khi cache nóng; 13.6 s ở lần chụp dưới đây do cache lạnh (`I/O read` 3.1 s).
- PG17 chưa có stats: **hơn 15 phút** (bị `statement_timeout` cắt).
- PG17 sau `VACUUM; REINDEX; ANALYZE`: 6.0 s với `TIMING OFF`; 12.2 s ở lần chụp có timing.

### 3.1 Câu query (`lab/sql/case2_update_nostats.sql`)
```sql
WITH open_orders AS MATERIALIZED (
  SELECT o.customer_id, o.cust_code, o.amount FROM orders o
  WHERE o.status = 'OPEN' AND o.amount BETWEEN 100 AND 100000
    AND o.created_at BETWEEN timestamp '2024-03-01' AND timestamp '2030-01-01' AND o.note LIKE 'xx%'
), hot_complaints AS MATERIALIZED (
  SELECT c.customer_id, c.severity FROM complaints c
  WHERE c.severity BETWEEN 3 AND 5 AND c.customer_id BETWEEN 1 AND 1000000 AND c.body LIKE 'y%'
)
UPDATE customer_stats cs SET open_pairs = s.n_pairs, open_amount = s.total, updated_at = now()
FROM (SELECT oo.customer_id, count(*) AS n_pairs, sum(oo.amount) AS total
      FROM open_orders oo
      JOIN hot_complaints hc ON hc.customer_id = oo.customer_id
      JOIN customers cu      ON cu.name = oo.cust_code
      GROUP BY oo.customer_id) s
WHERE cs.customer_id = s.customer_id;
```
Dữ liệu: `orders` 2M dòng (40 000 OPEN thuộc 500 khách), `complaints` 1M dòng, `customers` 500k dòng. Thực tế `open_orders` = 32 459 dòng, `hot_complaints` = 499 117 dòng.

### 3.2 Plan PG17 chưa có stats – chỉ `EXPLAIN` (`lab/results/case2_pg17_nostats_explain.txt`)
```
L01  Update on customer_stats cs  (cost=20985.50..20987.75 rows=0 width=0)
L02    CTE open_orders
L03      ->  Bitmap Heap Scan on orders o  (cost=69.17..7588.18 rows=1 width=52)
L04            Recheck Cond: (status = 'OPEN'::text)
L05            Filter: ((amount >= '100'::numeric) AND (amount <= '100000'::numeric) AND (created_at >= '2024-03-01 00:00:00'::timestamp without time zone) AND (created_at <= '2030-01-01 00:00:00'::timestamp without time zone) AND (note ~~ 'xx%'::text))
L06            ->  Bitmap Index Scan on orders_status_idx  (cost=0.00..69.16 rows=7845 width=0)
L07                  Index Cond: (status = 'OPEN'::text)
L08    CTE hot_complaints
L09      ->  Bitmap Heap Scan on complaints c  (cost=62.63..4790.84 rows=1 width=6)
L10            Recheck Cond: ((customer_id >= 1) AND (customer_id <= 1000000))
L11            Filter: ((severity >= 3) AND (severity <= 5) AND (body ~~ 'y%'::text))
L12            ->  Bitmap Index Scan on complaints_customer_idx  (cost=0.00..62.63 rows=5670 width=0)
L13                  Index Cond: ((customer_id >= 1) AND (customer_id <= 1000000))
L14    ->  Nested Loop  (cost=8606.48..8608.72 rows=1 width=122)
L15          ->  Subquery Scan on s  (cost=8606.05..8606.08 rows=1 width=112)
L16                ->  HashAggregate  (cost=8606.05..8606.07 rows=1 width=44)
L17                      Group Key: oo.customer_id
L18                      ->  Nested Loop  (cost=0.03..8591.41 rows=1953 width=20)
L19                            Join Filter: (oo.customer_id = hc.customer_id)
L20                            ->  CTE Scan on hot_complaints hc  (cost=0.00..0.02 rows=1 width=4)
L21                            ->  Hash Join  (cost=0.03..8566.97 rows=1953 width=20)
L22                                  Hash Cond: (cu.name = oo.cust_code)
L23                                  ->  Seq Scan on customers cu  (cost=0.00..7082.48 rows=390648 width=32)
L24                                  ->  Hash  (cost=0.02..0.02 rows=1 width=52)
L25                                        ->  CTE Scan on open_orders oo  (cost=0.00..0.02 rows=1 width=52)
L26          ->  Index Scan using customer_stats_pkey on customer_stats cs  (cost=0.42..2.64 rows=1 width=10)
L27                Index Cond: (customer_id = s.customer_id)
L28  Settings: work_mem = '64MB', random_page_cost = '1.1'
```
Plan này lấy từ một bản clone của DB `shop` đã xoá `pg_statistic` và đặt `reltuples = -1`, đúng trạng thái ngay sau `pg_upgrade`. Chạy ANALYZE mất hơn 15 phút nên chỉ có `EXPLAIN`.

### 3.3 Tái tạo ước lượng khi không có stats

**Bước 1: số dòng mỗi bảng, suy ra từ kích thước file.** Mật độ = ⌊8168 / (width + 28)⌋.

| Bảng | Block thật | width (theo kiểu dữ liệu, `text` = 32) | Mật độ | `rows=` |
|---|---|---|---|---|
| `customers` | 3 176 | 4 + 2 + 32 = **38** | ⌊8168/66⌋ = 123 | 3 176 × 123 = **390 648 ✓** (L23; thật 500 000) |
| `orders` | 30 764 | **132** | ⌊8168/160⌋ = 51 | 30 764 × 51 = **1 568 964** (thật 2 000 000) |
| `complaints` | 10 310 | 8 + 4 + 2 + 32 = **46** | ⌊8168/74⌋ = 110 | 10 310 × 110 = **1 134 100** (thật 1 000 000) |

**Bước 2: mỗi điều kiện dùng hằng mặc định.**

| Node | `rows=` | Phép tính | Kết quả |
|---|---|---|---|
| L06 `status = 'OPEN'` | 7 845 | 1 568 964 × 0.005 | 7 844.8 ⇒ 7 845 ✓ (thật 40 000) |
| L03 + 4 điều kiện | 1 | 1 568 964 × 0.005 (status) × 0.005 (amount BETWEEN) × 0.005 (created_at BETWEEN) × 0.005 (LIKE) = 0.001 | 1 ✓ (thật 32 459) |
| L12 `customer_id BETWEEN` | 5 670 | 1 134 100 × 0.005 | 5 670 ✓ |
| L09 + 2 điều kiện | 1 | 5 670 × 0.005 × 0.005 = 0.14 | 1 ✓ (thật 499 117) |
| L21 Hash Join | 1 953 | 390 648 × 1 / 200 (nd mặc định cho `cu.name` và `oo.cust_code`) | 1 953.2 ⇒ 1 953 ✓ |
| L18 Nested Loop | 1 953 | 1 × 1 953 × 1/max(1,1) (nd của CTE 1 dòng bị kẹp còn 1) | 1 953 ✓ |

### 3.4 Vì sao đây là thảm hoạ

- **L18–L21:** `hot_complaints` được tin là 1 dòng nên làm **outer** của Nested Loop. Inner là **toàn bộ L21**: seq scan 500k khách hàng và build lại hash. Cost của inner chỉ là 8 567, nhân 1 lần.
- **Thực tế:** inner bị chạy **499 117 lần**. Ở plan PG17 sau ANALYZE, `Seq Scan on customers` mất ~27 ms một lần.
- **Ước tính:** 499 117 × (30–50 ms) ≈ **4.2–6.9 giờ**. Lần chạy thật bị cắt ở 15 phút.

### 3.5 Plan PG13 – có stats (`lab/results/case2_pg13.txt`)
```
L01  Update on customer_stats cs  (cost=111343.95..1849777.40 rows=200 width=126) (actual time=13559.065..13559.074 rows=0 loops=1)
L02    Buffers: shared hit=5505 read=44301 dirtied=19 written=5
L03    I/O Timings: read=3062.324
L04    CTE open_orders
L05      ->  Index Scan using orders_status_idx on orders o  (cost=0.43..3627.03 rows=30999 width=21) (actual time=418.151..2567.129 rows=32459 loops=1)
L06            Index Cond: (status = 'OPEN'::text)
L07            Filter: ((amount >= '100'::numeric) AND (amount <= '100000'::numeric) AND (created_at >= …) AND (created_at <= …) AND (note ~~ 'xx%'::text))
L08            Rows Removed by Filter: 7541
L09            Buffers: shared read=30801
L10    CTE hot_complaints
L11      ->  Seq Scan on complaints c  (cost=0.00..32810.00 rows=498300 width=6) (actual time=0.085..831.755 rows=499117 loops=1)
L12            Filter: ((severity >= 3) AND (severity <= 5) AND (customer_id >= 1) AND (customer_id <= 1000000) AND (body ~~ 'y%'::text))
L13            Rows Removed by Filter: 500883
L14    ->  Nested Loop  (cost=74906.91..1813340.37 rows=200 width=126) (actual time=4525.388..13538.352 rows=500 loops=1)
L15          ->  Subquery Scan on s  (cost=74906.49..1812831.17 rows=200 width=112) (actual time=4523.977..13518.604 rows=500 loops=1)
L16                ->  GroupAggregate  (cost=74906.49..1812829.17 rows=200 width=44) (actual time=4523.961..13517.019 rows=500 loops=1)
L17                      Group Key: oo.customer_id
L18                      ->  Merge Join  (cost=74906.49..1233571.61 rows=77234008 width=20) (actual time=4519.858..9890.146 rows=32436838 loops=1)
L19                            Merge Cond: (oo.customer_id = hc.customer_id)
L20                            ->  Sort  (cost=17784.73..17862.23 rows=30999 width=20) (actual time=3239.800..3256.195 rows=32459 loops=1)
L21                                  ->  Hash Join  (cost=14426.00..15472.22 rows=30999 width=20) (actual time=994.780..3217.446 rows=32459 loops=1)
L22                                        Hash Cond: (oo.cust_code = cu.name)
L23                                        ->  CTE Scan on open_orders oo  (cost=0.00..619.98 rows=30999 width=52) (actual time=418.155..2600.396 rows=32459 loops=1)
L24                                        ->  Hash  (cost=8176.00..8176.00 rows=500000 width=11) (actual time=565.484..565.485 rows=500000 loops=1)
L25                                              ->  Seq Scan on customers cu  (cost=0.00..8176.00 rows=500000 width=11) (actual time=0.156..321.163 rows=500000 loops=1)
L26                            ->  Sort  (cost=57121.76..58367.51 rows=498300 width=4) (actual time=1279.957..3246.327 rows=32436801 loops=1)
L27                                  Sort Method: quicksort  Memory: 35685kB
L28                                  ->  CTE Scan on hot_complaints hc  (cost=0.00..9966.00 rows=498300 width=4) (actual time=0.090..1061.523 rows=499117 loops=1)
L29          ->  Index Scan using customer_stats_pkey on customer_stats cs  (cost=0.42..2.54 rows=1 width=10) (actual time=0.028..0.028 rows=1 loops=500)
L30  JIT: … Inlining 159.585 ms, Optimization 149.104 ms, Emission 108.486 ms, Total 420.941 ms
L31  Execution Time: 13590.616 ms
```
*(Rút gọn dòng `Buffers`/`I/O Timings` của node con. Bản đầy đủ nằm trong file kết quả.)*

| Dòng | Giải thích |
|---|---|
| L05–L09, L03 | Cache lạnh: `shared read=30801` block và `I/O read` 3.1 s trên tổng 13.6 s. Đây là lý do lần đo cache nóng chỉ còn 4.7 s |
| L05 / L11 | Có stats nên hai CTE ước lượng **đúng**: 30 999 (thật 32 459) và 498 300 (thật 499 117) |
| L18 | Merge Join ước lượng **77 234 008** = 30 999 × 498 300 / **200**. PG13 không có stats cho cột CTE nên dùng nd mặc định 200 ✓. Thực tế 32 436 838 dòng, cùng bậc độ lớn |
| L26 `rows=32436801` | Phía inner của Merge Join trả 32M dòng dù CTE chỉ 499k: Merge Join đọc lại các nhóm khóa trùng (mark/restore). Không phải lỗi ước lượng |
| L16 | GroupAggregate: dồn 32M dòng thành 500 nhóm (khoảng 3.6 s) |
| L30 | Cost 1.85M > 500 000 nên JIT bật inlining và optimization: 0.42 s |

### 3.6 Plan PG17 sau `VACUUM; REINDEX DATABASE; ANALYZE` (`lab/results/case2_pg17_after_analyze.txt`)
```
L01  Update on customer_stats cs  (cost=119715.28..127551.36 rows=0 width=0) (actual time=12150.885..12150.911 rows=0 loops=1)
L02    CTE open_orders
L03      ->  Index Scan using orders_status_idx on orders o  (cost=0.43..4156.09 rows=34423 width=21) (actual time=0.050..48.066 rows=32459 loops=1)
L04    CTE hot_complaints
L05      ->  Seq Scan on complaints c  (cost=0.00..32810.00 rows=499700 width=6) (actual time=14.240..292.768 rows=499117 loops=1)
L06    ->  Nested Loop  (cost=82749.18..90585.27 rows=5852 width=122) (actual time=12142.126..12144.547 rows=500 loops=1)
L07          ->  Subquery Scan on s  (cost=82748.76..82880.43 rows=5852 width=112) (actual time=12141.981..12142.559 rows=500 loops=1)
L08                ->  HashAggregate  (cost=82748.76..82821.91 rows=5852 width=44) (actual time=12141.950..12142.363 rows=500 loops=1)
L09                      Group Key: oo.customer_id
L10                      Batches: 1  Memory Usage: 465kB
L11                      ->  Hash Join  (cost=15694.26..60703.53 rows=2939364 width=20) (actual time=177.958..4866.781 rows=32436838 loops=1)
L12                            Hash Cond: (hc.customer_id = oo.customer_id)
L13                            ->  CTE Scan on hot_complaints hc  (cost=0.00..9994.00 rows=499700 width=4) (actual time=14.244..527.522 rows=499117 loops=1)
L14                            ->  Hash  (cost=15263.98..15263.98 rows=34423 width=20) (actual time=163.441..163.450 rows=32459 loops=1)
L15                                  ->  Hash Join  (cost=1118.75..15263.98 rows=34423 width=20) (actual time=67.566..159.495 rows=32459 loops=1)
L16                                        Hash Cond: (cu.name = oo.cust_code)
L17                                        ->  Seq Scan on customers cu  (cost=0.00..8176.00 rows=500000 width=11) (actual time=0.021..26.796 rows=500000 loops=1)
L18                                        ->  Hash  (cost=688.46..688.46 rows=34423 width=52) (actual time=67.273..67.275 rows=32459 loops=1)
L19                                              ->  CTE Scan on open_orders oo  (cost=0.00..688.46 rows=34423 width=52) (actual time=0.053..59.384 rows=32459 loops=1)
L20          ->  Index Scan using customer_stats_pkey on customer_stats cs  (cost=0.42..1.31 rows=1 width=10) (actual time=0.002..0.002 rows=1 loops=500)
L21  JIT: … Inlining false, Optimization false … Total 17.636 ms
L22  Execution Time: 12172.757 ms
```

| Node | `rows=` | Phép tính (PG17 mượn stats cột qua CTE) | Kết quả |
|---|---|---|---|
| L08 số nhóm | **5 852** | `orders.customer_id` có `n_distinct = −0.1700005` (17% số dòng), áp lên số dòng CTE: 0.1700005 × 34 423 | **5 851.9 ⇒ 5 852 ✓** (thật 500) |
| L11 Hash Join | 2 939 364 | 34 423 × 499 700 / max(5 852, 500) | 2 939 357 ≈ ✓ (thật 32.4M, thiếu 11×) |

- Mọi node `loops=1`, plan có cùng "hình dạng" với PG13 nên **không có regression**.
- Ước lượng join thiếu 11× nhưng **chưa đủ** để đổi plan.
- Hai con số trên là dấu vết của cơ chế 0.4: tỉ lệ 17% của toàn bảng bị áp lên tập con chỉ có 1.5% giá trị khác nhau (500 / 32 459).
- Khoảng một nửa trong 12.2 s là overhead đo thời gian khi gộp 32M dòng; với `TIMING OFF` chỉ còn 6.0 s.

**Kết luận CASE 2:** giải thích được các query chậm **ngay sau** upgrade và được sửa bằng ANALYZE. Dự án đã ANALYZE mà vẫn chậm, nên câu thật thuộc nhóm **CASE 3/4**.

**pgdoctor báo** (PG17 chỉ có `EXPLAIN`):
- `CMP-NOSTATS`: dấu hiệu thiếu stats.
- `CMP-EST-SHIFT`: hai CTE từ 498k xuống 1 và từ 31k xuống 1.
- `PLAN-NL-BET` (high): "mỗi lần lặp quét tới ~390k dòng".
- `CMP-JOIN-METHOD`: Merge Join → Nested Loop.

---

## 4. CASE 1 – đối chứng: cùng plan, ước lượng khác

**Kết quả:** PG13 27.0 s, PG17 15.5 s, **PG17 nhanh hơn**. Case này chỉ để minh hoạ công thức `n_distinct` ở 0.4. Plan đầy đủ: `lab/results/case1_pg13.txt`, `lab/results/case1_pg17.txt`.

Câu query (`lab/sql/case1_update.sql`): CTE `open_orders` (40 000 đơn OPEN của 500 khách) join `complaints` (1M dòng, 500 khách × ~2 000) theo `customer_id`, sau đó `GROUP BY customer_id`. Thực tế: **80 071 993** dòng join, **500** nhóm.

| | PG13 | PG17 |
|---|---|---|
| CTE `open_orders` | 38 333 = 2M × 0.0191667 | 42 600 = 2M × 0.0213 |
| Ước lượng join | **76 666 000** = 38 333 × 1 000 000 / max(**200**, 500) | **5 882 343** = 42 600 × 1 000 000 / max(**7 242**, 500) |
| Ước lượng số nhóm | **200** (mặc định) | **7 242** = 0.1700005 × 42 600 |
| Thực tế | 80.07M dòng, 500 nhóm | 80.07M dòng, 500 nhóm |
| Join | `Hash Join`: hash phía **complaints** (1M dòng, 43 MB), probe bằng CTE | `Hash Join`: hash phía **CTE** (40k dòng, 2.2 MB), probe bằng `Index Only Scan` complaints |
| JIT | cost 1.51M nên bật inlining/optimization: 161 ms | cost 147k nên không bật: 9 ms |
| Thời gian | 27.0 s | 15.5 s |

**Ý nghĩa:**
- PG13 dùng nd mặc định 200 cho cột CTE nên **tình cờ** ước lượng join đúng (77M so với 80M).
- PG17 dùng tỉ lệ 17% của toàn bảng nên ước lượng join thiếu 14× và số nhóm thừa 14×.
- Ở query này cả hai vẫn ra Hash Join. PG17 còn chọn phía build nhỏ hơn nên nhanh hơn.
- Nhưng cùng một cơ chế sai lệch, trong một query nhiều bảng hơn, có thể đổi thứ tự join theo hướng xấu.

---

## 5. Bảng tra nhanh và quy trình áp dụng cho câu thật

| Dấu hiệu trên plan PG17 (so với PG13) | Case | Thử ngay (trong `BEGIN … ROLLBACK`) | Sửa bền vững |
|---|---|---|---|
| `Filter: (NOT (ANY (… = (SubPlan N).col1)))` **không có `hashed`**, node dưới SubPlan có `loops` = số dòng ngoài, `temp read` khổng lồ | **★ 4** | `SET LOCAL work_mem = '<đủ lớn>';`. Ngưỡng: rows × (width + 24) ≤ work_mem × hash_mem_multiplier | `NOT IN` → `NOT EXISTS` (chú ý NULL) |
| `Nested Loop` + `CTE Scan … rows=1` (thực tế hàng nghìn trở lên) với nhiều điều kiện trên cột CTE, inner có `loops` lớn, `Rows Removed by Join Filter` khổng lồ | 3 | `SET LOCAL enable_nestloop = off;` (chỉ chẩn đoán) | `NOT MATERIALIZED`, hoặc bảng tạm + index + `ANALYZE` (`CREATE STATISTICS` **không** giúp) |
| Mọi scan `rows=1` dù có nhiều điều kiện; số dòng scan toàn bảng lệch ~20% (suy từ block); `pg_stats` rỗng | 2 | `ANALYZE <bảng>;` | `vacuumdb --all --analyze-in-stages` sau upgrade; kiểm tra ANALYZE không lỗi (search_path ở PG17) |
| Số nhóm hoặc ước lượng join trên cột CTE: PG13 = 200 (mặc định), PG17 = tỉ lệ `n_distinct` × số dòng CTE | 1 (dấu vết `f7816aec2`) | `AS NOT MATERIALIZED` để so | Tránh lọc/join nặng trên cột của CTE materialized |

**Quy trình cho câu thật của dự án:**
1. Lấy `EXPLAIN (SETTINGS, FORMAT JSON)` (không ANALYZE) trên PG17, và `EXPLAIN (ANALYZE, BUFFERS, SETTINGS, FORMAT JSON)` trên PG13 (bản clone trước upgrade).
2. Chạy: `python .claude/skills/pgdoctor/pgdoctor_cli.py analyze --old-plan q.pg13.json --new-plan q.pg17.json -o report.html`.
3. Tìm trong plan:
   - chữ `hashed` ở các dòng `SubPlan` (CASE 4);
   - `CTE Scan … rows=1` ở inner của `Nested Loop` (CASE 3);
   - các node CTE có `rows=` khác nhau giữa hai bản.
4. Tái tạo con số `rows=` bằng các công thức ở Phụ lục A với `pg_stats` thật. Nếu khớp, đã xác định được *vì sao* planner đoán như vậy.
5. Thử cách sửa trong `BEGIN … ROLLBACK`, so cost bằng `EXPLAIN`, rồi mới đo bằng `EXPLAIN ANALYZE`.

---

## Phụ lục A. Hằng số và công thức của planner dùng trong tài liệu

| Hằng / công thức | Giá trị | Dùng ở |
|---|---|---|
| `DEFAULT_EQ_SEL` | 0.005 | CASE 4 (PG13: 947 143 × 0.005 = 4 736), CASE 2 |
| `DEFAULT_INEQ_SEL` | 1/3 | CASE 3 (PG13: 10 067 × (1/3)³ = 373) |
| `DEFAULT_RANGE_INEQ_SEL` | 0.005 | CASE 2 (`BETWEEN`) |
| `DEFAULT_MATCH_SEL` (LIKE) | 0.005 | CASE 2 |
| `DEFAULT_NUM_DISTINCT` | 200 | CASE 1, 2 (PG13), CASE 2 không stats |
| Join selectivity (không MCV) | 1 / max(nd₁, nd₂) | CASE 1, 2 |
| `n_distinct` âm | \|n_distinct\| × số dòng của rel (PG17: số dòng **của CTE**) | CASE 1 (7 242), CASE 2 (5 852) |
| Số dòng khi `reltuples = -1` | block × ⌊8168 / (width + 28)⌋ | CASE 2 (390 648; 1 568 964; 1 134 100) |
| Ngưỡng `hashed SubPlan` | rows × (MAXALIGN(width) + 24) ≤ work_mem × hash_mem_multiplier | CASE 4 (kiểm chứng tại 10MB/11MB) |
| JIT inlining/optimization | total cost > 500 000 | CASE 2 (PG13), CASE 4 (PG17), CASE 1 (PG13) |
| Block | 8 KB | `Buffers`, `temp read` (6 348 856 block ≈ 48.4 GB) |
