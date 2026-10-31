# pgdoctor – phát hiện regression khi nâng cấp PostgreSQL 13 → 17 (offline)

`pgdoctor` đọc các **file** thu thập từ hai server (bản cũ / bản mới) và chỉ ra *vì sao* một query chậm đi sau khi upgrade, kèm bằng chứng, giải thích và cách xử lý. Tool **không kết nối database**, chỉ dùng thư viện chuẩn Python (≥ 3.8), không cần `pip install`.

```
python -m pgdoctor analyze --old-plan q.pg13.json --new-plan q.pg17.json -o report.html
```

Kết quả là một file `report.html` tự chứa (mở offline được, có chế độ sáng/tối), kèm tóm tắt in ra console. Có thể xuất thêm `--json findings.json` hoặc dùng `--fail-on high` để làm gate trong CI.

## 1. Tool phát hiện được gì

| Nhóm | Rule | Phát hiện |
|---|---|---|
| **So sánh plan** | `CMP-REGRESSION` | Thời gian thực thi tăng (có hệ số) |
| | `CMP-SUBPLAN-HASH-LOST` ★ | Bản cũ có `hashed SubPlan` (NOT IN/IN), bản mới thì không. Danh sách bị duyệt lại cho từng dòng, O(N×M) |
| | `CMP-NOSTATS` ★ | Bản mới có dấu hiệu thiếu stats (pg_upgrade chưa ANALYZE): ước lượng scan toàn bảng lệch, ước lượng sụp còn 1 dòng, số nhóm = 200 |
| | `CMP-CTE-CORRELATION` ★ | PG17 gắn stats bảng gốc cho cột CTE; lọc nhiều cột tương quan → ước lượng sụp còn ~1 dòng. **Không** phải do thiếu ANALYZE, phân biệt với `CMP-NOSTATS` |
| | `CMP-JOIN-METHOD` / `CMP-SCAN-METHOD` | Đổi phương thức join/scan cho cùng tập bảng (Hash → Nested Loop…) |
| | `CMP-EST-SHIFT` | Cùng node nhưng ước lượng khác ≥10× giữa hai bản; cho biết bản nào gần actual hơn; gợi ý cơ chế PG17 lấy stats xuyên CTE (f7816aec2) |
| | `CMP-DIVERGE` | Điểm phân kỳ đầu tiên của hai plan, thứ tự join khác nhau |
| | `CMP-CTE-PATHKEYS` | PG17 dùng sort order của CTE (Merge Join trên CTE Scan, a65724dfa) |
| | `CMP-NEW-NODE`, `CMP-SPILL`, `CMP-JIT`, `CMP-SETTINGS`, `CMP-GROUPKEY-ORDER` | Node chỉ có ở bản mới (Memoize…), tràn đĩa mới, JIT, EXPLAIN SETTINGS khác, GROUP BY reorder |
| **Từng plan** | `PLAN-NL-RESCAN` ★ | Nested Loop chạy lại inner side không có index (CTE Scan/Seq Scan/Materialize) hàng nghìn lần |
| | `PLAN-SUBPLAN-NOHASH` ★ | SubPlan NOT IN không được hash. Chạy được cả khi chỉ có `EXPLAIN` (ước lượng số phép so sánh) |
| | `PLAN-TEMP-IO` | Đọc file tạm hàng GB; báo khi lượng đọc gấp nhiều lần lượng ghi (kết quả trung gian bị quét lại) |
| | `PLAN-MISEST` | Node **khởi phát** sai lệch ước lượng (bỏ qua các trường hợp hợp lệ: Merge Join inner, Limit, semi-join, ModifyTable) |
| | `PLAN-NL-BET`, `PLAN-COLLAPSE`, `PLAN-DEFAULT-NDISTINCT` | Planner đặt cược vào ước lượng ≈1 dòng; nhiều điều kiện → 1 dòng; ước lượng mặc định 200 nhóm |
| | `PLAN-MEMOIZE`, `PLAN-SPILL`, `PLAN-JIT`, `PLAN-PARALLEL`, `PLAN-HEAPFETCH`, `PLAN-TRIGGER`, `PLAN-PLANNING`, `PLAN-CTE-RESCAN`, `PLAN-IO-COLD`, `PLAN-SAOP`… | Memoize miss, tràn đĩa, JIT, worker thiếu, visibility map, trigger/FK, planning time, CTE quét lại, cache lạnh, IN-list (PG17) |
| **Thống kê & catalog** | `HEALTH-SEARCHPATH-FUNC` ★ | Hàm trong expression index/matview không cố định `search_path`. Từ PG17, ANALYZE/VACUUM/REINDEX trên bảng đó **bị lỗi**. Tool đối chiếu với bảng đang thiếu stats để báo "nhiều khả năng ANALYZE đã lỗi". Chạy trên PG13 thì đây là cảnh báo **trước khi upgrade** |
| | `HEALTH-NEVER-ANALYZED`, `HEALTH-PARTITION-PARENT`, `HEALTH-EXTSTATS-NODATA`, `HEALTH-STATS-MISSING-COLS` | Bảng/cột/extended stats/bảng partitioned cha chưa có thống kê |
| | `HEALTH-NDISTINCT-SHIFT`, `HEALTH-STATS-TARGET`, `HEALTH-STALE`, `HEALTH-DEAD` | n_distinct lệch giữa hai bản, statistics target khác, stats cũ, dead tuple |
| | `HEALTH-EXT-OUTDATED`, `HEALTH-INVALID-INDEX`, `HEALTH-COLLATION` | Extension chưa `ALTER EXTENSION … UPDATE`, index invalid, collation version lệch |
| **Cấu hình** | `SET-DIFF`, `SET-ENABLE-OFF`, `SET-REMOVED`, `SET-NEW-PLANNER-GUC` | Tham số planner khác nhau (vd. `hash_mem_multiplier` mặc định 1 → 2), enable_* bị tắt, flag bị bỏ ở PG14–17, công tắc planner mới để chẩn đoán |
| **pg_stat_statements** | `PGSS-REGRESSION`, `PGSS-TEMP`, `PGSS-IO` | Query nào chậm đi nhiều nhất (ghép theo text đã chuẩn hoá vì `queryid` có thể đổi giữa các major version) |

★ là các pattern đã được **tái hiện và đo thật** trong lab docker của dự án. Xem `python -m pgdoctor kb` hoặc mục *Knowledge base* trong report để đọc giải thích và nguồn của từng rule.

## 2. Thu thập dữ liệu (chạy trên cả hai server)

### 2.1 Plan của query nghi ngờ
Chạy giống nhau trên PG13 (bản gốc hoặc clone trước upgrade) và PG17:

```sql
BEGIN;
EXPLAIN (ANALYZE, BUFFERS, SETTINGS, FORMAT JSON) <câu UPDATE/SELECT>;
ROLLBACK;            -- với UPDATE/DELETE: không ghi dữ liệu
```

```bash
psql "host=... dbname=... user=..." -X -q -At -f explain_q1.sql > plans/q1.pg13.json
```

- **Query chạy 2 giờ trên PG17?** Chạy `EXPLAIN (SETTINGS, FORMAT JSON)` **không có ANALYZE** (ra kết quả ngay). Tool vẫn phát hiện được các rủi ro chỉ cần ước lượng: `CMP-SUBPLAN-HASH-LOST`, `PLAN-SUBPLAN-NOHASH`, `PLAN-NL-BET`, `CMP-JOIN-METHOD`, `CMP-EST-SHIFT`, `CMP-NOSTATS`…
- Nhận cả **text format** (copy từ psql, kể cả khung `QUERY PLAN` và dấu `+`) lẫn log `auto_explain`.
- Đặt tên file theo dạng `<tên>.<nhãn>.json|txt`, với nhãn là `pg13`/`pg17`, `old`/`new` hoặc `before`/`after`. Khi đó dùng `--plans-dir plans/` để phân tích nhiều query cùng lúc.

### 2.2 Snapshot cấu hình, thống kê, pg_stat_statements
```bash
mkdir -p out/pg13 && cd out/pg13
psql "host=<pg13> dbname=<db> user=<user>" -X -v ON_ERROR_STOP=1 -f ../../collect/pgdoctor_collect.sql
# lặp lại cho PG17 vào out/pg17
```
Collector ghi ra `settings.json`, `health.json`, và `statements.json` (nếu đã cài `pg_stat_statements`). Collector:
- chỉ đọc catalog/view thống kê (`pg_settings`, `pg_class`, `pg_stats`, `pg_stats_ext`, `pg_index`, `pg_depend`…);
- **không** đọc dữ liệu bảng và **không** xuất giá trị MCV/histogram (chỉ xuất số đếm);
- được thiết kế để không cần superuser (dùng `pg_stats`/`pg_stats_ext` thay cho `pg_statistic`). Đã chạy thử trên PG13/PG17 của lab, **chưa** chạy thử trên Cloud SQL thật. Lưu ý `pg_stats` chỉ trả về bảng mà user có quyền SELECT.

Lưu ý: `statements.json` có chứa text query (đã normalise thành `$1`…). Hãy kiểm tra trước khi chia sẻ file ra ngoài.

## 3. Chạy phân tích

```bash
cd tools/pgdoctor
python -m pgdoctor analyze \
  --plans-dir plans/ \
  --old-dir out/pg13 --new-dir out/pg17 \
  -o report.html --json findings.json
```

Tuỳ chọn khác: `--old-settings/--new-settings`, `--old-health/--new-health`, `--old-statements/--new-statements` (JSON từ collector hoặc CSV export của `pg_stat_statements`), `--old-label/--new-label`, `--min-calls`, `--fail-on {critical,high,medium,low,info}`.

Cách đọc report:
1. Đọc **Nghi phạm hàng đầu** trước.
2. Phần **So sánh plan** hiển thị hai cây plan cạnh nhau:
   - Hàng vàng là node có finding, hàng đỏ là điểm phân kỳ đầu tiên.
   - Rê chuột lên một node để thấy node tương ứng ở bản kia.
   - Chip ↑/↓ là sai lệch ước lượng: thiếu hoặc thừa.
3. Mỗi finding gồm: bằng chứng (số liệu), *Vì sao*, *Nên làm*, câu lệnh chẩn đoán (`SET LOCAL … ; EXPLAIN …` trong transaction) và nguồn.

Khi bản cũ và bản mới cùng có vấn đề, finding thuộc **bản cũ** chỉ là bối cảnh nên bị hạ xuống mức `low`. Report tập trung vào những gì *thay đổi* sau upgrade.

## 4. Thử ngay với dữ liệu mẫu

`samples/` chứa plan **thật** chụp từ lab docker (PG 13.23 và PG 17.11, PG17 được tạo bằng `pg_upgrade` thật):

| Mẫu | Nội dung | Kết quả đo |
|---|---|---|
| `qA_cte_join_update` | 2 CTE MATERIALIZED join nhau + UPDATE; PG17 vừa upgrade, chưa ANALYZE | 188 ms → 16.4 s (≈87×), Nested Loop quét CTE 3927 lần |
| `qA_after_analyze` | Cùng query sau khi ANALYZE trên PG17 | 188 ms → 166 ms (hết regression) |
| `qB_bug18466`, `qC_cte_pathkeys` | Thay đổi PG17 về stats/pathkeys của CTE (BUG #18466, a65724dfa) | Không chậm hơn, nhưng plan đổi |
| `qD_cte_skew` | Tập con tập trung: PG17 dùng stats toàn bảng cho cột CTE | Ước lượng PG17 sai 44× (PG13 4×) |
| `qE_notin_subplan` | `UPDATE … NOT IN (SELECT … FROM cte)`: PG13 hashed SubPlan, PG17 thì không (plan PG17 **chỉ có EXPLAIN**) | Lab: 0.79 s → 289 s |
| `collected/pg13`, `collected/pg17` | Snapshot thật từ collector. Trên PG17, `ANALYZE ord` **lỗi** vì hàm `status_rank()` trong expression index | `HEALTH-SEARCHPATH-FUNC` = critical |
| `synthetic/` | pg_stat_statements **giả lập** (chỉ để kiểm thử module, không phải số đo) | |

```bash
python -m pgdoctor analyze --plans-dir samples/plans --old-dir samples/collected/pg13 --new-dir samples/collected/pg17 \
  --old-statements samples/synthetic/statements.pg13.json --new-statements samples/synthetic/statements.pg17.csv \
  -o samples/report_full.html
python -m unittest discover -s tests -v
```

Muốn tạo lại các mẫu: dựng lab ở thư mục gốc dự án, rồi chạy `bash capture.sh` trong `samples/lab`. Script tạo database riêng `pgdoctor_fx` trên cả hai container, chụp plan qA–qD, áp dụng `fx_health_extra.sql` và chạy collector. Mẫu `qE` lấy từ database `lab4` (`sql/case4_setup.sql` của lab) và chỉ được chụp nếu database đó đã tồn tại.

## 5. Giới hạn (đọc trước khi kết luận)
- Mọi rule là **heuristic**. Hãy xác nhận bằng `EXPLAIN (ANALYZE, BUFFERS)` và thử nghiệm `SET LOCAL …` trong transaction rồi `ROLLBACK`.
- Thời gian riêng (exclusive time) của từng node chỉ là xấp xỉ, nhất là với CTE (InitPlan) và node song song.
- Các node được ghép giữa hai plan dựa trên tập bảng/alias. Nếu query bị viết lại khác nhau giữa hai bản, việc ghép có thể sai.
- Ngưỡng hash của SubPlan chỉ là ước tính, gần với công thức của planner, nên chỉ dùng để định hướng.
- Tool không thấy được nguyên nhân nằm ngoài plan: chờ lock, IO throttling của Cloud SQL, replica/HA. Với các trường hợp này, xem `pg_stat_activity.wait_event` trong lúc query chạy.
