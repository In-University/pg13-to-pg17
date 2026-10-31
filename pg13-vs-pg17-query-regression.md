# PostgreSQL 13 → 17 (Cloud SQL, in-place upgrade): vì sao cùng một query chậm hơn hàng chục lần

> Bối cảnh: câu `UPDATE` dùng CTE + `MATERIALIZED` + join nhiều bảng chạy ~2 phút trên PG13 nhưng ~2 giờ trên PG17.
> Tài liệu này gồm: (1) nguyên nhân khả dĩ nhất xếp theo xác suất, (2) khác biệt planner/executor/config giữa PG14–PG17, (3) quy trình chẩn đoán, (4) cách xử lý, (5) checklist cho đợt upgrade.
>
> **Cập nhật (rev 2):** phía dự án đã chạy `VACUUM` → `REINDEX` → `ANALYZE` sau upgrade mà query vẫn chậm. Vì vậy giả thuyết "mất thống kê sau `pg_upgrade`" **không còn là nguyên nhân chính** (xem Mục 1: chuyển thành danh sách *xác minh lại*). Trọng tâm mới là: **với stats đầy đủ, vì sao planner/executor của PG17 vẫn chọn plan tệ hơn** (Mục 0, 2, 2.6, 4).
>
> **Độ tin cậy:** các mục có link nguồn là đã đối chiếu; các mục còn lại là kiến thức từ release notes PG14–17 — hãy đối chiếu lại bằng [release notes chính thức](https://www.postgresql.org/docs/17/release-17.html) và bằng `EXPLAIN` thực tế trên hệ thống của bạn trước khi kết luận.

---

## 0. TL;DR — thứ tự nghi phạm

> Điều kiện đã **loại trừ** (theo phản hồi từ dự án): `VACUUM`, `REINDEX`, `ANALYZE` đều đã chạy sau upgrade mà query vẫn chậm.
> Hệ quả: bloat, index hỏng/collation, visibility map, thiếu stats "thô" **không còn là nghi phạm chính** — nhưng vẫn cần *xác minh* `ANALYZE` thực sự thành công trên mọi bảng (Mục 1.5).

| # | Nghi phạm (khi stats đã đầy đủ) | Khả năng | Cách kiểm tra nhanh |
|---|-----------|----------|---------------------|
| 1 | **Planner PG17 chọn plan khác do thay đổi logic**, đặc biệt là việc **lấy stats của bảng gốc qua CTE** (PG17, Mục 2.6). Hệ quả: `NOT IN` mất `hashed SubPlan` (**đã tái hiện: ≈365×, CASE 4 – đúng mẫu câu của dự án**), hoặc Nested Loop quét lại CTE (**đã tái hiện: ≈72×, CASE 3**). Các thay đổi khác: Memoize (PG14+), `GROUP BY` reorder, Right Anti Join… | **Cao** | So sánh `EXPLAIN` hai phiên bản, tìm node **đầu tiên** phân kỳ (Mục 4.3, 7.6) |
| 2 | **Ước lượng vẫn sai dù đã ANALYZE** (tương quan cột, skew, điều kiện trên expression/CTE đã aggregate) mà PG13 "may mắn" chọn đúng; PG17 (cost/estimation mới) chọn Nested Loop trên `CTE Scan`/`Materialize` | **Cao** | `rows=` vs `actual rows=` lệch ≥100×; `loops` rất lớn ở inner side |
| 3 | **Memoize** chọn sai do `n_distinct` ước lượng sai → cache miss, tốn CPU/RAM | Trung bình | `SET enable_memoize = off;` |
| 4 | **Chênh lệch cấu hình** sau upgrade (flag Cloud SQL, `work_mem`, `hash_mem_multiplier` 1.0→2.0 mặc định PG15, `random_page_cost`, `jit`, parallel…) | Trung bình | Diff `pg_settings` hai bản (Mục 3) |
| 5 | **JIT** kích hoạt do cost lớn | Thấp–TB | `SET jit = off;` |
| 6 | **Không phải plan, mà là chờ/IO**: lock wait, autovacuum chặn, WAL/disk throttle, HA/replica sync, trigger/FK, checkpoint | Trung bình (hay bị bỏ sót với UPDATE dài) | `pg_stat_activity.wait_event`, `pg_stat_io` (PG16+), thời gian trigger trong `EXPLAIN ANALYZE` (Mục 2.6, 4.2) |
| 7 | Regression riêng của PG17 (B-tree `IN (...)`/SAOP, …) | Thấp | `enable_group_by_reordering=off`, so plan index scan |
| 8 | Thứ nghi vấn khác: sample `ANALYZE` khác nhau làm plan "lật" ở biên, extension chưa update, hàm user-defined (volatility/COST/ROWS/parallel safety) | Thấp | Tăng stats target; `ALTER EXTENSION … UPDATE`; xem Mục 2.6 |

**Kết luận sơ bộ (rev 2):** 2 phút → 2 giờ (~60x) vẫn là dấu hiệu của **plan thay đổi cấu trúc** (vd. nested loop lặp lại trên tập lớn không có index) *hoặc* của **thời gian chờ** (lock/IO), chứ không phải do thực thi thuần chậm hơn. Vì stats đã có, bước tiếp theo bắt buộc là **lấy plan thật của PG17 (và PG13) và so sánh node-by-node**; mọi suy đoán chung chung chỉ có giá trị đến đó.

---

## 1. (Đã xử lý) `pg_upgrade` không mang theo thống kê — chỉ cần xác minh lại

> Mục này giữ lại làm kiến thức nền. Dự án đã `VACUUM` + `REINDEX` + `ANALYZE`, nên đây **không còn là nguyên nhân chính**; xem Mục 1.5 để chắc chắn bước ANALYZE không bị "thủng".

### 1.1 Cơ chế
- Cloud SQL "in-place major version upgrade" thực chất dùng `pg_upgrade`. `pg_upgrade` chuyển **schema + data file**, **không** chuyển nội dung `pg_statistic` / `pg_statistic_ext_data` (thống kê của optimizer). Chỉ đến **PG18** `pg_upgrade` mới giữ lại optimizer statistics.
  Các hướng dẫn upgrade (kể cả cho Cloud SQL, GitLab, CloudNativePG) đều nói rõ phải `ANALYZE` lại sau khi nâng cấp: [GitLab – upgrading external PostgreSQL](https://docs.gitlab.com/administration/postgresql/external_upgrade), [CloudNativePG – upgrades](https://cloudnative-pg.io/documentation/current/postgres_upgrades).
- Từ PG14, bảng chưa từng được analyze/vacuum có `pg_class.reltuples = -1` ("chưa biết"). Khi đó planner dùng **heuristic mặc định**: ước lượng số trang dựa trên kích thước file, selectivity mặc định (vd. `col = const` ≈ 0.5%, range ≈ 0.33%, `LIKE`/join… dựa hằng số), `n_distinct` mặc định (200). Với bảng lớn, các ước lượng này thường **thấp hơn thực tế vài bậc**.
- **Autovacuum sẽ không cứu bạn** với bảng ít thay đổi: autovacuum analyze được kích hoạt bởi `n_mod_since_analyze` (đếm số dòng thay đổi). Sau upgrade bộ đếm = 0 → bảng "nguội" (ít ghi) có thể **không bao giờ** được analyze tự động. Bảng lớn thì ngưỡng `autovacuum_analyze_scale_factor = 0.1` (10% số dòng) cũng rất lâu mới tới.
- Dữ liệu **extended statistics** (`CREATE STATISTICS`): *định nghĩa* được dump/restore, nhưng *dữ liệu* thống kê phải chờ `ANALYZE`.
- Các view `pg_stat_*` (counter) cũng bị reset → `pg_stat_user_tables.last_analyze/last_autoanalyze`, `n_dead_tup` không đáng tin ngay sau upgrade.

### 1.2 Vì sao hậu quả lại là 2 phút → 2 giờ với query của bạn
Query có CTE `MATERIALIZED` + nhiều join:
1. Kết quả CTE materialize ra một "bảng tạm" **không có index và không có thống kê thật** (PG chỉ ước lượng rows từ plan con).
2. Nếu bảng nền chưa có stats → ước lượng rows của CTE bị thấp (vd. `rows=1…100`).
3. Planner chọn **Nested Loop** với inner side là `CTE Scan`/`Materialize` (rẻ khi rows nhỏ).
4. Thực tế CTE trả ra hàng triệu dòng → nested loop quét lại CTE `loops = N` lần → độ phức tạp O(N×M) → vài phút thành vài giờ.
5. Với `UPDATE`, phần đọc/join diễn ra trước khi ghi, nên toàn bộ thời gian bị kéo dài bởi phần join.

Dấu hiệu trong `EXPLAIN (ANALYZE)`: `Nested Loop (rows=1 ...) (actual rows=… loops=…)` với `CTE Scan`/`Materialize` ở nhánh inner, `Rows Removed by Join Filter` cực lớn, hoặc `Seq Scan` lặp lại.

### 1.3 Cách xử lý chuẩn
```bash
# Cách khuyến nghị sau pg_upgrade: ANALYZE theo giai đoạn (có stats thô rất nhanh, rồi chính xác dần)
vacuumdb --all --analyze-in-stages --jobs=8 -h <host> -U postgres
# hoặc nếu chỉ muốn một lần chính xác ngay:
vacuumdb --all --analyze-only --jobs=8 -h <host> -U postgres
```
Chạy qua Cloud SQL Auth Proxy hoặc private IP. Hoặc từ psql, trong từng database: `ANALYZE VERBOSE;`.

Kiểm tra bảng nào chưa có stats:
```sql
SELECT n.nspname, c.relname, c.reltuples, c.relpages,
       s.last_analyze, s.last_autoanalyze
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
WHERE c.relkind IN ('r','m','p')
  AND n.nspname NOT IN ('pg_catalog','information_schema')
  AND (c.reltuples < 0 OR (s.last_analyze IS NULL AND s.last_autoanalyze IS NULL))
ORDER BY c.relpages DESC;
```

### 1.4 Bẫy mới ở PG17: ANALYZE có thể **thất bại** với expression index
Từ PG17, các thao tác bảo trì (`ANALYZE`, `VACUUM`, `CREATE INDEX`, `REINDEX`, `CLUSTER`, `REFRESH MATERIALIZED VIEW`, kể cả autovacuum) chạy với **`search_path` an toàn** (chỉ `pg_catalog`, `pg_temp`). Nếu có **expression index** hoặc **index predicate** gọi hàm tự định nghĩa mà **không schema-qualified** (hoặc hàm không tự đặt `search_path`), `ANALYZE` sẽ **báo lỗi** ("function … does not exist") → bảng đó **không bao giờ có stats** mà bạn có thể không để ý nếu chỉ nhìn tổng thể.
→ Hãy đọc log của `ANALYZE VERBOSE` và grep lỗi; sửa hàm bằng `ALTER FUNCTION … SET search_path = …` hoặc qualify schema.

### 1.5 Xác minh ANALYZE đã thực sự "phủ" hết (dự án đã chạy, nhưng cần chắc chắn)
`VACUUM` → `REINDEX` → `ANALYZE` đã chạy **không có nghĩa** mọi bảng đều có stats. Các lỗ hổng hay gặp:

| Kiểm tra | Vì sao | Câu lệnh |
|---|---|---|
| Từng bảng trong query có `last_analyze` sau thời điểm upgrade | `ANALYZE` chạy nhầm DB/schema, hoặc chạy bằng user không có quyền trên một số bảng (bị bỏ qua kèm WARNING) | `SELECT relname, last_analyze, last_autoanalyze FROM pg_stat_user_tables WHERE relname IN (...);` |
| **Bảng partitioned (bảng cha)** | Autovacuum **không bao giờ** analyze bảng cha của partition. Một `ANALYZE` không tham số có xử lý bảng cha, nhưng `ANALYZE <partition>` hoặc `vacuumdb -t <partition>` thì không | `SELECT c.relname, s.n_distinct IS NOT NULL AS has_stats FROM pg_class c LEFT JOIN pg_stats s ON s.tablename=c.relname WHERE c.relkind='p';` |
| Expression index / index predicate gọi hàm không schema-qualified | PG17: `ANALYZE` chạy với safe `search_path` nên **lỗi**, bảng không có stats (Mục 1.4) | Chạy `ANALYZE VERBOSE <bảng>;` từng bảng và đọc lỗi |
| `default_statistics_target` và per-column `SET STATISTICS` | Flag Cloud SQL có thể khác PG13; `ALTER COLUMN … SET STATISTICS` được giữ qua pg_upgrade nhưng flag instance thì không chắc | `SHOW default_statistics_target;` `SELECT attname, attstattarget FROM pg_attribute WHERE attrelid='t'::regclass AND attnum>0;` |
| Extended statistics (`CREATE STATISTICS`) có dữ liệu | Định nghĩa được giữ, dữ liệu phải ANALYZE lại | `SELECT statistics_name, n_distinct IS NOT NULL, dependencies IS NOT NULL FROM pg_stats_ext;` |
| Bảng tạm / unlogged / bảng tạo trong batch | Autovacuum không analyze bảng tạm; nếu job tự tạo bảng trung gian rồi UPDATE ngay → không có stats ở **cả hai** phiên bản, nhưng plan mặc định có thể khác | Thêm `ANALYZE <bảng_tạm>` ngay sau khi nạp |

---

## 2. Các thay đổi planner / executor giữa PG13 và PG17

> Lưu ý: CTE inlining (non-recursive, không side-effect, tham chiếu 1 lần) và từ khóa `MATERIALIZED`/`NOT MATERIALIZED` đã có từ **PG12**, nên PG13 đã có hành vi này. `MATERIALIZED` của bạn **vẫn được tôn trọng ở PG17**; điều thay đổi là cách planner *dùng thông tin* từ CTE.

### 2.1 PG14
| Thay đổi | Ảnh hưởng tiềm năng |
|---|---|
| **Memoize** (trước gọi Result Cache), GUC `enable_memoize` (mặc định on) | Nested loop có thể thêm node `Memoize` ở inner side. Nếu `n_distinct` bị ước lượng sai (đặc biệt khi chưa có stats) → hit ratio kém, tốn RAM, hoặc planner chọn nested loop vì tin rằng cache hiệu quả. Cộng đồng đã ghi nhận các case phải tắt (ví dụ [thread pgsql-general](https://www.postgresql.org/message-id/CAApHDvqJK%2BARTYLYeS-C1i_seVbcp0nfCVbxu960nGpssFJA%2Bw%40mail.gmail.com)). |
| **Viết lại cách plan `UPDATE`/`DELETE` trên bảng partition/inheritance** | Cấu trúc plan của UPDATE khác (đọc 1 lần + junk `tableoid`/`ctid`). Thường nhanh hơn, nhưng plan shape khác PG13, cần so sánh. |
| Extended statistics cho **biểu thức** và cho điều kiện `OR`/`IN`/`ANY` | Ước lượng thay đổi (thường tốt hơn) → join order có thể đổi. |
| `reltuples = -1` cho bảng chưa analyze | Xem mục 1. |
| Cải thiện parallel query, `enable_async_append` (FDW), bottom-up index deletion (B-tree), vacuum failsafe | Plan song song khác; index bloat từ UPDATE giảm. |
| Hash/`Hash Join` & window `run condition`, các tối ưu nhỏ cho `LATERAL` | Thay đổi nhỏ về plan. |

### 2.2 PG15
| Thay đổi | Ảnh hưởng |
|---|---|
| **`hash_mem_multiplier` mặc định 1.0 → 2.0** | Hash join/hash agg được phép dùng gấp đôi `work_mem` → ít spill hơn, **nhưng** cost model coi hash rẻ hơn → planner thiên về Hash Join/HashAgg hơn. Nếu Cloud SQL giữ giá trị cũ/đặt tay, chú ý hai bên khác nhau. |
| Sort nhanh hơn (specialized sort cho nhiều kiểu dữ liệu), parallel `SELECT DISTINCT` | Cost sort thay đổi nhẹ. |
| Statistics collector chuyển sang **shared memory** (bỏ `stats_temp_directory`) | Không ảnh hưởng plan, nhưng `pg_stat_*` hành xử khác. |
| `MERGE`; bỏ quyền `CREATE` trên schema `public` với mọi user | Có thể gây lỗi quyền, không gây chậm. |

### 2.3 PG16
| Thay đổi | Ảnh hưởng |
|---|---|
| **Right Anti Join**, **Parallel Hash Full/Right Join** | `NOT EXISTS`/`NOT IN`/anti-join có thể chọn plan khác (hash phía bên kia). |
| `enable_presorted_aggregate`, incremental sort cho `DISTINCT`, tối ưu aggregate `ORDER BY` | Plan aggregate đổi. |
| `EXPLAIN (GENERIC_PLAN)`, `pg_stat_io` | Công cụ chẩn đoán mới. *(Đính chính rev 2: hàm `pg_log_query_plan()` mà bản trước nhắc tới **không có** trong PG16/17 — đó là patch chưa được merge; đã kiểm tra trực tiếp trên PG 17.11.)* |
| `vacuum_buffer_usage_limit`, `pg_stat_io` | Chủ yếu vận hành. |
| Tối ưu Memoize/Append, parallel string_agg/array_agg | Plan đổi nhẹ. |

### 2.4 PG17
| Thay đổi | Ảnh hưởng |
|---|---|
| **Planner dùng *statistics* và *sort order* của cột tham chiếu bởi outer query để tối ưu plan CTE** ([release notes](https://postgresql.org/docs/current/release-17.html): "improve CTE plans by considering the statistics and sort order of columns referenced in earlier row output clauses") | Trực tiếp liên quan query của bạn (CTE + join). Join order/phương thức join/việc có sort hay không có thể khác PG13. Thường tốt khi stats đầy đủ, nhưng nếu stats thiếu/sai → hậu quả khuếch đại. |
| **`GROUP BY` reordering** (`enable_group_by_reordering`, default on) ([doc](https://postgresqlco.nf/doc/en/param/enable_group_by_reordering/)) | Planner có thể đổi thứ tự cột GROUP BY để khớp sort order của child node/index → thay đổi plan aggregate. Thử `SET enable_group_by_reordering = off`. |
| **B-tree xử lý `IN (list)`/`= ANY(array)` (SAOP) tốt hơn** (một lần scan cho nhiều giá trị) | Thường nhanh hơn, nhưng đã có **báo cáo regression** (index-only scan mất hiệu quả/heap fetch nhiều, sort lại mảng SAOP): [thread 1](https://www.postgresql.org/message-id/2CB274CF-6282-48F9-988F-59F16E1D0EEC@blackduck.com), [thread 2](https://www.postgresql.org/message-id/4A6CAA9C-E2A2-46EB-BBF6-E33256DDEAF3%40blackduck.com). Nếu query có `IN (…)` lớn, cần kiểm tra. |
| Tối ưu `IS [NOT] NULL` trên cột `NOT NULL`; subquery `IN` tương quan được chuyển thành join | Plan đổi (thường tốt). |
| **Vacuum dùng TidStore** (không còn trần 1GB `maintenance_work_mem` cho dead tuples), `autovacuum_work_mem` | Vận hành vacuum; không ảnh hưởng plan. |
| **Streaming I/O (read stream)** cho seq scan & ANALYZE, `io_combine_limit` | Thay đổi pattern I/O; thường có lợi, nhưng hiệu quả phụ thuộc storage. |
| Bảo trì chạy với **safe `search_path`** | Xem 1.4 (ANALYZE có thể fail). |
| Bỏ `old_snapshot_threshold`; `pg_stat_bgwriter` tách `pg_stat_checkpointer`; `pg_stat_statements` thêm cột mới | Công cụ monitoring cũ có thể hỏng. |
| `JSON_TABLE`, `MERGE ... RETURNING`, `MAINTAIN` privilege | Tính năng mới. |

### 2.5 Các điểm xuyên suốt cần để ý
- **JIT** (`jit = on` mặc định từ PG12; `jit_above_cost = 100000`). Nếu stats sai làm cost lớn → JIT kích hoạt; chi phí compile LLVM cho query rất nhiều biểu thức có thể lên hàng chục giây–phút, cộng thêm vào. Tắt thử: `SET jit = off`.
- **Hash aggregate spill ra đĩa** đã có từ PG13 nên chi phí tương tự; nhưng thay đổi mặc định `hash_mem_multiplier` ở PG15 làm cost model khác.
- **GEQO / join_collapse_limit**: `join_collapse_limit = 8`, `from_collapse_limit = 8`, `geqo_threshold = 12` không đổi, nhưng nếu CTE được inline khác trước, số "relation" trong một join problem có thể vượt ngưỡng → thứ tự join do GEQO (ngẫu nhiên) hoặc theo thứ tự viết.

### 2.6 Khi stats đầy đủ vẫn chậm: PG17 "nhìn xuyên" vào CTE

Đây là thay đổi **liên quan trực tiếp nhất** tới query dạng *CTE MATERIALIZED + join + UPDATE*, và là cơ chế tôi **tái hiện được trong lab** sau khi đã `VACUUM`/`REINDEX`/`ANALYZE` (Mục 7).

#### 2.6.1 Hai commit của PG17
| Commit | Nội dung | Nguồn |
|---|---|---|
| `f7816aec2` "Extract column statistics from CTE references, if possible" (Tom Lane, Jian Guo) | Khi query ngoài tham chiếu một cột của CTE đã materialize, planner **đi xuyên vào subquery của CTE**; nếu cột đó là một **Var trơn** (cột của bảng, không phải biểu thức/aggregate) thì dùng `pg_statistic` của **cột bảng gốc** (n_distinct, MCV, histogram) | [pgsql-hackers thread](https://www.postgresql.org/message-id/flat/4504e67078d648cdac3651b2960da6e7%40nidsa.net), [BUG #18466](https://www.postgresql.org/message-id/flat/18466-1d296028273322e2%40postgresql.org), [pganalyze](https://pganalyze.com/blog/5mins-postgres-17-materialized-cte-scans) |
| `a65724dfa` "Propagate pathkeys from CTEs up to the outer query" (Richard Guo, Tom Lane) | Thứ tự sắp xếp (ORDER BY) của kết quả CTE được truyền lên → query ngoài có thể chọn Merge Join mà không cần Sort | [thread](https://www.postgresql.org/message-id/flat/CAMbWs49xYd3f8CrE8-WW3--dV1zH_sDSDn-vs2DzHj81Wcnsew%40mail.gmail.com), [pganalyze](https://pganalyze.com/blog/5mins-postgres-17-materialized-cte-scans) |

Hai commit này **không** được backport về PG16 trở xuống. Mục đích ban đầu là *sửa* các ước lượng tệ: thread pgsql-hackers ở trên là một query PG 15.1 chậm ~60× (224 ms so với 13.4 s) vì planner dùng `n_distinct` mặc định = 200 cho cột CTE và chọn Nested Loop với `Rows Removed by Join Filter: 88552397`.

#### 2.6.2 Vì sao "sửa ước lượng" lại có thể làm query **chậm đi**
- PG13 (và PG16): cột của `CTE Scan` **không có stats** → dùng hằng số mặc định: `=` → 0.5%, `>=`/`<` → 33.3%, `n_distinct` = 200. Các hằng số này **"trung tính"**: thường sai, nhưng hiếm khi sai tới mức cực đoan.
- PG17: dùng stats của **bảng gốc TOÀN BỘ, chưa áp điều kiện `WHERE` bên trong CTE**. Nếu CTE lọc ra một **tập con có phân bố khác hẳn bảng gốc** (dữ liệu lệch/tương quan — rất hay gặp: "đơn OPEN đều là đơn mới", "brand hiện hành chỉ ở vài vùng"…), stats bảng gốc sẽ **sai một cách có hệ thống** cho tập con đó:
  - Điều kiện ngoài CTE như `cte.created_at >= gần đây`: bảng gốc nói "1%", nhưng trong CTE là 100% → **ước lượng thấp** → Nested Loop quét lại CTE (CASE 3).
  - Điều kiện `cte.region = 'VN'`: bảng gốc nói "40%", PG13 đoán 0.5% → PG17 ước lượng **cao hơn** → vượt ngưỡng bộ nhớ của **hashed SubPlan** → `NOT IN` chuyển sang quét tuyến tính O(N×M) (CASE 4, mẫu câu của dự án).
  - Join trên cột CTE: `n_distinct` của bảng gốc được thu nhỏ theo công thức xác suất (giả định tập con là **ngẫu nhiên**): `nd' = nd × (1 − (1 − rows_cte/rows_bảng)^(rows_bảng/nd))`. Nếu tập con thực ra **tập trung** vào ít khóa, `nd'` bị thổi phồng → ước lượng join thấp (CASE 1: PG17 ước 5.9M dòng/6.9k nhóm, thực tế 80M/500).
- Tóm lại: PG17 **chính xác hơn khi tập con của CTE "giống" bảng gốc**, và **có thể tệ hơn PG13** khi tập con lệch mạnh. Câu query của bạn chạy nhanh trên PG13 có thể chỉ nhờ "may mắn" của hằng số mặc định. Trong lab, khi ép CTE inline (`NOT MATERIALIZED`), **PG13 cũng chọn đúng plan xấu đó** (Mục 7, CASE 4).

#### 2.6.3 Đặc biệt với `NOT IN (subquery)` — "hashed SubPlan" vs "SubPlan"
- `NOT IN (SELECT …)` **không** được chuyển thành anti-join (khác với `NOT EXISTS`) ở cả PG13 lẫn PG17. Planner chỉ có hai lựa chọn:
  - `hashed SubPlan`: chạy subquery 1 lần, đưa vào bảng băm → mỗi dòng ngoài tra O(1). Nhanh.
  - `SubPlan` thường: mỗi dòng ngoài **quét tuyến tính** kết quả subquery (dòng không khớp phải quét **hết** danh sách) → O(N×M). Rất chậm.
- Planner chỉ chọn hashed nếu **ước lượng** `rows × (width + ~24 byte)` ≤ `work_mem × hash_mem_multiplier` (PG13: ×1.0, PG17: ×2.0 — đã kiểm tra trên container). Lúc chạy, bảng băm vẫn có thể lớn hơn mức đó; quyết định chỉ dựa trên **ước lượng**.
- Vì vậy nếu PG17 ước lượng subquery **lớn hơn** PG13 (do stats propagate, hoặc do join estimate thay đổi), câu `NOT IN` có thể rơi từ hashed sang tuyến tính → chậm hàng trăm lần **dù stats đầy đủ**.
- Nhận diện trong `EXPLAIN`: PG13 `Filter: (NOT (hashed SubPlan 2))`; PG17 `Filter: (NOT (ANY (brand_code = (hashed SubPlan 2).col1)))` là tốt; **không có chữ `hashed`** là xấu.

#### 2.6.4 Nguyên nhân KHÔNG thuộc về plan (UPDATE chạy lâu vì *chờ*)
| Nguyên nhân | Dấu hiệu | Kiểm tra |
|---|---|---|
| Lock wait (UPDATE chờ transaction khác/`autovacuum` wraparound/DDL) | `wait_event_type = 'Lock'` | `SELECT pid, wait_event_type, wait_event, pg_blocking_pids(pid), query FROM pg_stat_activity WHERE state <> 'idle';` |
| WAL / IO throttle (disk Cloud SQL hết IOPS/throughput, checkpoint dồn) | `wait_event` dạng `WALWrite`, `WALSync`, `DataFileRead`; `EXPLAIN (ANALYZE, BUFFERS, WAL)` cho WAL bytes lớn | `pg_stat_io` (PG16+), `pg_stat_wal`, Cloud Monitoring (disk read/write ops, throughput) |
| HA / sync replication (Enterprise Plus, `synchronous_commit`) | `wait_event = 'SyncRep'` | `pg_stat_replication`, `SHOW synchronous_commit;` |
| Trigger / FK | `EXPLAIN ANALYZE` in `Trigger … time=… calls=…` lớn | Xem phần cuối output của `EXPLAIN ANALYZE` |
| Index cần cập nhật (UPDATE không HOT) | WAL lớn, nhiều `Buffers: dirtied` | `pg_stat_user_tables.n_tup_hot_upd` so với `n_tup_upd` |

Nếu `wait_event` gần như luôn NULL (đang dùng CPU) suốt 2 giờ → gần như chắc chắn là **plan**, quay về 2.6.1–2.6.3.

#### 2.6.5 Nguồn cần lưu ý
- [boringSQL – Good CTE, bad CTE](https://boringsql.com/posts/good-cte-bad-cte/): tổng hợp hành vi CTE qua các phiên bản (PG17+: "Materialized (better stats)"), khuyên dùng bảng tạm + index + ANALYZE cho kết quả trung gian lớn.
- Bài Microsoft "When PostgreSQL v17 Chooses the Wrong Plan: A Deep Dive into CTEs with Data Skew" có tiêu đề **khớp đúng** cơ chế trên nhưng **tôi không đọc được nội dung** (trang chỉ trả về tiêu đề) → chưa kiểm chứng, không trích dẫn chi tiết.

---

## 3. Các yếu tố đặc thù Cloud SQL / hạ tầng cần loại trừ

1. **Database flags**: so sánh `pg_settings` giữa instance PG13 (hoặc bản clone trước upgrade) và PG17.
   ```sql
   SELECT name, setting, unit, source, boot_val
   FROM pg_settings
   WHERE source NOT IN ('default','override')
   ORDER BY name;
   ```
   Xuất ra 2 file và `diff`. Các flag cần quan tâm: `work_mem`, `hash_mem_multiplier`, `maintenance_work_mem`, `shared_buffers`, `effective_cache_size`, `random_page_cost`, `seq_page_cost`, `effective_io_concurrency`, `max_parallel_workers*`, `parallel_setup_cost`, `jit*`, `enable_*`, `default_statistics_target`, `join_collapse_limit`, `from_collapse_limit`, `autovacuum_*`. Flag bị loại bỏ/không hỗ trợ ở PG17 có thể đã bị Cloud SQL bỏ qua.
2. **Cache lạnh** (shared_buffers + OS page cache) sau upgrade/restart: chạy query hai lần; cache lạnh giải thích vài × chứ không phải 60×.
3. **Extension chưa cập nhật** sau major upgrade: `SELECT * FROM pg_extension;` rồi `ALTER EXTENSION <name> UPDATE;` (PostGIS, `pg_trgm`, `btree_gist`, `pg_stat_statements`…). Phiên bản extension cũ có thể thiếu cost/`PARALLEL` marker mới.
4. **Collation / glibc**: nếu image OS của Cloud SQL đổi phiên bản glibc, thứ tự sắp xếp của collation libc có thể đổi → index text/varchar có thể sai thứ tự (lỗi kết quả, đôi khi planner bỏ index). Kiểm tra bằng `amcheck`: `SELECT bt_index_check(c.oid) …` và `REINDEX` nếu cần. Ghi nhận đây là rủi ro đúng đắn, không chắc là nguyên nhân chậm.
5. **Replica / HA / Read pool**: sau upgrade replica được tạo lại → cache lạnh và stats không áp dụng trên replica (stats nằm trong DB, replicate từ primary sau khi ANALYZE).
6. **Cấu hình máy** (vCPU, RAM, disk type, IOPS) có thay đổi trong lúc upgrade không (Enterprise vs Enterprise Plus, data cache).
7. **Bloat**: `pg_upgrade` giữ nguyên layout vật lý → bloat không đổi. Nhưng nếu PG13 chạy autovacuum ổn mà PG17 thì tụt vì counter bị reset, hãy kiểm tra `n_dead_tup`.

---

## 4. Quy trình chẩn đoán đề xuất

### 4.1 Tạo môi trường so sánh sạch
- Clone instance PG13 (trước upgrade) → môi trường A.
- Clone/Restore bản đã upgrade lên PG17 → môi trường B.
- Chạy query trên cả hai với **cùng dữ liệu, cùng flag**, ghi lại plan.

### 4.2 Lấy plan khi query chạy quá lâu
```sql
-- UPDATE nên bọc trong transaction và rollback
BEGIN;
EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, WAL, TIMING OFF) UPDATE ...;
ROLLBACK;
```
Nếu chạy 2 giờ không thể chờ:
- `EXPLAIN (VERBOSE, SETTINGS)` (không ANALYZE) để so sánh *plan shape* với PG13.
- Query đang chạy: xem `pg_stat_activity` (`wait_event_type`, `wait_event`, `state`) và `pg_locks` để phân biệt **đang tính toán** (wait_event NULL, CPU cao) với **đang chờ** (Lock / IO / WAL) — xem 2.6.4.
- `SET statement_timeout = '10min';` + `auto_explain` (`auto_explain.log_analyze`, `log_buffers`, `log_nested_statements`) để lấy plan thực tế khi bị cắt.
- Thử trên tập dữ liệu thu nhỏ có cùng phân bố.

### 4.3 Đọc plan: tìm node đầu tiên có sai lệch ước lượng lớn
- So `rows=` (ước lượng) với `actual rows=`; sai lệch ≥ 10–100× là điểm nghi vấn.
- Chú ý: `Nested Loop` + `CTE Scan`/`Materialize`/`Seq Scan` ở inner; `loops` lớn; `Rows Removed by Filter/Join Filter`; `Memoize` với `Hits/Misses/Evictions`; `Batches` > 1 (spill) ở Hash; `Sort Method: external merge`; `JIT:` section (thời gian Generation/Optimization/Emission); `Heap Fetches` lớn ở Index Only Scan.
- Với UPDATE: xem thời gian **trigger** (`Trigger … time=`) – FK/trigger nội bộ có thể là điểm nghẽn riêng.

### 4.4 Bộ công tắc cách ly nguyên nhân (đặt bằng `SET LOCAL` trong transaction thử nghiệm)
```sql
BEGIN;
SET LOCAL jit = off;
SET LOCAL enable_memoize = off;
SET LOCAL enable_group_by_reordering = off;      -- PG17
-- Nếu nghi nested loop:
-- SET LOCAL enable_nestloop = off;
-- Nếu nghi spill:
-- SET LOCAL work_mem = '256MB'; SET LOCAL hash_mem_multiplier = 2;
EXPLAIN (ANALYZE, BUFFERS) UPDATE ...;
ROLLBACK;
```
Bật/tắt từng công tắc **sau khi đã ANALYZE** và ghi lại cái nào đổi plan/thời gian. Đừng dùng `enable_*` làm giải pháp lâu dài — chỉ để chẩn đoán.

### 4.5 So sánh workload toàn diện
- `pg_stat_statements` PG13 vs PG17: tìm *tất cả* query chậm đi, không chỉ một (lưu ý tên cột `total_exec_time`, PG17 thêm `shared_blk_read_time`/`local_blk_read_time`, `stats_since`).
- Nếu chỉ vài query chậm nhưng sau ANALYZE vẫn chậm → regression của planner thật sự → báo cáo lên `pgsql-performance` / `pgsql-bugs` kèm plan hai phiên bản.

---

## 5. Cách xử lý / giảm thiểu

### 5.1 Ngay lập tức
1. **Xác minh** stats đã đủ theo Mục 1.5 (dự án đã ANALYZE; bước này chỉ để loại trừ chắc chắn, không còn là cách sửa chính).
2. Lấy `EXPLAIN` của câu chậm trên PG17 và đối chiếu với các dấu hiệu ở Mục 7.6:
   - `NOT IN` mất `hashed SubPlan` (CASE 4) → viết lại thành `NOT EXISTS` (chú ý ngữ nghĩa NULL) hoặc tạm thời `SET work_mem` lớn hơn cho session batch.
   - `Nested Loop` + `CTE Scan … rows=1` lặp lại (CASE 3) → `NOT MATERIALIZED` hoặc bảng tạm + index + `ANALYZE` (5.2).
3. Với cột dùng làm join key/filter có phân bố lệch: `ALTER TABLE … ALTER COLUMN … SET STATISTICS 500|1000; ANALYZE …;`
4. Cột tương quan (nhiều điều kiện cùng bảng, join trên nhiều cột): `CREATE STATISTICS s (dependencies, ndistinct, mcv) ON a, b FROM t; ANALYZE t;`. Lưu ý: extended stats **không** áp dụng cho điều kiện đặt *bên ngoài* CTE materialized; với CASE 3, hãy chuyển điều kiện vào trong CTE hoặc dùng bảng tạm.

### 5.2 Tái cấu trúc query (bền vững nhất cho UPDATE lớn)
- **Thay CTE `MATERIALIZED` bằng bảng tạm + index + ANALYZE**:
  ```sql
  CREATE TEMP TABLE tmp_src AS SELECT ...;          -- thay thế CTE
  CREATE INDEX ON tmp_src (join_key);
  ANALYZE tmp_src;                                   -- cho planner stats thật
  UPDATE target t SET ... FROM tmp_src s WHERE t.id = s.id;
  ```
  Planner khi đó có stats thật và có thể dùng index/hash join thay vì scan CTE nhiều lần. Đây thường là thay đổi mang lại hiệu quả lớn nhất khi CTE trả ra nhiều dòng.
- Thử `NOT MATERIALIZED` nếu CTE chỉ tham chiếu một lần, hoặc ngược lại giữ `MATERIALIZED` nhưng bảo đảm số dòng nhỏ.
- Chia UPDATE thành batch theo khoảng khóa (giảm WAL, lock, và rủi ro plan xấu trên toàn bộ).
- Dùng `pg_hint_plan` nếu phiên bản Cloud SQL của bạn hỗ trợ (kiểm tra danh sách extension).

### 5.3 Cấu hình
- Đặt `work_mem`/`hash_mem_multiplier` phù hợp cho session chạy batch (`SET work_mem` theo session/role thay vì toàn instance).
- `jit = off` ở mức role/DB cho workload OLTP/batch nếu JIT không hữu ích.
- Cân nhắc `random_page_cost = 1.1` cho SSD (nếu chưa).
- Giữ autovacuum/analyze scale factor nhỏ hơn cho bảng lớn: `ALTER TABLE … SET (autovacuum_analyze_scale_factor = 0.02)`.

---

## 6. Checklist cho đợt upgrade (rút kinh nghiệm)

**Trước:**
- [ ] Thu thập baseline: `pg_stat_statements` top N theo `total_exec_time`; plan (`EXPLAIN`) + thời gian của các query quan trọng (kể cả query batch như UPDATE này).
- [ ] Dump `pg_settings` (non-default) và danh sách extension + version.
- [ ] Chạy thử upgrade trên **clone** và đo lại toàn bộ query then chốt.
- [ ] Kiểm tra `pg_upgrade --check`-tương đương (Cloud SQL "pre-check"), loại extension/feature không tương thích.
- [ ] Rà soát hàm dùng trong expression index/predicate/materialized view về `search_path` (PG17).

**Ngay sau:**
- [ ] `vacuumdb --all --analyze-in-stages` (hoặc `ANALYZE`) **trước khi mở traffic**; đọc log lỗi ANALYZE.
- [ ] `ALTER EXTENSION … UPDATE` cho mọi extension.
- [ ] Diff `pg_settings`.
- [ ] Chạy lại bộ query baseline, so sánh plan và thời gian; cảnh báo nếu lệch > 2×.
- [ ] Bật `auto_explain` (ngưỡng hợp lý) và `pg_stat_statements` để bắt query regressions sớm.
- [ ] `amcheck` / `REINDEX` nếu OS/glibc/collation có thể đã đổi.

---

## 7. Kết quả thực nghiệm trong lab (đo thực tế)

**Môi trường:** Docker trên Windows (8 vCPU, 8 GB), `postgres:13` (13.23) → `pg_upgrade` thật bằng `tianon/postgres-upgrade:13-to-17` → `postgres:17` (17.11). Hai instance dùng **cùng tham số** (`shared_buffers=1GB, work_mem=64MB, random_page_cost=1.1, …`); `hash_mem_multiplier` để mặc định của từng bản (PG13 = 1, PG17 = 2). Mỗi câu UPDATE chạy bằng `BEGIN; EXPLAIN (ANALYZE, TIMING OFF) …; ROLLBACK;`. Cách chạy lại: xem [`README.md`](README.md).
Dữ liệu là **dữ liệu tổng hợp** được thiết kế có phân bố lệch/tương quan. Lab chứng minh **cơ chế**, không khẳng định đây chính là nguyên nhân trong production; cần đối chiếu bằng plan thật (Mục 4).
Lưu ý độ chính xác: một số phép đo chạy song song với query khác trên cùng máy (CASE 2 timeout chạy cùng lúc với CASE 3/4; ngoài ra có một session lạ trên DB `pgdoctor_fx` của pg17 khi đo CASE 4). Thời gian tuyệt đối có thể lệch vài chục %, nhưng **hệ số hàng chục–hàng trăm lần đến từ khác biệt plan**, không phải do nhiễu.

> 📄 **Giải thích EXPLAIN từng dòng, từng case (plan đầy đủ PG13 vs PG17):** xem [`explain-walkthrough.md`](explain-walkthrough.md). Output nguyên văn: thư mục `results/`.

### 7.0 Tổng hợp

| Case | Trạng thái stats | PG13 | PG17 | Hệ số | Cơ chế |
|---|---|---|---|---|---|
| ★ **CASE 4: mẫu câu của dự án** (`CTE a NOT MATERIALIZED` + `current_brand_code` + `UPDATE … NOT IN (…)`) | **Đã ANALYZE cả hai** | **0.79 s** | **288.9 s** | **≈ 365×** | PG17 lấy stats bảng gốc qua CTE → ước lượng subquery 380k dòng (PG13: 4.7k) → bỏ `hashed SubPlan` → `NOT IN` quét tuyến tính |
| CASE 3: CTE dùng 2 lần + lọc trên cột CTE | **Đã ANALYZE cả hai** | 0.23 s | 16.7 s | ≈ 72× | PG17 ước lượng CTE còn 1 dòng (PG13: 373, thực tế 10k) → Nested Loop quét lại CTE 10k lần |
| CASE 2: 2 CTE MATERIALIZED + join nhiều bảng | PG17 **ngay sau pg_upgrade, chưa ANALYZE** | 4.7 s | **> 900 s** (bị cắt bởi `statement_timeout` 15 phút) | **> 190×** | Không có stats → mỗi CTE ước lượng 1 dòng → Nested Loop với nhánh inner (Seq Scan 500k khách hàng + hash) bị chạy lại cho từng dòng |
| CASE 2 sau `VACUUM; REINDEX; ANALYZE` | PG17 đã ANALYZE | 4.7 s | 6.0 s | ≈ 1.3× | Plan trở lại giống PG13 (đúng như dự án đã làm, nên đây **không** phải nguyên nhân của dự án) |
| CASE 1: CTE + join (đối chứng) | Đã ANALYZE cả hai | 24.5 s | 15.0 s | PG17 nhanh hơn | Plan tương đương; nhưng ước lượng khác nhau rõ (xem 7.4) |

### 7.1 ★ CASE 4 – mẫu câu của dự án (`NOT IN` + CTE)  — `sql/case4_setup.sql`, `sql/case4_update.sql`

**Câu query** (cùng "hình dạng" với câu của dự án):
```sql
SET work_mem = '4MB';   -- mặc định PostgreSQL / nhiều tier Cloud SQL
WITH a AS NOT MATERIALIZED (
  SELECT s.id, s.region FROM stores s WHERE s.active
), current_brand_code AS MATERIALIZED (            -- join nhiều bảng
  SELECT b.code AS brand_code, a.region
  FROM brand_assignments ba
  JOIN brands b ON b.id = ba.brand_id
  JOIN a        ON a.id = ba.store_id
  WHERE ba.is_current
)
UPDATE products p
SET status = 'ORPHAN', updated_at = now()
WHERE p.status = 'PENDING'
  AND p.brand_code NOT IN (
        SELECT cbc.brand_code FROM current_brand_code cbc WHERE cbc.region = 'VN');
```
Dữ liệu: 20k stores (40% ở `VN`), 5k brands, 2M `brand_assignments` (50% hiện hành), 200k products (5k `PENDING`). Kết quả CTE thực tế 1M dòng, trong đó 400k dòng `VN`.

**Plan đo được (rút gọn):**
```
PG13  Filter: (NOT (hashed SubPlan 2))
        ->  CTE Scan on current_brand_code cbc  (rows=4736)   (actual rows=400000 loops=1)
              Filter: (region = 'VN'::text)
      Execution Time: 790.875 ms

PG17  Filter: (NOT (ANY (brand_code = (SubPlan 2).col1)))          <-- không còn "hashed"
        ->  CTE Scan on current_brand_code cbc  (rows=379696) (actual rows=200252 loops=5000)
              Filter: (region = 'VN'::text)
      Execution Time: 288905.470 ms
```
**Giải thích từng bước:**
1. Cột `region` của CTE là một Var trơn lấy từ `stores.region` (qua CTE `a` đã inline).
2. **PG13:** cột của `CTE Scan` không có stats nên `region = 'VN'` lấy selectivity mặc định 0.5%. Ước lượng là 947k × 0.005 ≈ **4.7k dòng**, nhỏ, nên planner chọn `hashed SubPlan`: chạy subquery **1 lần** rồi tra bảng băm. Ước lượng sai 85 lần nhưng lại "may mắn" ra plan tốt.
3. **PG17** (commit `f7816aec2`): planner đi xuyên vào CTE, dùng stats của `stores.region` (VN = 40%). Ước lượng là **380k dòng**, gần đúng hơn (thực tế 400k).
4. 380k × (32 + 24 byte) ≈ 21 MB, lớn hơn `work_mem × hash_mem_multiplier` = 4 MB × 2 = 8 MB, nên planner **không dám dùng bảng băm**. Nó chuyển sang `SubPlan` thường: với **mỗi** sản phẩm PENDING (5k dòng), quét lại danh sách kết quả CTE cho tới khi gặp giá trị khớp. Dòng không khớp phải quét **hết**. Tổng cộng khoảng 5k × 200k = 1 tỉ phép so sánh.
5. Nghịch lý: **PG17 ước lượng đúng hơn nhưng chạy chậm hơn 365 lần.** PG13 nhanh vì đoán sai theo hướng có lợi.

**Kiểm chứng nguyên nhân (EXPLAIN đo trên cả hai bản):**

| Biến thể | PG13 | PG17 |
|---|---|---|
| Nguyên bản (`current_brand_code AS MATERIALIZED`, `work_mem=4MB`) | `hashed SubPlan` ✅ | `SubPlan` ❌ (288.9 s) |
| `current_brand_code AS NOT MATERIALIZED` (inline; cả hai bản đều thấy stats thật của `stores`) | **`SubPlan` ❌** | `SubPlan` ❌ |
| `SET work_mem = '16MB'` | `hashed SubPlan` ✅ | `hashed SubPlan` ✅ |
| Viết lại thành `NOT EXISTS (SELECT 1 … WHERE cbc.brand_code = p.brand_code)` | `Merge Anti Join` ✅ | `Hash Anti Join` ✅ |

Dòng thứ 2 cho thấy: khi PG13 cũng được biết stats thật (CTE inline), **PG13 chọn đúng plan xấu như PG17**. Vậy nguyên nhân là **khác biệt về thông tin stats mà planner nhìn thấy**, không phải PG17 thực thi chậm hơn.

**Sửa bền vững:** viết lại `NOT IN (subquery)` thành `NOT EXISTS` (anti-join, không phụ thuộc ngưỡng bộ nhớ; chú ý ngữ nghĩa NULL: `NOT IN` trả rỗng nếu subquery có NULL, `NOT EXISTS` thì không). Tạm thời: tăng `work_mem` cho session chạy batch.

### 7.2 CASE 3 – CTE dùng 2 lần + điều kiện lọc trên cột CTE  — `sql/case3_setup.sql`, `sql/case3_update.sql`
```sql
WITH open_orders AS MATERIALIZED (SELECT id, customer_id, cust_code, amount, priority, created_at
                                  FROM orders2 WHERE status = 'OPEN')
UPDATE customer_stats2 cs SET pair_cnt = s.n, open_amount = s.total, updated_at = now()
FROM (SELECT a.customer_id, count(*) n, sum(a.amount) total
      FROM open_orders a
      JOIN open_orders b ON b.customer_id = a.customer_id AND b.id <> a.id
      JOIN customers2 cu ON cu.name = a.cust_code
      WHERE a.priority >= 8 AND a.created_at >= '2026-10-01' AND a.amount >= 900
        AND b.priority >= 8 AND b.created_at >= '2026-10-01' AND b.amount >= 900
      GROUP BY a.customer_id) s
WHERE cs.customer_id = s.customer_id;
```
Dữ liệu: 2M đơn, 0.5% `OPEN` (10k). Mọi đơn OPEN đều mới, ưu tiên cao và giá trị lớn, trong khi trên toàn bảng các đặc điểm này là thiểu số (dữ liệu **tương quan**).

| | Ước lượng CTE `a` sau lọc | Thực tế | Plan join a–b | Thời gian |
|---|---|---|---|---|
| PG13 | 373 (mặc định 0.333³) | 10 000 | Merge Join | **0.23 s** |
| PG17 | **1** (stats toàn bảng: mỗi điều kiện ~1%) | 10 000 | **Nested Loop**, `CTE Scan b … loops=10000`, `Rows Removed by Join Filter: 99 810 000` | **16.7 s** |

Thực nghiệm sửa trên PG17: `open_orders AS NOT MATERIALIZED` cho 0.37 s (planner dùng index `orders2(customer_id)`), `SET enable_nestloop=off` (chỉ để chẩn đoán) cho 0.28 s.
Hệ số tăng theo **bình phương** số dòng thực của CTE: 10k dòng ra 72×, CTE trăm nghìn dòng có thể thành hàng giờ.

### 7.3 CASE 2 – ngay sau `pg_upgrade`, chưa ANALYZE  — `sql/case2_update_nostats.sql` (DB `shop`)
Tái hiện đúng trạng thái Cloud SQL **ngay sau** in-place upgrade (đã kiểm tra: `pg_class.reltuples = -1`, `pg_stats` rỗng; `pg_upgrade` in ra *"Optimizer statistics are not transferred by pg_upgrade"*).
- PG13: Merge Join + Hash Join, **4.7 s** (thực tế 32k × 499k → 32M cặp).
- PG17 chưa ANALYZE: CTE `open_orders` và `hot_complaints` mỗi cái ước lượng **1 dòng** (thực tế 32k / 499k). Planner dựng `Nested Loop (outer = hot_complaints, inner = Hash Join(Seq Scan customers 500k, open_orders))`, nghĩa là nhánh inner chạy lại cho **mỗi** dòng của `hot_complaints`. Kết quả: **> 900 s**, bị `statement_timeout = 15min` cắt ngang (chưa xong).
- Sau `VACUUM; REINDEX DATABASE; ANALYZE`: **6.0 s** (Hash Join như PG13; ước lượng join 2.9M so với thực tế 32M nhưng plan vẫn đúng).
- Kết luận: case này giải thích được các query chậm **ngay sau upgrade**. Dự án đã ANALYZE mà vẫn chậm, nên nguyên nhân của dự án nhiều khả năng thuộc nhóm CASE 3/4.

### 7.4 CASE 1 – đối chứng ước lượng (không phải regression)  — `sql/case1_update.sql`
CTE `OPEN` (40k dòng, chỉ thuộc 500 khách) join `complaints` (1M dòng, 500 khách):

| | Ước lượng join | Ước lượng nhóm | Thực tế | Thời gian |
|---|---|---|---|---|
| PG13 | 77M | 200 | 80M dòng, 500 nhóm | 24.5 s |
| PG17 | 5.9M (thấp 13×) | 6 897 (cao 14×) | 80M dòng, 500 nhóm | 15.0 s |

Plan giống nhau (Hash Join), nhưng cho thấy công thức thu nhỏ `n_distinct` (2.6.2) đánh giá sai tập con **tập trung**. Ở query lớn hơn, sai lệch này có thể làm đổi thứ tự/phương thức join.

### 7.5 Những gì đã thử mà KHÔNG ra regression (để tránh kết luận vội)
- CTE ước lượng 1 dòng join với bảng lớn qua khóa có thể hash: cả hai bản đều chọn **Hash Join** (hash phía nhỏ), không bị chậm. Nested Loop chỉ thành "thảm họa" khi **cả hai phía** đều bị ước lượng rất nhỏ (CASE 2, 3) hoặc khi không có lựa chọn hash (`NOT IN` không hash được – CASE 4).
- Tăng quy mô CASE 3 (7M đơn, 70k OPEN): ước lượng PG17 lên 10 dòng, planner chuyển về Merge Join. Regression **phụ thuộc mạnh vào dữ liệu cụ thể**, nên phải kiểm chứng bằng plan thật.

### 7.6 Áp dụng cho câu query thật của dự án
1. Chạy `EXPLAIN (SETTINGS)` trên PG17 (không cần ANALYZE): nếu thấy `NOT (ANY (... = (SubPlan N).col1))` **không có `hashed`** trong khi PG13 có `hashed SubPlan`, thì đó là CASE 4. Thử `SET work_mem='64MB'` trong session, hoặc viết lại thành `NOT EXISTS`.
2. Nếu thấy `Nested Loop` với `CTE Scan … rows=1` và nhánh inner là `CTE Scan`/`Hash Join`/`Seq Scan`, thì đó là CASE 3. Thử `NOT MATERIALIZED`, hoặc dùng bảng tạm + index + `ANALYZE`.
3. So sánh ước lượng `rows=` của các `CTE Scan … Filter:` giữa PG13 và PG17: chênh lệch lớn chính là dấu vết của commit `f7816aec2`.

---

## 8. Nguồn tham khảo
- [PostgreSQL 17 release notes](https://postgresql.org/docs/current/release-17.html) (CTE stats/sort order, GROUP BY reordering…)
- [pgsql-hackers: Wrong rows estimations with joins of CTEs (dẫn tới commit f7816aec2)](https://www.postgresql.org/message-id/flat/4504e67078d648cdac3651b2960da6e7%40nidsa.net)
- [BUG #18466: Wrong row estimate for nested loop (PG16 vs PG17)](https://www.postgresql.org/message-id/flat/18466-1d296028273322e2%40postgresql.org)
- [pgsql-hackers: Propagate pathkeys from CTEs up to the outer query](https://www.postgresql.org/message-id/flat/CAMbWs49xYd3f8CrE8-WW3--dV1zH_sDSDn-vs2DzHj81Wcnsew%40mail.gmail.com)
- [pganalyze: Better Query Plans for Materialized CTE Scans (PG17)](https://pganalyze.com/blog/5mins-postgres-17-materialized-cte-scans)
- [boringSQL: Good CTE, bad CTE](https://boringsql.com/posts/good-cte-bad-cte/)
- Microsoft Tech Community: "When PostgreSQL v17 Chooses the Wrong Plan: A Deep Dive into CTEs with Data Skew" (chưa đọc được nội dung, chưa kiểm chứng)
- [`enable_group_by_reordering` (PG17)](https://postgresqlco.nf/doc/en/param/enable_group_by_reordering/)
- [pgsql-bugs: Queries are too slow after upgrading to PG17 from PG15 (1)](https://www.postgresql.org/message-id/2CB274CF-6282-48F9-988F-59F16E1D0EEC@blackduck.com), [(2)](https://www.postgresql.org/message-id/4A6CAA9C-E2A2-46EB-BBF6-E33256DDEAF3%40blackduck.com)
- [pgsql-general: Performance degradation after upgrading 9.5→14 (Memoize)](https://www.postgresql.org/message-id/CAApHDvqJK%2BARTYLYeS-C1i_seVbcp0nfCVbxu960nGpssFJA%2Bw%40mail.gmail.com)
- [GitLab: Upgrading external PostgreSQL databases (ANALYZE bắt buộc sau major upgrade)](https://docs.gitlab.com/administration/postgresql/external_upgrade)
- [CloudNativePG: PostgreSQL upgrades](https://cloudnative-pg.io/documentation/current/postgres_upgrades)
- Release notes PG [14](https://www.postgresql.org/docs/14/release-14.html), [15](https://www.postgresql.org/docs/15/release-15.html), [16](https://www.postgresql.org/docs/16/release-16.html) — đối chiếu các mục ở Mục 2.
- Tài liệu Google Cloud SQL "Upgrade the database major version in-place" — nên đọc để xác nhận Cloud SQL có tự chạy `ANALYZE` hay không (tôi chưa xác nhận được từ nguồn Google trong lần tra cứu này).
