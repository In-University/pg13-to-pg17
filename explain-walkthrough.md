# Giải thích EXPLAIN từng case: PG13 vs PG17

> Report này đi kèm [`pg13-vs-pg17-query-regression.md`](pg13-vs-pg17-query-regression.md) (Mục 7) và [`README.md`](README.md).
> Output **đầy đủ, nguyên văn** của từng plan nằm trong thư mục `results/`. Các plan dưới đây được trích từ đó, chỉ bỏ bớt dòng `Buffers`/`I/O Timings` cho dễ đọc.
> Mọi plan được chụp bằng `BEGIN; EXPLAIN (ANALYZE, BUFFERS) <câu UPDATE>; ROLLBACK;` nên không thay đổi dữ liệu.
> Chế độ `ANALYZE` có đo thời gian từng node, nên tổng thời gian có thể cao hơn số trong bảng tổng hợp (lần đo đó dùng `TIMING OFF`). Một số lần đo chạy song song với query khác. **Hệ số chênh lệch giữa hai plan mới là điều cần nhìn, không phải số mili-giây tuyệt đối.**

---

## 0. Cách đọc một dòng plan (60 giây)

```
->  CTE Scan on current_brand_code cbc  (cost=0.00..21310.72 rows=4736 width=32) (actual time=5.041..854.093 rows=400000 loops=1)
      Filter: (region = 'VN'::text)
      Rows Removed by Filter: 600000
```

| Phần | Ý nghĩa |
|---|---|
| `CTE Scan on current_brand_code cbc` | Loại node: đọc kết quả của CTE đã materialize. CTE Scan **không có index**, chỉ đọc tuần tự. |
| `cost=0.00..21310.72` | Chi phí **ước lượng** (đơn vị tương đối): chi phí trả dòng đầu tiên .. chi phí trả hết. |
| `rows=4736` | Số dòng planner **ĐOÁN** node này trả ra. **Mọi quyết định chọn plan đều dựa trên con số này.** |
| `width=32` | Độ rộng trung bình ước lượng của mỗi dòng (byte). |
| `actual time=5.041..854.093` | Thời gian thật (ms): ra dòng đầu .. ra dòng cuối, tính **cho một lần chạy** của node. |
| `actual rows=400000` | Số dòng **THẬT** trả ra (trung bình mỗi lần chạy). |
| `loops=1` | Node được chạy bao nhiêu lần. **Tổng thời gian ≈ actual time × loops**, tổng số dòng ≈ actual rows × loops. |
| `Filter:` / `Rows Removed by Filter` | Điều kiện lọc áp trên từng dòng, và số dòng bị loại bỏ. |

**Quy tắc vàng:** so `rows=` (đoán) với `actual rows=` (thật) từ node lá đi lên. Node **đầu tiên** lệch hàng chục, hàng trăm lần là nơi planner bắt đầu chọn sai. Sau đó tìm node có `loops` lớn hoặc `Rows Removed by Join Filter` khổng lồ: đó là nơi tốn thời gian.

Hai ký hiệu quan trọng trong report này:
- `hashed SubPlan N`: subquery chạy **1 lần**, kết quả đưa vào bảng băm, mỗi dòng ngoài tra O(1). **Tốt.**
- `SubPlan N` (không có chữ `hashed`): mỗi dòng ngoài **quét lại** kết quả subquery từ đầu. **O(N×M), rất nguy hiểm** với `NOT IN`.

---

## 1. ★ CASE 4 – MẪU CÂU CỦA DỰ ÁN: `UPDATE … NOT IN (SELECT … FROM current_brand_code …)`

**Kết quả:** PG13 **0.8–1.0 s**, PG17 **257–289 s**. Hệ số **≈ 270–365×**. Cả hai đều **đã VACUUM ANALYZE**.

### 1.1 Câu query (`sql/case4_update.sql`)
```sql
SET work_mem = '4MB';                                  -- (1) mặc định PostgreSQL / nhiều tier Cloud SQL
WITH a AS NOT MATERIALIZED (                           -- (2) được inline vào chỗ dùng
  SELECT s.id, s.region FROM stores s WHERE s.active
), current_brand_code AS MATERIALIZED (                -- (3) chạy 1 lần, kết quả lưu tạm (không index, không stats)
  SELECT b.code AS brand_code, a.region                --     a.region là cột "trơn" của stores.region
  FROM brand_assignments ba
  JOIN brands b ON b.id = ba.brand_id
  JOIN a        ON a.id = ba.store_id
  WHERE ba.is_current
)
UPDATE products p
SET status = 'ORPHAN', updated_at = now()
WHERE p.status = 'PENDING'                             -- (4) 5 000 sản phẩm
  AND p.brand_code NOT IN (                            -- (5) NOT IN: không bao giờ được chuyển thành anti-join
        SELECT cbc.brand_code FROM current_brand_code cbc
        WHERE cbc.region = 'VN'                        -- (6) điều kiện đặt TRÊN CỘT CỦA CTE
      );
```
Dữ liệu: 20k stores (40% `VN`), 5k brands, 2M brand_assignments (50% `is_current`), 200k products (5k `PENDING`, một nửa mang mã `LEGACY-…` không có trong danh sách).

### 1.2 Plan PG13 — 0.95 s (`results/case4_pg13.txt`)
```
Update on products p  (rows=2551) (actual time=948.590..948.597 rows=0 loops=1)
  CTE current_brand_code
    ->  Hash Join  (rows=947143) (actual time=5.034..542.755 rows=1000000 loops=1)              [A]
          Hash Cond: (ba.brand_id = b.id)
          ->  Hash Join  (rows=949038) (actual rows=1000000 loops=1)
                Hash Cond: (ba.store_id = s.id)
                ->  Seq Scan on brand_assignments ba  (rows=1003333) (actual rows=1000000 loops=1)
                      Filter: is_current
                ->  Hash -> Seq Scan on stores s  (rows=19000) (actual rows=19000 loops=1)
                      Filter: active
          ->  Hash -> Seq Scan on brands b  (rows=5000) (actual rows=5000 loops=1)
  ->  Index Scan using products_status_idx on products p  (rows=2551) (actual time=923.060..931.367 rows=2500 loops=1)   [B]
        Index Cond: (status = 'PENDING'::text)
        Filter: (NOT (hashed SubPlan 2))                                                        [C]
        Rows Removed by Filter: 2500
        SubPlan 2
          ->  CTE Scan on current_brand_code cbc  (rows=4736) (actual time=5.041..854.093 rows=400000 loops=1)   [D]
                Filter: (region = 'VN'::text)
                Rows Removed by Filter: 600000
Execution Time: 951.028 ms
```
Đọc từ dưới lên:

| Mốc | Diễn giải |
|---|---|
| **[A]** | CTE `current_brand_code` được tính **1 lần**: hai Hash Join nối `brand_assignments` (1M dòng hiện hành) với `stores` và `brands`. Ước lượng 947k, thực tế 1M, **chính xác**. Mất ~0.54 s. |
| **[D]** | Subquery của `NOT IN`: đọc CTE, lọc `region = 'VN'`. **Ước lượng 4 736 dòng, thực tế 400 000 (sai 85×).** Vì sao? Ở PG13 cột của `CTE Scan` **không có thống kê**, nên `region = 'VN'` dùng selectivity mặc định **0.5%**: 947 143 × 0.005 ≈ 4 736. |
| **[C]** | Vì planner tin subquery chỉ có ~4.7k dòng × (32 + 24 byte) ≈ 0.26 MB, nhỏ hơn `work_mem` 4 MB, nên nó chọn **`hashed SubPlan`**. `loops=1` ở [D] chứng minh subquery **chỉ chạy 1 lần**. 400k mã brand được đưa vào bảng băm (bảng băm được phép lớn hơn ước lượng lúc chạy). |
| **[B]** | Lấy 5 000 sản phẩm `PENDING` qua index, mỗi dòng tra bảng băm O(1). 2 500 dòng có mã nằm trong danh sách bị loại (`Rows Removed by Filter: 2500`), 2 500 dòng `LEGACY-…` sẽ được UPDATE. |

**Tóm lại:** PG13 **đoán sai rất nhiều** (4.7k thay vì 400k) nhưng sai **theo hướng có lợi**, nên ra plan băm nhanh.

### 1.3 Plan PG17 — 257 s (lần chụp này; lần đo `TIMING OFF`: 289 s) (`results/case4_pg17.txt`)
```
Update on products p  (rows=0) (actual time=257005.662..257005.675 rows=0 loops=1)
  Buffers: shared hit=39425 dirtied=1934 written=2, temp read=6348856 written=2537                [F]
  CTE current_brand_code
    ->  Hash Join  (rows=949241) (actual time=7.226..410.082 rows=1000000 loops=1)                 [A]
          Hash Cond: (ba.brand_id = b.id)
          ->  Hash Join  (rows=950380) (actual rows=1000000 loops=1)
                Hash Cond: (ba.store_id = s.id)
                ->  Seq Scan on brand_assignments ba  (rows=1000400) (actual rows=1000000 loops=1)
                      Filter: is_current
                ->  Hash -> Seq Scan on stores s  (rows=19000) (actual rows=19000 loops=1)
          ->  Hash -> Seq Scan on brands b  (rows=5000) (actual rows=5000 loops=1)
  ->  Index Scan using products_status_idx on products p  (cost=0.29..55913216.62 rows=2507)
                                     (actual time=963.262..256763.926 rows=2500 loops=1)         [B]
        Index Cond: (status = 'PENDING'::text)
        Filter: (NOT (ANY (brand_code = (SubPlan 2).col1)))                                       [C]
        Rows Removed by Filter: 2500
        SubPlan 2
          ->  CTE Scan on current_brand_code cbc  (rows=379696) (actual time=0.004..41.935 rows=200252 loops=5000)   [D]
                Filter: (region = 'VN'::text)
                Rows Removed by Filter: 300378
                I/O Timings: temp read=8624.995
JIT: … Total 247.758 ms
Execution Time: 257021.845 ms
```

| Mốc | Diễn giải |
|---|---|
| **[A]** | CTE giống hệt PG13: ước lượng ~949k, thực tế 1M. **Không có gì khác** ở phần join nhiều bảng. |
| **[D]** | Subquery `NOT IN`: **ước lượng 379 696 dòng** (thực tế 400k, gần đúng). Khác PG13 vì commit `f7816aec2` của PG17: planner "nhìn xuyên" vào CTE, thấy `cbc.region` chính là `stores.region` và **dùng thống kê thật của `stores.region`** (VN = 40%): ≈ 949k × 0.4 ≈ 380k. |
| **[C]** | Giờ planner tính kích thước bảng băm: 379 696 × (32 + 24 byte) ≈ **21 MB**, lớn hơn `work_mem × hash_mem_multiplier` = 4 MB × 2 = **8 MB**. Planner **không dám dùng bảng băm**: dòng `Filter` **không còn chữ `hashed`**: `NOT (ANY (brand_code = (SubPlan 2).col1))`. |
| **[D] loops** | `loops=5000`: với **mỗi** sản phẩm PENDING, PG17 quét lại kết quả CTE từ đầu. Sản phẩm có mã khớp dừng sớm. Sản phẩm `LEGACY-…` (không khớp) phải đọc **toàn bộ** 1M dòng CTE và lọc 400k dòng VN mới kết luận được "NOT IN = true". `actual rows=200252` là **trung bình** mỗi lần quét. |
| **[B]** | `cost=0.29..55913216.62`: chính planner cũng **biết** plan này rất đắt (55 triệu đơn vị cost so với ~60 nghìn ở PG13). Nhưng vì đã loại phương án băm (do ngưỡng bộ nhớ), nó **không còn lựa chọn nào khác** cho `NOT IN`. `actual time=963..256763`: dòng đầu ra sau 1 s, dòng cuối sau 257 s. |
| **[F]** | `temp read=6348856`: kết quả CTE (1M dòng ≈ 20 MB) **lớn hơn `work_mem` 4 MB nên bị ghi ra file tạm**. Mỗi lần quét lại phải đọc lại file tạm: 6.3 triệu block × 8 KB ≈ **48 GB đọc từ file tạm** (phần lớn trúng OS cache: `temp read=8.6 s`). PG13 cũng ghi file tạm (`temp written=2536`) nhưng chỉ đọc **1 lần**. Trên Cloud SQL với disk thật, phần I/O này còn có thể nặng hơn. |
| **Tổng** | ≈ 5 000 lần × (≈ 200k dòng trả ra + ≈ 300k dòng bị lọc) ≈ **2.5 tỉ dòng được đọc/so sánh**, so với 1M dòng ở PG13. Đó là 257–289 giây. |

### 1.4 So sánh trực tiếp

| | PG13 | PG17 |
|---|---|---|
| Ước lượng subquery `NOT IN` | 4 736 (mặc định 0.5%) | 379 696 (stats `stores.region`) |
| Thực tế | 400 000 | 400 000 |
| Ước lượng kích thước băm | ≈ 0.26 MB | ≈ 21 MB |
| Ngưỡng `work_mem × hash_mem_multiplier` | 4 MB × **1** = 4 MB | 4 MB × **2** = 8 MB |
| Kiểu subplan | **`hashed SubPlan`**, `loops=1` | **`SubPlan`**, `loops=5000` |
| Đọc file tạm | ghi 2 536 block, đọc 1 lần | đọc lại **6.3 triệu block** (≈ 48 GB) |
| Thời gian | ≈ 0.8–1.0 s | ≈ 257–289 s |

### 1.5 Kiểm chứng nguyên nhân (chỉ đổi một yếu tố mỗi lần)

| Biến thể | PG13 | PG17 | Kết luận |
|---|---|---|---|
| Nguyên bản | `hashed SubPlan` | `SubPlan` | Regression |
| `current_brand_code AS NOT MATERIALIZED` (inline, cả hai cùng thấy stats thật) | **`SubPlan`** | `SubPlan` | Khi PG13 cũng biết sự thật, **PG13 cũng chọn plan xấu**. Nguyên nhân là **thông tin stats planner nhìn thấy**, không phải PG17 chạy chậm hơn. |
| `SET work_mem = '16MB'` | `hashed SubPlan` | `hashed SubPlan` | Ngưỡng bộ nhớ là "công tắc" quyết định. |
| `NOT EXISTS (SELECT 1 … WHERE cbc.brand_code = p.brand_code)` | `Merge Anti Join` | `Hash Anti Join` | Anti-join không phụ thuộc ngưỡng: **cách sửa bền vững**. |

> ⚠️ `NOT IN` và `NOT EXISTS` khác nhau khi subquery có `NULL`: `x NOT IN (…, NULL)` không bao giờ đúng, còn `NOT EXISTS` thì bỏ qua NULL. Nếu cột `brand_code` có thể NULL, cần thêm `WHERE cbc.brand_code IS NOT NULL` hoặc xác nhận lại nghiệp vụ trước khi đổi.

### 1.6 Cách nhận ra câu thật của dự án có rơi vào CASE 4 không
1. `EXPLAIN` (không cần ANALYZE) câu UPDATE trên PG17 và trên một bản PG13 (clone).
2. Tìm dòng `Filter:` chứa `SubPlan`. PG13 là `hashed SubPlan N`, PG17 chỉ còn `(SubPlan N)` thì **đúng là CASE 4**.
3. So `rows=` của node dưới `SubPlan N` giữa hai bản: PG17 lớn hơn nhiều chính là dấu vết của việc lấy stats qua CTE.
4. Thử nhanh trong session: `SET work_mem = '64MB'; EXPLAIN …`. Nếu xuất hiện lại chữ `hashed` thì đã xác định xong nguyên nhân.

---

## 2. CASE 3 – CTE dùng 2 lần, điều kiện lọc trên cột CTE (`sql/case3_update.sql`)

**Kết quả** (`TIMING OFF`): PG13 0.23 s, PG17 16.7 s, **≈ 72×**. Cả hai **đã ANALYZE**. (Lần chụp plan dưới đây bật timing: 0.57 s và 20.6 s.)

### 2.1 Câu query
```sql
WITH open_orders AS MATERIALIZED (                      -- 10 000 đơn OPEN (0.5% của 2M)
  SELECT id, customer_id, cust_code, amount, priority, created_at
  FROM orders2 WHERE status = 'OPEN'
)
UPDATE customer_stats2 cs SET pair_cnt = s.n, open_amount = s.total, updated_at = now()
FROM (
  SELECT a.customer_id, count(*) n, sum(a.amount) total
  FROM open_orders a
  JOIN open_orders b ON b.customer_id = a.customer_id AND b.id <> a.id     -- CTE dùng 2 lần
  JOIN customers2 cu ON cu.name = a.cust_code
  WHERE a.priority >= 8 AND a.created_at >= '2026-10-01' AND a.amount >= 900  -- lọc NGOÀI CTE
    AND b.priority >= 8 AND b.created_at >= '2026-10-01' AND b.amount >= 900
  GROUP BY a.customer_id) s
WHERE cs.customer_id = s.customer_id;
```
Đặc điểm dữ liệu: **mọi** đơn OPEN đều mới, ưu tiên cao, giá trị lớn (tương quan 100%). Trên **toàn bảng** mỗi đặc điểm chỉ chiếm khoảng 1%.

### 2.2 Plan PG13 — 0.57 s (`results/case3_pg13.txt`)
```
Nested Loop  (rows=170) (actual rows=500 loops=1)
  ->  GroupAggregate  (rows=170) (actual rows=500 loops=1)
        ->  Merge Join  (rows=692) (actual time=506.346..540.078 rows=190000 loops=1)           [C]
              Merge Cond: (a.customer_id = b.customer_id)
              Join Filter: (b.id <> a.id)
              Rows Removed by Join Filter: 10000
              ->  Sort -> Hash Join  (rows=373) (actual rows=10000 loops=1)                    [B]
                       Hash Cond: (a.cust_code = cu.name)
                       ->  CTE Scan on open_orders a  (rows=373) (actual rows=10000 loops=1)   [A]
                             Filter: ((priority >= 8) AND (created_at >= …) AND (amount >= 900))
                       ->  Hash -> Seq Scan on customers2 cu  (rows=500000) (actual rows=500000)
              ->  Sort -> CTE Scan on open_orders b  (rows=373) (actual rows=10000 loops=1)
  ->  Index Scan using customer_stats2_pkey on customer_stats2 cs  (actual rows=1 loops=500)
Execution Time: 566.402 ms
```
| Mốc | Diễn giải |
|---|---|
| **[A]** | 3 điều kiện trên cột CTE, mà PG13 **không có stats cho cột CTE**, nên mỗi điều kiện `>=` dùng mặc định **1/3**: 10 067 × (1/3)³ ≈ **373**. Thực tế 10 000 (sai 27×, nhưng vẫn là "hàng trăm"). |
| **[B]** | Hash Join với `customers2` qua `cust_code`, bình thường. |
| **[C]** | Với ~373 dòng mỗi bên, planner chọn **Merge Join** (sort hai phía rồi trộn). Mỗi phía `loops=1`: CTE `b` chỉ đọc **một lần**. 190k cặp ra trong ~35 ms. |

### 2.3 Plan PG17 — 20.6 s (`results/case3_pg17.txt`)
```
Nested Loop  (rows=1) (actual rows=500 loops=1)
  ->  GroupAggregate  (rows=1) (actual rows=500 loops=1)
        ->  Sort  (rows=1) (actual rows=190000 loops=1)
              ->  Nested Loop  (rows=1) (actual time=19.014..20438.410 rows=190000 loops=1)    [C]
                    Join Filter: ((b.id <> a.id) AND (a.customer_id = b.customer_id))
                    Rows Removed by Join Filter: 99810000                                     [D]
                    ->  Hash Join  (rows=1) (actual rows=10000 loops=1)                       [B]
                          Hash Cond: (cu.name = a.cust_code)
                          ->  Seq Scan on customers2 cu  (rows=500000) (actual rows=500000)
                          ->  Hash  (rows=1) (actual rows=10000)  Buckets: 16384 (originally 1024)
                                ->  CTE Scan on open_orders a  (rows=1) (actual rows=10000 loops=1)   [A]
                                      Filter: ((priority >= 8) AND (created_at >= …) AND (amount >= 900))
                    ->  CTE Scan on open_orders b  (rows=1) (actual time=0.001..1.500 rows=10000 loops=10000)   [E]
                          Filter: (… giống trên …)
  ->  Index Scan using customer_stats2_pkey on customer_stats2 cs  (actual rows=1 loops=500)
Execution Time: 20625.978 ms
```
| Mốc | Diễn giải |
|---|---|
| **[A]** | PG17 lấy stats **toàn bảng `orders2`** qua CTE: `priority >= 8` ≈ 0.75%, `created_at >= 2026-10-01` ≈ 0.5%, `amount >= 900` ≈ 10%. Tích ≈ 0.0004%, nên **ước lượng 1 dòng**. Nhưng CTE chỉ chứa đơn OPEN, và **100%** đơn OPEN thỏa cả 3 điều kiện: thực tế **10 000** (sai 10 000×). Stats toàn bảng **không áp dụng được** cho tập con đã lọc. |
| **[B]** | `Buckets: 16384 (originally 1024)`: bảng băm phải tự nới rộng lúc chạy, thêm một dấu hiệu ước lượng quá nhỏ. |
| **[C]** | Planner tin cả `a` lẫn `b` chỉ có 1 dòng, nên Nested Loop "1 × 1" là rẻ nhất. Nó còn bỏ luôn điều kiện join vào `Join Filter` (so sánh từng cặp) thay vì dùng hash/merge. |
| **[E]** | `loops=10000`: CTE `b` bị **quét lại 10 000 lần**, mỗi lần đọc và lọc 10 000 dòng (~1.5 ms). 10 000 × 1.5 ms ≈ 15–20 s. |
| **[D]** | `Rows Removed by Join Filter: 99 810 000` = 10 000 × 10 000 − 190 000: **100 triệu cặp** được so sánh để lấy 190k cặp đúng. Đây chính là chữ ký O(N×M). |

### 2.4 Ý nghĩa
- Hệ số tăng theo **bình phương** số dòng thật của CTE. CTE 10k dòng cho 72×, CTE 100k dòng sẽ thành hàng giờ.
- Cách sửa đã đo trên PG17: `open_orders AS NOT MATERIALIZED` cho **0.37 s** (planner thấy được index `orders2(customer_id)`). Bảng tạm + index + `ANALYZE` cũng hiệu quả. `SET enable_nestloop = off` cho 0.28 s nhưng **chỉ dùng để chẩn đoán**.
- Dấu hiệu nhận diện trên plan thật: `CTE Scan … rows=1` mà `actual rows` hàng nghìn trở lên, kèm `loops` lớn ở nhánh inner của `Nested Loop`.

---

## 3. CASE 2 – PG17 ngay sau `pg_upgrade`, CHƯA ANALYZE (`sql/case2_update_nostats.sql`)

**Kết quả:** PG13 4.7 s (cache nóng; lần chụp dưới đây cache lạnh nên 13.6 s). PG17 chưa có stats **> 900 s** (bị `statement_timeout` cắt). PG17 sau `VACUUM; REINDEX; ANALYZE` 6.0 s (lần chụp có timing, chạy song song: 12.2 s).

### 3.1 Plan PG13 (`results/case2_pg13.txt`)
```
GroupAggregate (rows=200) (actual rows=500)
  ->  Merge Join  (rows=77234008) (actual rows=32436838 loops=1)                 [C]
        Merge Cond: (oo.customer_id = hc.customer_id)
        ->  Sort -> Hash Join (rows=30999) (actual rows=32459 loops=1)           [B]
                    Hash Cond: (oo.cust_code = cu.name)
                    ->  CTE Scan on open_orders oo  (rows=30999) (actual rows=32459)          [A1]
                    ->  Hash -> Seq Scan on customers cu  (rows=500000)
        ->  Sort -> CTE Scan on hot_complaints hc  (rows=498300) (actual rows=499117 loops=1) [A2]
Execution Time: 13590.616 ms   (cache lạnh: I/O read ≈ 3 s; lần đo cache nóng: 4.7 s)
```
- **[A1]/[A2]**: hai CTE được ước lượng **đúng** (31k / 498k) vì bảng nền có stats từ PG13.
- **[C]**: Merge Join đọc mỗi phía **1 lần** (`loops=1`) và tạo 32M cặp. Ước lượng 77M, cùng bậc độ lớn.
- Phần `JIT: … Total 420 ms`: JIT được bật vì cost lớn, chi phí ~0.4 s, không đáng kể.

### 3.2 Plan PG17 chưa có stats (chỉ `EXPLAIN`: chạy ANALYZE mất > 15 phút)
Chụp ngay sau `pg_upgrade` (`pg_class.reltuples = -1`, `pg_stats` rỗng):
```
Nested Loop  (rows=1)
  ->  Subquery Scan on s -> HashAggregate (rows=1)
        ->  Nested Loop  (rows=1953)                                              [C]
              Join Filter: (oo.customer_id = hc.customer_id)
              ->  CTE Scan on hot_complaints hc  (rows=1)                         [A2]  thực tế 499 117
              ->  Hash Join  (rows=1953)                                          [D]
                    Hash Cond: (cu.name = oo.cust_code)
                    ->  Seq Scan on customers cu  (rows=390648)
                    ->  Hash -> CTE Scan on open_orders oo  (rows=1)              [A1]  thực tế 32 459
  ->  Index Scan using customer_stats_pkey on customer_stats cs  (rows=1)
CTE open_orders:   Bitmap Heap Scan on orders o  (rows=1)      … 4 điều kiện, không stats
CTE hot_complaints: Bitmap Heap Scan on complaints c (rows=1)   … 3 điều kiện, không stats
```
| Mốc | Diễn giải |
|---|---|
| **[A1]/[A2]** | Bảng nền **chưa có stats**, nên mỗi điều kiện `BETWEEN` dùng mặc định 0.5%, `LIKE` mặc định, tích lại ra **1 dòng** cho cả hai CTE (thực tế 32k và 499k). |
| **[D]** | Hash Join giữa `customers` (500k dòng, Seq Scan) và `open_orders`. |
| **[C]** | Vì `hot_complaints` "chỉ có 1 dòng", planner đặt nó làm **outer** của Nested Loop. Nhánh inner là **toàn bộ [D]** (Seq Scan 500k khách hàng + build hash), bị **chạy lại cho từng dòng** của `hot_complaints`. Thực tế: 499 117 lần × (quét 500k dòng ≈ 30–50 ms) ≈ **4–7 giờ**. Lần chạy bị cắt ở 15 phút. |

### 3.3 Plan PG17 sau `VACUUM; REINDEX DATABASE; ANALYZE` (`results/case2_pg17_after_analyze.txt`)
```
HashAggregate (rows=5852) (actual rows=500)
  ->  Hash Join  (rows=2939364) (actual rows=32436838 loops=1)
        Hash Cond: (hc.customer_id = oo.customer_id)
        ->  CTE Scan on hot_complaints hc  (rows=499700) (actual rows=499117 loops=1)
        ->  Hash -> Hash Join (rows=34423) (actual rows=32459 loops=1)
                    ->  Seq Scan on customers cu
                    ->  Hash -> CTE Scan on open_orders oo  (rows=34423) (actual rows=32459)
Execution Time: 12172.757 ms   (TIMING ON + chạy song song; lần đo TIMING OFF: 6.0 s)
```
- Có stats nên CTE được ước lượng đúng (34k / 500k), mọi node `loops=1`, plan tương đương PG13.
- Ước lượng join vẫn thấp 11× (2.9M so với 32M thực tế, xem 2.6.2 của tài liệu chính) nhưng **không đủ** để đổi plan.
- **Kết luận:** CASE 2 giải thích được các query chậm *ngay sau* upgrade và được sửa bằng ANALYZE. Dự án đã ANALYZE mà vẫn chậm, nên câu của dự án thuộc nhóm **CASE 3/4**.

---

## 4. CASE 1 – đối chứng (không phải regression)

Chỉ dùng để minh họa công thức thu nhỏ `n_distinct` của PG17 khi tập con CTE **tập trung** vào ít khóa:

| | Ước lượng join | Ước lượng số nhóm | Thực tế | Plan | Thời gian |
|---|---|---|---|---|---|
| PG13 | 77M | 200 (mặc định) | 80M dòng / 500 nhóm | Hash Join | 24.5 s |
| PG17 | 5.9M | 6 897 | 80M dòng / 500 nhóm | Hash Join | 15.0 s |

Plan giống nhau nên không chậm, nhưng ước lượng PG17 sai theo hướng khác hẳn PG13. Ở query lớn hơn, sai lệch kiểu này có thể đổi thứ tự join.

---

## 5. Bảng tra nhanh: dấu hiệu trên plan → case → cách thử

| Dấu hiệu trên plan PG17 (so với PG13) | Case | Thử ngay (trong `BEGIN … ROLLBACK`) | Sửa bền vững |
|---|---|---|---|
| `Filter: (NOT (ANY (… = (SubPlan N).col1)))` **không có `hashed`**, node dưới SubPlan `loops` = số dòng ngoài | **★ 4** | `SET LOCAL work_mem='64MB';` | `NOT IN` thành `NOT EXISTS` (chú ý NULL) |
| `Nested Loop` + `CTE Scan … rows=1` (actual hàng nghìn+), nhánh inner `loops` lớn, `Rows Removed by Join Filter` khổng lồ | 3 | `SET LOCAL enable_nestloop=off;` (chẩn đoán) | `NOT MATERIALIZED`, hoặc bảng tạm + index + `ANALYZE`, hoặc đưa điều kiện lọc vào trong CTE |
| Mọi node đều `rows=1`, `pg_stats` rỗng | 2 | `ANALYZE <bảng>;` | `vacuumdb --analyze-in-stages` sau upgrade |
| Ước lượng `rows=` của `CTE Scan … Filter:` khác hẳn giữa PG13 và PG17 | dấu vết commit `f7816aec2` | `AS NOT MATERIALIZED` để so | Viết lại tránh lọc trên cột CTE materialized |
