"""Knowledge base: every rule the tool can raise, with explanation, actions and sources.

The text is Vietnamese (target audience), identifiers/SQL stay in English.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

REF = {
    "pg17_rel": ("PostgreSQL 17 release notes", "https://www.postgresql.org/docs/17/release-17.html"),
    "pg16_rel": ("PostgreSQL 16 release notes", "https://www.postgresql.org/docs/16/release-16.html"),
    "pg15_rel": ("PostgreSQL 15 release notes", "https://www.postgresql.org/docs/15/release-15.html"),
    "pg14_rel": ("PostgreSQL 14 release notes", "https://www.postgresql.org/docs/14/release-14.html"),
    "pgupgrade": ("pg_upgrade docs (stats không được chuyển)", "https://www.postgresql.org/docs/17/pgupgrade.html"),
    "cloudsql_upgrade": ("Cloud SQL: in-place major version upgrade", "https://cloud.google.com/sql/docs/postgres/upgrade-major-db-version-inplace"),
    "row_est": ("Row estimation examples", "https://www.postgresql.org/docs/17/row-estimation-examples.html"),
    "ext_stats": ("Extended statistics", "https://www.postgresql.org/docs/17/planner-stats.html#PLANNER-STATS-EXTENDED"),
    "cte_stats_pganalyze": ("pganalyze: PG17 better plans for materialized CTE scans (f7816aec2, a65724dfa)",
                            "https://pganalyze.com/blog/5mins-postgres-17-materialized-cte-scans"),
    "bug18466": ("BUG #18466 – ước lượng CTE (PG16 vs PG17)", "https://www.postgresql.org/message-id/flat/18466-1d296028273322e2%40postgresql.org"),
    "cte_thread": ("pgsql-hackers: join của CTE ước lượng sai → chậm ~60x",
                   "https://www.postgresql.org/message-id/flat/4504e67078d648cdac3651b2960da6e7%40nidsa.net"),
    "boringsql_cte": ("boringSQL: Good CTE, bad CTE", "https://boringsql.com/posts/good-cte-bad-cte/"),
    "memoize": ("enable_memoize", "https://www.postgresql.org/docs/17/runtime-config-query.html#GUC-ENABLE-MEMOIZE"),
    "hash_mem": ("hash_mem_multiplier", "https://www.postgresql.org/docs/17/runtime-config-resource.html#GUC-HASH-MEM-MULTIPLIER"),
    "jit": ("JIT: khi nào JIT được dùng", "https://www.postgresql.org/docs/17/jit-decision.html"),
    "groupby": ("enable_group_by_reordering (PG17)", "https://www.postgresql.org/docs/17/runtime-config-query.html"),
    "saop1": ("pgsql: chậm sau khi lên PG17 (SAOP / IN-list)", "https://www.postgresql.org/message-id/2CB274CF-6282-48F9-988F-59F16E1D0EEC@blackduck.com"),
    "saop2": ("pgsql: bisect regression PG17 (IN-list)", "https://www.postgresql.org/message-id/4A6CAA9C-E2A2-46EB-BBF6-E33256DDEAF3%40blackduck.com"),
    "explain": ("Using EXPLAIN", "https://www.postgresql.org/docs/17/using-explain.html"),
    "vacuum_vm": ("Visibility map & index-only scans", "https://www.postgresql.org/docs/17/indexes-index-only-scans.html"),
    "pgss": ("pg_stat_statements", "https://www.postgresql.org/docs/17/pgstatstatements.html"),
    "notin_wiki": ("PostgreSQL wiki – Don't Do This: NOT IN", "https://wiki.postgresql.org/wiki/Don%27t_Do_This#Don.27t_use_NOT_IN"),
    "subquery": ("Subquery expressions (NOT IN và NULL)", "https://www.postgresql.org/docs/17/functions-subquery.html"),
    "collation": ("Collation versions", "https://www.postgresql.org/docs/17/sql-altercollation.html"),
    # --- added in rev 3 (research agents + lab/remedies) ---
    "cloudsql_ext": ("Cloud SQL: supported extensions (pg_hint_plan, auto_explain)", "https://docs.cloud.google.com/sql/docs/postgres/extensions"),
    "cloudsql_flags": ("Cloud SQL: database flags (work_mem 4MB, hash_mem_multiplier 2, pg_hint_plan.*)", "https://docs.cloud.google.com/sql/docs/postgres/flags"),
    "cloudsql_features": ("Cloud SQL: unsupported features (LLVM JIT)", "https://docs.cloud.google.com/sql/docs/postgres/features"),
    "hint_table": ("pg_hint_plan 1.7 (PG17): hint table theo query_id", "https://github.com/ossc-db/pg_hint_plan/blob/PG17/docs/hint_table.md"),
    "alter_role": ("ALTER ROLE … IN DATABASE … SET (thứ tự ưu tiên)", "https://www.postgresql.org/docs/17/sql-alterrole.html"),
    "subselect_src": ("subplan_is_hashable() – subselect.c (REL_17_STABLE)", "https://github.com/postgres/postgres/blob/REL_17_STABLE/src/backend/optimizer/plan/subselect.c"),
    "tom_notin": ("Tom Lane: hashed subplan vs không – O(N²) khi bảng băm ước lượng vượt work_mem", "https://postgresql.org/message-id/6343.1491401090%40sss.pgh.pa.us"),
    "perf_12_17": ("pgsql-performance: 12.15 → 17.2, 250 ms → 377 s, work_mem 256MB sửa được", "https://www.postgresql.org/message-id/43ed5a72fdfdd44fa046d8911c496364a3eb9231.camel@cybertec.at"),
    "q95": ("pgsql-hackers: TPC-DS q95 chậm 2× ở PG17 do f7816aec2 (không sửa)", "https://www.postgresql.org/message-id/4e14cc4f-edaf-0820-2bdf-db8dd0a9efab%40gmail.com"),
    "memoize_reg": ("pgsql-hackers: Memoize regression (costing giả định khoá phân bố đều)", "https://www.postgresql.org/message-id/CAApHDvpBd%2BGb43wmZ94qnKLEEmT0h-S6No4mA2%2BYiqs-%3D0yQHQ%40mail.gmail.com"),
}


@dataclass
class Rule:
    id: str
    title: str
    category: str
    why: str
    action: str
    diag: List[str] = field(default_factory=list)
    refs: List[str] = field(default_factory=list)
    since: str = ""          # version that introduced the behaviour (if version-specific)


_R: List[Rule] = [
    # ------------------------------------------------------------------ plan
    Rule("PLAN-MISEST", "Ước lượng số dòng sai lệch lớn (điểm khởi phát)", "plan",
         "Planner chọn join order/join method dựa trên số dòng ước lượng. Node được đánh dấu là nơi sai lệch "
         "BẮT ĐẦU (các node con vẫn ước lượng đúng), nên đây thường là gốc của plan tệ. Sai lệch ≥100× gần như "
         "chắc chắn làm planner chọn sai (Nested Loop thay vì Hash Join, sai thứ tự join…).",
         "Xác định vì sao ước lượng sai: thiếu stats (chưa ANALYZE sau pg_upgrade), cột tương quan "
         "(cần CREATE STATISTICS), phân bố lệch (tăng STATISTICS target), điều kiện trên biểu thức/CTE "
         "(planner không có stats). Sửa ở đúng node này trước khi tinh chỉnh chỗ khác.",
         ["ANALYZE <table>;",
          "ALTER TABLE <t> ALTER COLUMN <c> SET STATISTICS 1000; ANALYZE <t>;",
          "CREATE STATISTICS <s> (dependencies, ndistinct, mcv) ON <c1>, <c2> FROM <t>; ANALYZE <t>;"],
         ["row_est", "ext_stats"]),
    Rule("PLAN-NL-RESCAN", "Nested Loop quét lặp lại inner side không có index", "plan",
         "Inner side của Nested Loop là Seq Scan / CTE Scan / Materialize / Subquery… (không phải index lookup) "
         "và bị chạy lại hàng nghìn lần. Chi phí thực tế là O(outer × inner) – đây là chữ ký kinh điển của "
         "'2 phút thành 2 giờ'. Thường do outer side bị ước lượng ≈1 dòng.",
         "Sửa ước lượng của outer side (xem PLAN-MISEST). Tạm thời có thể kiểm chứng bằng SET LOCAL enable_nestloop=off "
         "trong transaction thử nghiệm. Với CTE MATERIALIZED trả nhiều dòng: chuyển thành temp table + index + ANALYZE.",
         ["BEGIN; SET LOCAL enable_nestloop = off; EXPLAIN (ANALYZE, BUFFERS) ...; ROLLBACK;",
          "CREATE TEMP TABLE tmp AS <cte query>; CREATE INDEX ON tmp(<join key>); ANALYZE tmp;"],
         ["cte_thread", "explain"]),
    Rule("PLAN-CTE-COST", "Thời gian của CTE: thân CTE hay quét lại kết quả CTE", "plan",
         "Khi thấy 'CTE Scan on current_brand_code' rất lâu, có hai khả năng khác hẳn nhau: (a) THÂN CTE (join nhiều bảng) "
         "chạy chậm – chỉ chạy 1 lần; (b) kết quả CTE đã lưu bị QUÉT LẠI hàng nghìn lần (SubPlan NOT IN không hash, Nested Loop "
         "có CTE Scan ở inner). Thời gian gộp của CTE Scan bao gồm cả lần chạy thân CTE, nên rule tách: đọc lại ≈ tổng thời "
         "gian CTE Scan − thời gian thân CTE. Trong lab CASE 4, thân CTE ~1–2 s nhưng phần đọc lại chiếm gần hết thời gian.",
         "(b) → sửa nguyên nhân quét lại (xem PLAN-SUBPLAN-NOHASH / PLAN-NL-RESCAN và phần Khắc phục); thêm index cho bảng "
         "trong thân CTE KHÔNG giúp. (a) → so plan của riêng SELECT trong CTE giữa PG13/PG17 (join order, Memoize, index).",
         ["EXPLAIN (ANALYZE, BUFFERS) <SELECT trong thân CTE>;  -- chạy riêng thân CTE trên cả hai bản"],
         ["explain", "cte_stats_pganalyze"]),
    Rule("PLAN-NL-BET", "Nested Loop dựa trên ước lượng outer ≈ 1 dòng (rủi ro)", "plan",
         "Planner chỉ chọn Nested Loop với inner không có index khi tin rằng outer side rất nhỏ (thường rows=1). "
         "Nếu ước lượng này sai, thời gian tăng theo cấp số nhân. Rule này hoạt động cả khi chỉ có EXPLAIN (không ANALYZE).",
         "Kiểm tra số dòng thật của outer side (SELECT count(*) của phần đó). Nếu lớn: cung cấp stats/extended stats, "
         "viết lại CTE thành temp table có ANALYZE, hoặc thêm index cho khoá join của inner.",
         ["SELECT count(*) FROM (<outer subquery>) s;"], ["row_est"]),
    Rule("PLAN-COLLAPSE", "Ước lượng sụp về 1 dòng do nhiều điều kiện lọc", "plan",
         "Planner giả định các điều kiện độc lập và nhân selectivity với nhau; khi không có stats mỗi điều kiện dùng "
         "hằng mặc định (= 0.005, < > 0.333, LIKE 0.005…). 4–6 điều kiện → ước lượng 1 dòng dù thực tế hàng chục nghìn.",
         "ANALYZE bảng; với các cột tương quan dùng CREATE STATISTICS (dependencies, mcv).",
         ["SELECT attname, n_distinct, most_common_vals FROM pg_stats WHERE tablename = '<t>';"],
         ["row_est", "ext_stats"]),
    Rule("PLAN-DEFAULT-NDISTINCT", "Ước lượng nhóm/join = 200 → planner không có stats cho cột (DEFAULT_NUM_DISTINCT)", "plan",
         "Khi không có pg_statistic cho một cột (cột của CTE ở PG ≤16, cột biểu thức, bảng chưa ANALYZE), planner dùng "
         "n_distinct mặc định = 200. Ở PG17, cột CTE là Var thuần sẽ được lấy stats từ bảng gốc (commit f7816aec2), "
         "nên cùng câu query PG13 và PG17 có thể ước lượng KHÁC NHAU tại đây.",
         "Nếu ở bản mới vẫn là 200: bảng gốc chưa có stats hoặc cột là biểu thức/aggregate. Nếu bản cũ 200 và bản mới khác: "
         "xem CMP-EST-SHIFT.",
         [], ["bug18466", "cte_stats_pganalyze"]),
    Rule("PLAN-MEMOIZE", "Memoize kém hiệu quả (cache miss / eviction)", "plan",
         "Memoize (PG14+) cache kết quả inner side của Nested Loop theo tham số. Planner chọn nó dựa trên n_distinct "
         "ước lượng của khoá; nếu sai, cache miss cao hoặc bị evict → tốn thêm CPU/RAM và có thể khiến planner chọn "
         "Nested Loop thay vì Hash Join.",
         "So sánh với SET enable_memoize = off. Nếu tốt hơn rõ rệt: sửa ước lượng n_distinct của khoá (ALTER TABLE ... "
         "ALTER COLUMN ... SET (n_distinct = ...)) hoặc tắt memoize cho session/role tạm thời.",
         ["BEGIN; SET LOCAL enable_memoize = off; EXPLAIN (ANALYZE, BUFFERS) ...; ROLLBACK;"],
         ["memoize", "memoize_reg", "pg14_rel"], since="14"),
    Rule("PLAN-SPILL", "Tràn bộ nhớ ra đĩa (Hash batches / Sort / HashAgg)", "plan",
         "Hash join chia nhiều batch, sort external merge, HashAggregate ghi đĩa: thời gian tăng mạnh. Batches tăng so "
         "với 'originally' cho thấy planner ước lượng thiếu số dòng. Mặc định hash_mem_multiplier đổi 1.0 → 2.0 ở PG15.",
         "Tăng work_mem cho session chạy batch, kiểm tra hash_mem_multiplier giữa hai bản, sửa ước lượng.",
         ["BEGIN; SET LOCAL work_mem = '256MB'; EXPLAIN (ANALYZE, BUFFERS) ...; ROLLBACK;",
          "SHOW hash_mem_multiplier;"], ["hash_mem", "pg15_rel"]),
    Rule("PLAN-JIT", "JIT chiếm thời gian đáng kể", "plan",
         "JIT kích hoạt khi tổng cost vượt jit_above_cost (mặc định 100000). Ước lượng sai làm cost phồng → JIT compile "
         "(inlining/optimization) tốn hàng giây đến hàng chục giây mà không đem lại lợi ích.",
         "Đo lại với SET jit = off. Nếu tốt hơn: tắt jit theo role/database hoặc tăng jit_above_cost/jit_optimize_above_cost. "
         "Lưu ý: Cloud SQL KHÔNG hỗ trợ LLVM JIT – nếu plan lấy từ lab/self-hosted mà thấy JIT, trên Cloud SQL phần này không tồn tại.",
         ["BEGIN; SET LOCAL jit = off; EXPLAIN (ANALYZE) ...; ROLLBACK;", "SELECT pg_jit_available();"],
         ["jit", "cloudsql_features"]),
    Rule("PLAN-PARALLEL", "Worker song song không được cấp đủ", "plan",
         "Workers Launched < Workers Planned: hết max_parallel_workers / max_worker_processes. Plan được chọn với giả định "
         "song song nhưng chạy tuần tự → chậm hơn dự kiến.",
         "So sánh max_worker_processes, max_parallel_workers, max_parallel_workers_per_gather giữa hai instance; kiểm tra tải đồng thời.",
         ["SHOW max_parallel_workers; SHOW max_worker_processes;"], []),
    Rule("PLAN-HEAPFETCH", "Index Only Scan phải đọc heap nhiều (visibility map chưa set)", "plan",
         "Index Only Scan chỉ nhanh khi trang đã 'all-visible'. Sau pg_upgrade/ghi nhiều, nếu VACUUM chưa chạy thì Heap "
         "Fetches cao. PG17 cũng có báo cáo regression liên quan IN-list/SAOP + index only scan.",
         "VACUUM (ANALYZE) bảng; so sánh Heap Fetches giữa hai bản.", ["VACUUM (VERBOSE, ANALYZE) <table>;"],
         ["vacuum_vm", "saop1"]),
    Rule("PLAN-TRIGGER", "Trigger / ràng buộc FK chiếm nhiều thời gian", "plan",
         "Với UPDATE/DELETE, mỗi dòng có thể kích hoạt trigger hoặc kiểm tra FK. Thời gian này nằm ngoài cây plan nên dễ bị bỏ qua.",
         "Kiểm tra index trên cột FK phía bảng con, logic trigger, và so sánh số dòng bị cập nhật giữa hai bản.",
         [], ["explain"]),
    Rule("PLAN-PLANNING", "Planning time lớn", "plan",
         "Nhiều bảng join (vượt join_collapse_limit/geqo_threshold), nhiều partition, hoặc stats target rất lớn làm "
         "planning chậm; GEQO cho plan không ổn định giữa các lần chạy.",
         "So sánh join_collapse_limit, from_collapse_limit, geqo_threshold; cân nhắc viết lại thứ tự join rõ ràng.",
         ["SHOW join_collapse_limit; SHOW geqo_threshold;"], []),
    Rule("PLAN-FILTER-WASTE", "Quét tuần tự rồi loại bỏ phần lớn dòng", "plan",
         "Seq Scan đọc rất nhiều dòng nhưng Filter loại bỏ gần hết: thiếu index hoặc index không dùng được (kiểu dữ liệu, biểu thức, collation).",
         "Xem có index phù hợp ở bản cũ không (REINDEX/invalid index sau upgrade?).", [], []),
    Rule("PLAN-CTE-RESCAN", "CTE materialized bị quét lại nhiều lần", "plan",
         "CTE Scan với loops lớn: kết quả CTE (không có index) bị duyệt lại cho mỗi dòng outer.",
         "Chuyển CTE sang temp table có index + ANALYZE, hoặc NOT MATERIALIZED nếu CTE chỉ dùng 1 lần.", [],
         ["boringsql_cte", "cte_thread"]),
    Rule("PLAN-NOSTATS-SIGNATURE", "Dấu hiệu bảng chưa có thống kê (reltuples chưa biết)", "plan",
         "Seq Scan không có điều kiện lọc nhưng ước lượng số dòng lệch rõ so với thực tế → planner đang suy ra số dòng từ "
         "kích thước file (reltuples = -1). Điển hình ngay sau pg_upgrade/Cloud SQL in-place upgrade, trước khi ANALYZE.",
         "Chạy ANALYZE (vacuumdb --all --analyze-in-stages) và kiểm tra bằng collector health.",
         ["SELECT relname, reltuples FROM pg_class WHERE reltuples < 0 AND relkind IN ('r','p','m');"],
         ["pgupgrade", "cloudsql_upgrade"]),
    Rule("PLAN-TEMP-IO", "Đọc/ghi file tạm (temp) rất lớn", "plan",
         "Kết quả trung gian (CTE materialized, sort, hash) vượt work_mem nên nằm trên đĩa. Nếu nó bị quét lại nhiều lần "
         "(SubPlan không hash, Nested Loop quét lại CTE), lượng temp ĐỌC có thể gấp hàng trăm lần lượng ghi: hàng chục GB I/O.",
         "Xử lý nguyên nhân quét lại (xem PLAN-SUBPLAN-NOHASH / PLAN-NL-RESCAN); tăng work_mem cho session; theo dõi "
         "log_temp_files.", ["SET log_temp_files = 0;   -- ghi log mọi file tạm"], ["hash_mem"]),
    Rule("PLAN-IO-COLD", "Đọc đĩa nhiều (cache lạnh / IO)", "plan",
         "Phần lớn block được đọc từ đĩa thay vì shared buffers. Sau upgrade/restart cache lạnh; hoặc IO của instance mới bị giới hạn.",
         "Chạy lại query lần 2 để so sánh; kiểm tra IOPS/throughput của disk Cloud SQL và track_io_timing.", [], []),
    Rule("PLAN-SAOP", "Index scan với IN-list / = ANY(array) lớn", "plan",
         "PG17 thay đổi cách B-tree xử lý ScalarArrayOp (IN-list): thường nhanh hơn nhưng đã có báo cáo regression "
         "(heap fetch tăng, sort lại mảng nhiều lần).",
         "So sánh Heap Fetches / thời gian node này giữa hai bản; thử viết lại IN-list lớn thành JOIN với VALUES/temp table.",
         [], ["saop1", "saop2"], since="17"),
    Rule("PLAN-SUBPLAN-NOHASH", "SubPlan (NOT IN / IN / = ANY) KHÔNG được hash – duyệt lại danh sách cho từng dòng", "plan",
         "Với `x NOT IN (SELECT ...)`, planner chỉ dùng 'hashed SubPlan' nếu bảng băm ước lượng vừa với work_mem × "
         "hash_mem_multiplier. Nếu ước lượng số dòng của subquery lớn hơn ngưỡng đó, mỗi dòng ngoài sẽ duyệt tuyến tính "
         "danh sách kết quả: O(N×M) – hàng trăm triệu đến hàng tỉ phép so sánh. Ở PG17, việc lấy stats xuyên CTE "
         "(f7816aec2) có thể làm ước lượng của subquery TĂNG (đúng hơn) → vượt ngưỡng → mất hash, dù PG13 vẫn hash nhờ "
         "ước lượng sai 'may mắn'.",
         "KHÔNG sửa code (xem mục Khắc phục / lệnh `remedy`): (1) pg_hint_plan hint table `Set(hash_mem_multiplier N)` chỉ cho "
         "đúng câu này (khoá query_id); (2) ALTER ROLE <role batch> IN DATABASE <db> SET hash_mem_multiplier/work_mem; "
         "(3) flag instance. Ngưỡng chính xác (subplan_is_hashable, giống nhau PG13→18): rows × (MAXALIGN(width) + 24) ≤ "
         "work_mem × hash_mem_multiplier – tool tính sẵn giá trị tối thiểu (khớp lab tới từng kB). SỬA CODE: NOT EXISTS "
         "(anti-join, không phụ thuộc work_mem; chú ý NULL). Index KHÔNG giúp (SubPlan duyệt tuplestore của CTE). "
         "SubPlan tương quan (tham chiếu cột ngoài) thì không bao giờ hash được.",
         ["BEGIN; SET LOCAL work_mem = '<min từ tool>'; EXPLAIN ...; ROLLBACK;   -- plan có quay lại 'hashed SubPlan'?",
          "... WHERE NOT EXISTS (SELECT 1 FROM <list> l WHERE l.col = p.col)   -- thay cho p.col NOT IN (SELECT col FROM <list>)"],
         ["subselect_src", "tom_notin", "perf_12_17", "notin_wiki", "subquery", "hint_table", "hash_mem", "cte_stats_pganalyze"]),
    # --------------------------------------------------------------- compare
    Rule("CMP-REGRESSION", "Thời gian thực thi tăng giữa hai phiên bản", "compare",
         "Cùng câu query, cùng dữ liệu, bản mới chậm hơn. Các finding khác cho biết vì sao.",
         "Đi từ node phân kỳ đầu tiên (CMP-DIVERGE) và node có sai lệch ước lượng lớn nhất.", [], ["explain"]),
    Rule("CMP-DIVERGE", "Plan hai phiên bản phân kỳ", "compare",
         "Cấu trúc plan khác nhau từ một điểm cụ thể (join method / thứ tự join / scan method). Điểm phân kỳ đầu tiên "
         "theo thứ tự thực thi thường là nơi quyết định của planner khác đi.",
         "So sánh số dòng ước lượng ở đầu vào của node phân kỳ trên hai bản; quyết định khác nhau thường do ước lượng khác nhau.",
         [], ["explain"]),
    Rule("CMP-JOIN-METHOD", "Đổi phương thức join cho cùng tập bảng", "compare",
         "Ví dụ Hash Join (PG13) → Nested Loop (PG17). Nested Loop chỉ tốt khi outer nhỏ; nếu outer thực tế lớn → thảm hoạ.",
         "Thử SET LOCAL enable_nestloop/enable_hashjoin/enable_mergejoin để xác nhận plan cũ nhanh hơn, sau đó sửa ước lượng.",
         ["BEGIN; SET LOCAL enable_nestloop = off; EXPLAIN (ANALYZE, BUFFERS) ...; ROLLBACK;"], []),
    Rule("CMP-SCAN-METHOD", "Đổi phương thức scan cho cùng bảng", "compare",
         "Seq Scan ↔ Index Scan ↔ Bitmap… thay đổi do ước lượng selectivity, cost parameters (random_page_cost, "
         "effective_cache_size) hoặc index khác/invalid sau upgrade.",
         "Diff pg_settings; kiểm tra index tồn tại & hợp lệ (pg_index.indisvalid).", [], []),
    Rule("CMP-EST-SHIFT", "Cùng node nhưng ước lượng khác nhau giữa hai phiên bản", "compare",
         "Hai bản ước lượng khác nhau ≥10× cho cùng node. Nguyên nhân phổ biến khi 13→17: (1) bản mới thiếu stats; "
         "(2) PG17 lấy stats của bảng gốc cho cột CTE (f7816aec2) – stats của TOÀN BẢNG, không phản ánh điều kiện lọc "
         "trong CTE, nên với tập con tập trung (skew) có thể sai nhiều hơn mặc định 200 của PG13; (3) extended stats/"
         "STATISTICS target khác nhau; (4) sample ANALYZE khác.",
         "Bên nào gần actual hơn? Nếu PG13 đúng nhờ 'may mắn' với mặc định: cung cấp stats thật cho tập con (temp table + "
         "ANALYZE), hoặc đưa điều kiện lọc ra ngoài/NOT MATERIALIZED để planner thấy.",
         [], ["cte_stats_pganalyze", "bug18466", "boringsql_cte"], since="17"),
    Rule("CMP-SUBPLAN-HASH-LOST", "Bản cũ dùng 'hashed SubPlan', bản mới thì không", "compare",
         "Đây là regression kiểu 'vài giây → vài giờ': cùng điều kiện NOT IN/IN (subquery) nhưng bản mới không còn bảng băm, "
         "chuyển sang duyệt danh sách cho từng dòng. Nguyên nhân thường gặp khi 13→17: ước lượng số dòng của subquery ở bản "
         "mới lớn hơn (PG17 dùng stats xuyên CTE; hoặc stats mới) nên bảng băm ước lượng vượt work_mem × hash_mem_multiplier; "
         "hoặc work_mem/hash_mem_multiplier của instance mới nhỏ hơn.",
         "Như PLAN-SUBPLAN-NOHASH. Không sửa code: hint table Set(hash_mem_multiplier) / ALTER ROLE … IN DATABASE … SET / flag. "
         "Sửa code: NOT EXISTS. Revert code ứng dụng KHÔNG cứu được vì câu SQL cũ vẫn gặp planner PG17. Kiểm tra lại "
         "work_mem/hash_mem_multiplier giữa hai instance (Cloud SQL flags).",
         ["BEGIN; SET LOCAL hash_mem_multiplier = <min từ tool>; EXPLAIN ...; ROLLBACK;"],
         ["subselect_src", "tom_notin", "perf_12_17", "hint_table", "alter_role", "cloudsql_flags", "notin_wiki", "cte_stats_pganalyze"],
         since="17"),
    Rule("CMP-CTE-PATHKEYS", "PG17 dùng thứ tự sort của CTE (Merge Join trên CTE Scan)", "compare",
         "Từ PG17 (commit a65724dfa) planner biết kết quả CTE đã sắp xếp và có thể chọn Merge Join không cần Sort. "
         "Thường tốt, nhưng làm plan khác PG13 và có thể kéo theo index scan toàn bộ bảng phía bên kia.",
         "So sánh thời gian node Merge Join; thử bỏ ORDER BY trong CTE hoặc SET LOCAL enable_mergejoin = off để so sánh.",
         ["BEGIN; SET LOCAL enable_mergejoin = off; EXPLAIN (ANALYZE) ...; ROLLBACK;"],
         ["cte_stats_pganalyze"], since="17"),
    Rule("CMP-NEW-NODE", "Node type chỉ có ở bản mới", "compare",
         "Bản mới dùng kỹ thuật không có trong plan cũ (Memoize – PG14; Right Anti Join, Parallel Hash Right/Full Join – PG16; "
         "Incremental Sort cho DISTINCT…). Mỗi kỹ thuật có GUC để tắt khi chẩn đoán.",
         "Tắt GUC tương ứng trong transaction thử nghiệm để xem có phải nguyên nhân.", [], ["pg14_rel", "pg16_rel", "pg17_rel"]),
    Rule("CMP-NOSTATS", "Bản mới có dấu hiệu thiếu thống kê", "compare",
         "Ước lượng số dòng cho scan toàn bảng ở bản mới lệch khỏi bản cũ (reltuples chưa biết), hoặc ước lượng nhóm = 200. "
         "pg_upgrade (Cloud SQL in-place upgrade) KHÔNG chuyển optimizer statistics (trước PG18).",
         "vacuumdb --all --analyze-in-stages; xác minh bằng collector health (reltuples, last_analyze, pg_stats).",
         ["vacuumdb --all --analyze-in-stages --jobs=8 -h <host> -U <user>"], ["pgupgrade", "cloudsql_upgrade"]),
    Rule("CMP-CTE-CORRELATION", "PG17: ước lượng CTE sụp còn ~1 dòng do stats bảng gốc + điều kiện tương quan", "compare",
         "Từ PG17, cột của CTE (Var thuần) được gắn stats của BẢNG GỐC (f7816aec2). Khi query lọc trên nhiều cột của CTE, "
         "planner nhân selectivity của từng điều kiện theo giả định độc lập, dùng phân bố của TOÀN BẢNG – trong khi dữ liệu "
         "trong CTE đã được lọc và các cột thường TƯƠNG QUAN (vd. đơn OPEN đều là đơn mới, ưu tiên cao, giá trị lớn). Kết quả: "
         "ước lượng sụp còn 1 dòng → Nested Loop quét lại CTE. PG13 không có stats cho cột CTE nên dùng hằng mặc định và "
         "'may mắn' ước lượng lớn hơn. Đây KHÔNG phải do thiếu ANALYZE – ANALYZE lại không giúp được.",
         "Dùng NOT MATERIALIZED (planner thấy bảng gốc và index – lab CASE 3: 20.6 s → 0.37 s); hoặc materialize vào temp "
         "table + index rồi ANALYZE để có stats của CHÍNH tập con. Lưu ý: CREATE STATISTICS trên bảng gốc KHÔNG giúp được "
         "CTE Scan materialized (planner chỉ mượn stats từng cột) – đã thử trong lab, ước lượng vẫn 1 dòng. Không sửa code: hint "
         "table pg_hint_plan (HashJoin(...) hoặc Set(enable_nestloop off)) chỉ cho câu này. Không có GUC tắt f7816aec2; "
         "cộng đồng PG coi đây là hành vi đúng (TPC-DS q95 chậm 2× trên PG17 nhưng không sửa).",
         ["WITH x AS NOT MATERIALIZED (...)   -- thử trong transaction và so sánh plan",
          "CREATE TEMP TABLE x AS <cte query>; CREATE INDEX ON x(<join key>); ANALYZE x;"],
         ["cte_stats_pganalyze", "q95", "ext_stats", "boringsql_cte", "hint_table"], since="17"),
    Rule("CMP-SETTINGS", "Tham số planner khác nhau (theo EXPLAIN SETTINGS)", "compare",
         "Các tham số được in bởi EXPLAIN (SETTINGS) khác nhau giữa hai lần chạy – plan có thể khác vì cấu hình chứ không vì phiên bản.",
         "Đồng bộ tham số rồi so sánh lại.", [], []),
    Rule("CMP-SPILL", "Bản mới tràn đĩa, bản cũ không", "compare",
         "Hash/Sort/HashAgg ở bản mới dùng đĩa: work_mem/hash_mem_multiplier khác, hoặc ước lượng thiếu làm số batch ban đầu nhỏ.",
         "Diff work_mem, hash_mem_multiplier; tăng tạm work_mem để xác nhận.", [], ["hash_mem"]),
    Rule("CMP-JIT", "JIT ở bản mới tốn nhiều hơn", "compare",
         "JIT bật/tốn thời gian hơn ở bản mới (cost ước lượng lớn hơn hoặc cấu hình jit khác).", "SET jit = off để so sánh.",
         [], ["jit"]),
    Rule("CMP-GROUPKEY-ORDER", "Thứ tự Group Key/Sort Key thay đổi", "compare",
         "PG17 có thể đổi thứ tự cột GROUP BY để khớp thứ tự sort của node con (enable_group_by_reordering).",
         "SET LOCAL enable_group_by_reordering = off để so sánh.",
         ["BEGIN; SET LOCAL enable_group_by_reordering = off; EXPLAIN (ANALYZE) ...; ROLLBACK;"], ["groupby"], since="17"),
    # -------------------------------------------------------------- settings
    Rule("SET-DIFF", "Tham số quan trọng khác nhau giữa hai instance", "settings",
         "Tham số ảnh hưởng planner/executor khác nhau → plan khác nhau mà không phải do phiên bản.",
         "Đồng bộ (Cloud SQL database flags) hoặc ghi nhận khác biệt có chủ đích.", [], []),
    Rule("SET-ENABLE-OFF", "enable_* đang bị tắt", "settings",
         "Một enable_* = off ở mức instance ép planner tránh một loại plan – thường là 'thuốc' cũ từ PG13 không còn đúng ở PG17 (hoặc ngược lại).",
         "Xem lại lý do; chỉ nên đặt ở mức session/role cho query cụ thể.", [], []),
    Rule("SET-REMOVED", "Tham số đã bị bỏ/đổi tên ở bản mới", "settings",
         "Flag cấu hình ở bản cũ không còn tồn tại (vd. vacuum_cleanup_index_scale_factor – PG14, stats_temp_directory – PG15, "
         "force_parallel_mode → debug_parallel_query – PG16, old_snapshot_threshold – PG17). Hành vi tương ứng thay đổi.",
         "Kiểm tra release notes và cấu hình thay thế.", [], ["pg14_rel", "pg15_rel", "pg16_rel", "pg17_rel"]),
    Rule("SET-NEW-PLANNER-GUC", "GUC planner mới (mặc định bật) – công tắc chẩn đoán", "settings",
         "Bản mới có kỹ thuật planner mới, mặc định bật: enable_memoize (14), enable_async_append (14), "
         "enable_presorted_aggregate (16), enable_group_by_reordering (17)… Đây là các công tắc để chẩn đoán regression.",
         "Thử tắt từng công tắc trong transaction thử nghiệm với query bị chậm.", [], ["pg17_rel"]),
    Rule("QCFG-DIFF", "Cấu hình hiệu lực khi chạy CHÍNH câu lệnh này khác nhau", "settings",
         "So pg_settings lấy NGAY trong session chạy câu lệnh trên mỗi server (đã gồm ALTER ROLE/DATABASE … SET, tham số "
         "kết nối của client), chỉ giữ tham số điều khiển các node có trong plan. 'critical' = tham số nằm trong cơ chế của "
         "một finding (vd. work_mem/hash_mem_multiplier với NOT IN mất hashed). EXPLAIN (SETTINGS) không đủ: nó chỉ in tham "
         "số khác mặc định, nên mặc định đổi theo phiên bản (hash_mem_multiplier 1.0 → 2.0 từ PG15) không bao giờ hiện ra.",
         "Đồng bộ tham số khác biệt không chủ đích (Cloud SQL flag, ALTER ROLE … IN DATABASE … SET); nếu tham số then chốt "
         "giống nhau mà plan vẫn khác thì nguyên nhân nằm ở planner/thống kê.",
         ["pgdoctor run --old-dsn … --new-dsn … --query-file q.sql",
          "SELECT name, setting, source FROM pg_settings WHERE name IN ('work_mem','hash_mem_multiplier');"],
         ["hash_mem", "alter_role", "cloudsql_flags"]),
    Rule("QCFG-NEW-GUC", "Plan bản mới dùng tính năng planner không có ở bản cũ", "settings",
         "Plan của bản mới có node chỉ tồn tại từ phiên bản sau (Memoize – PG14, Incremental Sort – PG13, GROUP BY "
         "reordering – PG17…) và tham số bật/tắt nó đang bật.",
         "Thử tắt tham số đó trong transaction thử nghiệm; nếu nhanh lại thì tắt riêng cho câu lệnh (hint table Set(…)) "
         "hoặc cho role.", ["BEGIN; SET LOCAL enable_memoize = off; EXPLAIN (ANALYZE) …; ROLLBACK;"],
         ["memoize", "groupby", "pg17_rel"]),
    Rule("QCFG-VERDICT", "Kết luận: config hay ước lượng gây khác biệt", "settings",
         "Với SubPlan NOT IN, tool tính quyết định hash ở hai bên bằng giá trị THẬT của session: ước lượng × (width+24) so "
         "với work_mem × hash_mem_multiplier. Nếu bản mới có ngân sách bộ nhớ bằng/lớn hơn mà vẫn không hash thì nguyên nhân "
         "là ước lượng của planner, không phải config.", "Xem phần Khắc phục.", [], ["subselect_src", "cte_stats_pganalyze"]),
    # ---------------------------------------------------------------- health
    Rule("HEALTH-NEVER-ANALYZED", "Bảng chưa có thống kê / chưa từng ANALYZE", "health",
         "reltuples < 0 hoặc không có dòng nào trong pg_stats: planner dùng heuristic mặc định. Sau pg_upgrade mọi bảng ở trạng thái này "
         "cho tới khi ANALYZE; autovacuum KHÔNG tự analyze bảng ít ghi (n_mod_since_analyze reset về 0).",
         "vacuumdb --all --analyze-in-stages, hoặc ANALYZE từng bảng; kiểm tra log lỗi.", [], ["pgupgrade"]),
    Rule("HEALTH-PARTITION-PARENT", "Bảng partitioned (cha) chưa có thống kê", "health",
         "Autovacuum không bao giờ ANALYZE bảng partitioned cha; stats của cha (inherited) cần cho ước lượng join/aggregate trên toàn bảng.",
         "ANALYZE <partitioned_parent>; định kỳ sau khi dữ liệu thay đổi nhiều.", [], ["pg17_rel"]),
    Rule("HEALTH-EXTSTATS-NODATA", "Extended statistics đã định nghĩa nhưng chưa có dữ liệu", "health",
         "Định nghĩa CREATE STATISTICS được pg_upgrade giữ lại nhưng dữ liệu thì không; cần ANALYZE.", "ANALYZE bảng tương ứng.",
         [], ["ext_stats", "pgupgrade"]),
    Rule("HEALTH-SEARCHPATH-FUNC", "Hàm dùng trong expression index / materialized view không cố định search_path", "health",
         "PG17 chạy ANALYZE/VACUUM/REINDEX/REFRESH MATERIALIZED VIEW (cả autovacuum) với search_path an toàn (pg_catalog, pg_temp). "
         "Hàm user-defined dùng trong index expression/predicate hoặc matview mà bên trong tham chiếu object không ghi schema sẽ lỗi "
         "→ ANALYZE bảng đó thất bại → bảng không bao giờ có stats.",
         "ALTER FUNCTION <f> SET search_path = <schema>, pg_temp; hoặc schema-qualify trong thân hàm; chạy lại ANALYZE và đọc log.",
         [], ["pg17_rel"], since="17"),
    Rule("HEALTH-EXT-OUTDATED", "Extension chưa được cập nhật sau upgrade", "health",
         "installed_version khác default_version: extension vẫn chạy bản SQL cũ (cost/parallel safety/hàm mới thiếu).",
         "ALTER EXTENSION <name> UPDATE;", [], []),
    Rule("HEALTH-INVALID-INDEX", "Index không hợp lệ (indisvalid = false)", "health",
         "Index invalid (vd. REINDEX/CREATE INDEX CONCURRENTLY thất bại) không được planner dùng nhưng vẫn phải cập nhật khi ghi.",
         "REINDEX INDEX CONCURRENTLY <idx>; hoặc DROP và tạo lại.", [], []),
    Rule("HEALTH-COLLATION", "Collation version không khớp", "health",
         "Phiên bản thư viện collation (glibc/ICU) khác với khi tạo database/index → index text có thể sai thứ tự (sai kết quả, không chỉ chậm).",
         "REINDEX các index trên cột text/varchar; sau đó ALTER DATABASE ... REFRESH COLLATION VERSION.", [], ["collation"]),
    Rule("HEALTH-STALE", "Thống kê có thể đã cũ", "health",
         "n_mod_since_analyze lớn so với số dòng: dữ liệu đã thay đổi nhiều kể từ lần ANALYZE cuối.",
         "ANALYZE bảng; giảm autovacuum_analyze_scale_factor cho bảng lớn.", [], []),
    Rule("HEALTH-DEAD", "Tỉ lệ dead tuple cao", "health",
         "Nhiều dead tuple làm scan chậm, visibility map không set (Index Only Scan phải đọc heap).", "VACUUM (ANALYZE) bảng.",
         [], ["vacuum_vm"]),
    Rule("HEALTH-NDISTINCT-SHIFT", "n_distinct của cột khác nhau đáng kể giữa hai bản", "health",
         "Stats khác nhau (sample ANALYZE khác, statistics target khác, dữ liệu khác) → ước lượng join/group khác → plan khác.",
         "Tăng STATISTICS target cho cột quan trọng, hoặc cố định bằng ALTER TABLE ... ALTER COLUMN ... SET (n_distinct = ...).",
         [], ["row_est"]),
    Rule("HEALTH-STATS-MISSING-COLS", "Cột có stats ở bản cũ nhưng không có ở bản mới", "health",
         "Bản mới thiếu stats cho các cột này (chưa ANALYZE, ANALYZE lỗi, hoặc ANALYZE theo danh sách cột).",
         "ANALYZE bảng tương ứng và đọc log lỗi.", [], ["pgupgrade"]),
    Rule("HEALTH-STATS-TARGET", "Statistics target khác nhau", "health",
         "default_statistics_target hoặc STATISTICS theo cột khác nhau giữa hai bản → độ chính xác MCV/histogram khác.",
         "Đồng bộ với bản cũ (ALTER TABLE ... ALTER COLUMN ... SET STATISTICS ...).", [], []),
    # ------------------------------------------------------------ statements
    Rule("PGSS-REGRESSION", "Query chậm hơn theo pg_stat_statements", "statements",
         "mean_exec_time bản mới / bản cũ vượt ngưỡng. queryid có thể thay đổi giữa major version nên tool ghép theo text đã chuẩn hoá.",
         "Lấy EXPLAIN (ANALYZE, BUFFERS, SETTINGS, FORMAT JSON) của query này trên hai bản và chạy pgdoctor để tìm nguyên nhân.",
         [], ["pgss"]),
    Rule("PGSS-TEMP", "Query ghi temp nhiều hơn ở bản mới", "statements",
         "temp_blks_written tăng: sort/hash tràn đĩa (work_mem, hash_mem_multiplier, ước lượng sai).",
         "Xem PLAN-SPILL/CMP-SPILL cho plan của query.", [], ["hash_mem"]),
    Rule("PGSS-IO", "Query đọc đĩa nhiều hơn ở bản mới", "statements",
         "shared_blks_read tăng mạnh: plan đọc nhiều dữ liệu hơn (scan method khác) hoặc cache lạnh.",
         "So sánh plan; chạy lại sau khi cache ấm.", [], []),
]

RULES: Dict[str, Rule] = {r.id: r for r in _R}


def refs_for(rule_id: str):
    r = RULES.get(rule_id)
    if not r:
        return []
    return [REF[k] for k in r.refs if k in REF]
