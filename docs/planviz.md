# planviz: phân tích và trực quan hoá EXPLAIN của PostgreSQL

`planviz` là công cụ riêng, tách khỏi `pgdoctor`, chuyên trả lời các câu hỏi về **một plan** (hoặc vài plan của cùng một câu lệnh):

- Thời gian thực sự nằm ở node nào, sau khi đã trừ thời gian của các node con.
- Planner ước lượng sai bao nhiêu lần (×N) ở từng node, và **sai số đó sinh ra tại node nào**. Một node có thể lệch ×80 chỉ vì nhận đầu vào đã sai, chứ không phải do chính nó.
- Node nào bị quét lại hàng nghìn lần, tràn bộ nhớ ra đĩa, hoặc đọc rồi bỏ gần hết dữ liệu.
- So với plan cũ (PG13), hoặc với plan sau khi sửa (hint, NOT EXISTS), thì cái gì đã thay đổi.

Đầu ra gồm hai phần. Một là bản tóm tắt trên console (để đọc nhanh hoặc để agent đọc). Hai là **một file HTML tự chứa**: mở được offline, không tải gì từ mạng, và có thể dán hay kéo-thả thêm plan khác vào ngay trong trình duyệt. Phần phân tích chạy bằng JavaScript nhúng trong trang, nên plan của production không bị gửi đi đâu.

Mẫu đã sinh từ lab: [`lab/results/planviz/`](../lab/results/planviz/). Trong đó `case4-pg13-pg17.html` gồm 4 plan: PG13, PG17, PG17 kèm hint work_mem, PG17 viết lại bằng NOT EXISTS.

---

## 1. Chạy

Chỉ cần Python 3.8 trở lên, không phải cài thêm thư viện nào.

```bash
CLI=".claude/skills/planviz/planviz_cli.py"

# Một plan: in tóm tắt và ghi trang HTML
python $CLI report q.pg17.json -o q.html

# Nhiều plan của cùng câu lệnh: có thêm phần so sánh (plan đầu tiên là gốc)
python $CLI report q.pg13.json q.pg17.json fix.pg17.txt --labels "PG13,PG17,PG17 + NOT EXISTS" -o cmp.html --json cmp.json

# Chỉ in tóm tắt
python $CLI summary q.pg17.json

# Trang công cụ trống (có sẵn ví dụ) để dán plan: gửi cho đồng nghiệp, mở bằng trình duyệt là dùng được
python $CLI app -o planviz.html

# Chạy EXPLAIN trực tiếp qua psql rồi phân tích (xem Mục 6 về an toàn)
python $CLI explain --dsn "host=... dbname=... user=..." --query-file q.sql --analyze --timeout 30min \
       -o q.pg17.json --html q.html
```

### Plan nhận vào

Tool tự nhận dạng định dạng:

| Đầu vào | Ghi chú |
|---|---|
| `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` | Đầy đủ nhất, nên dùng. Thêm `VERBOSE, SETTINGS` để có `Output`, `Query Identifier` và các tham số khác mặc định. |
| EXPLAIN dạng text | Bản gốc, hoặc copy từ psql (khung `QUERY PLAN`, dấu `+`, `(57 rows)`, `Time:`, `BEGIN`… đều được bỏ qua). |
| Log của auto_explain | Dạng text (có tiền tố `LOG: duration … plan:`) hoặc JSON. |
| Thư mục của `pgdoctor capture` / `pgdoctor run` | Dùng `plan_analyze.json`, nếu không có thì `plan.json`. Tên plan lấy theo phiên bản server. |
| `-` | Đọc từ stdin, ví dụ `psql -XAt -f q.sql \| planviz summary -`. |

Tên plan mặc định lấy từ phần `.pgNN.` trong tên file (`q.pg17.json` thành `PG17`). Dùng `--labels` để tự đặt tên.

---

## 2. Đọc trang HTML

Thứ tự trên trang là: thanh plan, dải số liệu chính, danh sách phát hiện, rồi năm chế độ xem. Khung **chi tiết node** nằm bên phải. Bấm vào một node ở bất kỳ đâu (sơ đồ, bảng, biểu đồ, phát hiện) thì mọi chế độ xem đều trỏ tới đúng node đó.

### 2.1. Sơ đồ

Cây plan vẽ từ trên xuống. Node gốc (Update/Select) ở trên cùng, dữ liệu chảy từ lá lên.

- **Dải màu trên đầu mỗi node** là chỉ số đang chọn ở ô "Tô màu theo":
  - *Thời gian riêng*: thang xám, càng đậm thì càng chiếm nhiều phần trăm thời gian.
  - *Sai số ước lượng*: thang phân kỳ. Xanh dương là planner ước lượng **thừa**, cam là ước lượng **thiếu**, xám là lệch trong vòng 10×.
  - *Sai số tại chỗ*: như trên, nhưng chỉ tính phần sai số sinh ra ở chính node (Mục 3.3).
  - *Buffers đọc*, *Ghi/đọc file tạm*: phần riêng của node so với tổng của cả plan.
- **Độ dày cạnh** là số dòng node con trả lên node cha, thang log: 1 dòng thì mảnh, 1 triệu dòng thì dày. Nhãn trên cạnh ghi số dòng thực tế và số loop.
- Cạnh nét đứt nối **SubPlan / InitPlan / CTE**; tên của chúng ghi phía trên node con.
- Viền đậm đánh dấu **đường nóng**: đi từ gốc xuống, mỗi bước chọn node con tốn thời gian nhất.
- Dòng cuối trong mỗi node là `ước lượng → thực tế` (mỗi loop) kèm chip `↑85×` / `↓12×`. `×5.0K` ở góc phải là số loop.
- Kéo để di chuyển, cuộn để phóng to; nút ⤢ đưa cả cây về vừa khung. Ô tìm kiếm làm mờ những node không khớp (tìm theo bảng, CTE, index hoặc chữ trong điều kiện).

### 2.2. Bảng cây

Kiểu explain.depesz.com: mỗi dòng một node, thụt lề theo cây. Các cột:

- *Riêng*: thời gian và % (nền đậm theo %).
- *Tổng*: thời gian gồm cả node con.
- *Ước lượng / Thực tế*: số dòng **mỗi loop**, đúng như EXPLAIN in ra. Rê chuột lên ô để xem tổng.
- *Sai số / Tại chỗ*: hai loại sai số (Mục 3.2, 3.3).
- *Loops*.
- *Buffers / Tạm*: phần riêng của node.

Bấm tiêu đề cột để sắp xếp. Ví dụ, sắp theo *Tại chỗ* là tìm ngay được chỗ planner đoán sai.

### 2.3. Ước lượng ↔ thực tế

Biểu đồ log-log: trục ngang là số dòng ước lượng, trục dọc là số dòng thực tế, cả hai tính mỗi loop.

- Đường chéo là ước lượng đúng. Vùng xám là sai trong vòng 10×.
- Chấm nằm phía trên đường chéo: planner đoán **thiếu**. Phía dưới: đoán **thừa**.
- Diện tích chấm tỉ lệ với thời gian riêng, nên chấm to và nằm xa đường chéo là chỗ đáng xem nhất.

### 2.4. Thời gian

Biểu đồ icicle (flame graph lộn ngược):

- Bề rộng ô là thời gian gồm cả node con; phần còn trống bên phải một ô là thời gian riêng của nó.
- Plan của CTE được đặt **dưới CTE Scan đã kích hoạt nó**. InitPlan được đặt dưới node đã đọc kết quả của nó (Mục 3.1). Nhờ vậy cây thời gian phản ánh đúng thứ tự thực thi, khác với cây cú pháp.
- Thời gian không thuộc node nào (trigger, JIT, phần ngoài cây plan) ghi ở dưới biểu đồ.

### 2.5. So sánh (từ 2 plan trở lên)

- Chọn plan A (gốc) và plan B.
- **Bảng chỉ số**: thời gian thực thi, lập kế hoạch, sai số lớn nhất, buffers, file tạm, số node; cột cuối ghi B so với A (▲ ×N là tệ hơn, ▼ ÷N là tốt hơn).
- **Bảng node ghép được**, xếp theo chênh lệch thời gian riêng lớn nhất trước. Mỗi bên có ước lượng → thực tế, sai số và thời gian riêng kèm số loop. Bấm một dòng để mở node đó bên B.
- **Khác biệt về hình dạng**: các node chỉ có ở một bên, ví dụ NOT IN (SubPlan) đổi thành Hash Anti Join.
- **Biểu đồ đoạn nối**: mỗi đoạn nối cùng một node ở A (vòng tròn rỗng) và B (chấm đặc) trên mặt phẳng ước lượng/thực tế. Đoạn dài nghĩa là ước lượng hoặc thực tế đã đổi giữa hai plan.

Cách ghép node giữa hai plan: dựa vào **tập bảng/CTE mà node đọc** (cùng phạm vi CTE/sub-plan) cộng với loại node, rồi đến nhóm loại (scan/join). Không dựa vào số thứ tự `SubPlan 1` / `SubPlan 2`, vì số này đổi giữa các phiên bản (PG13 in `hashed SubPlan 2`, PG17 in `(SubPlan 2).col1`).

### 2.6. Khung chi tiết node

Khung này hiển thị:

- Phát hiện gắn với node.
- Thời gian: riêng, gồm con, mỗi loop, thời điểm trả dòng đầu tiên.
- Số dòng và **phân rã sai số**: `tích luỹ = đầu vào × tại chỗ`.
- **Sai số lan lên các node cha**: thanh phân kỳ từ node lên tới gốc, cho thấy sai số được khuếch đại hay triệt tiêu dọc đường.
- Bộ nhớ (Sort, Hash, HashAgg, Memoize) và bảng buffers gồm con / riêng.
- Node tương ứng ở các plan khác, có nút mở sang.
- Toàn bộ thuộc tính còn lại của planner (Filter, Index Cond, Output…).

---

## 3. Các con số được tính thế nào

### 3.1. Thời gian riêng (exclusive time)

EXPLAIN ANALYZE in `actual time=startup..total` là **trung bình mỗi loop** và đã **gồm cả node con**. planviz tính như sau:

1. **Thời gian gồm con** = `Actual Total Time × Actual Loops`. Với node chạy trong worker song song (nằm dưới `Gather` / `Gather Merge`), giá trị này được chia cho số tiến trình `min(loops, Workers Launched + 1)`. Lý do: các worker chạy đồng thời, cộng dồn lại sẽ vượt thời gian thật.
2. **Thời gian riêng** = gồm con − tổng thời gian gồm con của các node con, với hai ngoại lệ mà cách cộng trừ ngây thơ sẽ sai:
   - **CTE**. Plan của `CTE x` treo dưới node gốc của query, nhưng thực ra chỉ chạy khi một `CTE Scan on x` đọc dòng đầu tiên. Vì vậy thời gian của nó đã nằm trong CTE Scan đó. planviz không trừ CTE khỏi node mà nó treo vào (nếu trừ sẽ thành hai lần và node gốc bị âm). Thay vào đó, planviz trừ khỏi các CTE Scan của CTE đó, lần lượt theo thứ tự plan.
   - **InitPlan**. PostgreSQL gắn InitPlan vào node trên cùng của query, nhưng chỉ tính nó **lần đầu tiên có biểu thức cần kết quả**. Biểu thức đó có thể nằm ở một node sâu hơn, ví dụ `One-Time Filter: (NOT (InitPlan 3).col1)` của một node `Result`. planviz tìm node đầu tiên có biểu thức nhắc tới `(InitPlan n)` (PG17) hoặc `$n` (PG ≤ 16) và trừ InitPlan khỏi node đó.

     Ví dụ thật trong lab (plan NOT EXISTS + kiểm tra NULL trên PG17): nếu trừ ngây thơ, node `Result` chiếm 74% thời gian (3.1 s) còn `Update` bằng 0. Nếu tính đúng, `Result` chỉ có 2 ms, `Update` có 100 ms, còn 3.1 s thuộc về InitPlan 3, tức CTE Scan dò NULL cùng với CTE mà nó kích hoạt.
3. SubPlan (có tương quan, hoặc NOT IN không hash) chạy trong lúc node cha tính biểu thức, nên được trừ khỏi node cha như node con bình thường.
4. Kết quả được chặn dưới ở 0. Tổng thời gian riêng của mọi node bằng thời gian gồm con của node gốc; test kiểm tra sai lệch dưới 1% trên các plan lab. Phần còn lại tới `Execution Time` là trigger, JIT và chi phí ngoài cây plan.

**Buffers** (shared/local/temp, hit/read/dirtied/written) trong EXPLAIN là **tổng qua các loop và gồm cả node con**. Phần riêng được tính bằng đúng quy tắc như thời gian.

### 3.2. Sai số ước lượng (q-error)

Với mỗi node đã chạy, so sánh **mỗi loop**:

```
lq = log10( max(thực tế, 1) / max(ước lượng, 1) )        hiển thị: ↑10^lq×  (thiếu)  hoặc  ↓10^-lq×  (thừa)
```

So mỗi loop vì `Plan Rows` của planner là mỗi lần thực thi. Ví dụ, node phía trong của Nested Loop chạy 5 000 loop, mỗi loop đúng 1 dòng, là ước lượng đúng chứ không phải lệch ×5000. Node ghi dữ liệu (Update/Insert/Delete không có RETURNING) trả 0 dòng theo thiết kế, nên không được tính.

### 3.3. Sai số tại chỗ: sai số sinh ra ở đâu

Sai số lan lên trên cây: nếu một bảng bị ước lượng thiếu ×100, mọi join phía trên nó cũng lệch ít nhất ×100. Nếu chỉ nhìn sai số tích luỹ thì cả nhánh đều đỏ, không biết sửa ở đâu. planviz tách sai số tích luỹ ra:

```
lq(node) = lq(đầu vào) + lq(tại chỗ)
```

với `lq(đầu vào)` tính theo loại node:

| Node | Đầu vào | Vì sao |
|---|---|---|
| Scan bảng (lá) | 0 | Toàn bộ sai số là của điều kiện lọc hoặc thống kê bảng. |
| Join Inner/Left/Full | lq(phía ngoài) + lq(phía trong, mỗi loop) | Planner tính `dòng = sel × ngoài × trong`, nên trong log thì sai số của hai đầu vào cộng lại. Phần dư là sai số **độ chọn lọc của điều kiện join**. |
| Semi / Anti join | lq(phía ngoài); Right Semi/Anti: phía trong | Số dòng ra chỉ phụ thuộc phía được giữ lại. |
| CTE Scan | lq(node gốc của CTE) | CTE Scan nhận ước lượng của CTE; phần dư là sai số của điều kiện lọc trên CTE. |
| Append / Merge Append | log(Σ thực tế / Σ ước lượng của các nhánh) | Đầu ra là tổng, không phải tích. |
| Node khác (Sort, Hash, Aggregate, Limit, Gather…) | lq(node con dữ liệu) | Phần dư là của gom nhóm, giới hạn… |

Node có `|sai số tại chỗ| ≥ 10×` là **nơi sinh ra sai số** (phát hiện `MISEST`). Đó là điều kiện hoặc join cần sửa thống kê.

Ví dụ trong lab, PG13: `CTE Scan current_brand_code` lệch ↑85×, trong đó đầu vào (chính CTE) đúng ≈1×, nên toàn bộ ↑80× sinh ra ở `Filter: (cbc.region = 'VN')`. Trên PG13 thì CTE không có thống kê cột, planner đoán 0.5% cho mọi điều kiện `=`. Xem `docs/explain-walkthrough.md`.

### 3.4. Ngưỡng hash của SubPlan (NOT IN)

Với SubPlan không được hash (trong biểu thức của node cha không có chữ `hashed SubPlan n`) mà chạy ≥ 10 loop, planviz dựng lại phép so sánh mà planner đã làm (`subplan_is_hashable`, `subselect.c`):

```
ước lượng dòng × (MAXALIGN(width) + 24 B)   ≤   work_mem × hash_mem_multiplier
```

- `work_mem` và `hash_mem_multiplier` lấy từ `Settings` của plan nếu có (chỉ có khi giá trị khác mặc định). Nếu không có thì dùng mặc định theo phiên bản: 4 MB × 1.0 cho PG13/14, × 2.0 từ PG15.
- Phiên bản suy ra từ cú pháp: `(SubPlan 2).col1` là PG17; `Query Identifier` là ≥ PG14; hoặc lấy từ thư mục capture.

Phát hiện ghi rõ cần bao nhiêu bộ nhớ, và `work_mem` tối thiểu để SubPlan được hash lại. Cơ chế và lý do NOT EXISTS không có vấn đề này: xem `docs/solutions.md` mục 6.1.

---

## 4. Danh sách phát hiện

| Mã | Khi nào |
|---|---|
| `SUBPLAN_RESCAN` | SubPlan không hash chạy ≥ 10 lần. Mức độ theo % thời gian của cả nhánh (≥ 50% là nghiêm trọng). Có kèm ngưỡng hash nếu là mẫu NOT IN / `= ANY`. |
| `RESCAN` | Seq/CTE Scan, Materialize, Sort, Aggregate, Hash Join… chạy ≥ 1 000 loop (thường là phía trong Nested Loop) và chiếm ≥ 10% thời gian hoặc trả ≥ 1 triệu dòng. |
| `MISEST` | Sai số **tại chỗ** ≥ 10×; tối đa 6 node nặng nhất. Mức cao nếu ≥ 1000×, hoặc ≥ 100× ở nhánh chiếm ≥ 20% thời gian. |
| `HOTSPOT` | Một node chiếm ≥ 25% thời gian riêng. |
| `SORT_SPILL`, `HASH_SPILL`, `HASHAGG_SPILL` | Sort trên đĩa; Hash nhiều batch (ghi rõ nếu planner tưởng ít batch hơn, tức phía build bị ước lượng thiếu); HashAggregate tràn đĩa. Có gợi ý mức bộ nhớ cần. |
| `TEMP_IO` | Node tự ghi ≥ 100 MB file tạm mà không thuộc các trường hợp trên (ví dụ tuplestore của CTE). |
| `FILTER_WASTE` | Scan bỏ ≥ 90% (và ≥ 100 000) số dòng đã đọc. Gợi ý khác nhau cho Seq Scan, CTE Scan và Index Scan. |
| `LOSSY_BITMAP`, `HEAP_FETCHES` | Bitmap vượt work_mem; Index Only Scan vẫn phải đọc heap vì visibility map cũ. |
| `WORKERS`, `MEMOIZE` | Thiếu worker song song; Memoize trượt cache nhiều hơn trúng. |
| `JIT`, `TRIGGERS`, `PLANNING` | JIT ≥ 10% thời gian; trigger (thường là kiểm tra khoá ngoại) ≥ 10%; lập kế hoạch lâu hơn thực thi. |
| `NO_ANALYZE`, `NO_TIMING`, `NEVER_EXECUTED` | Thông tin: plan không có số thực tế; không có thời gian; có nhánh không chạy. |

Nội dung (tiêu đề, vì sao, hướng xử lý) nằm trong `planviz/assets/rules.json`. Cả CLI và trang HTML đều dùng chung file này.

---

## 5. Ví dụ: đọc case lab PG13 → PG17

```bash
D=.claude/skills/planviz/planviz/assets/demo
python $CLI report $D/pg13_baseline.json $D/pg17_baseline.json $D/pg17_hint_work_mem.json $D/pg17_not_exists.txt \
       --labels "PG13,PG17,PG17 + hint work_mem,PG17 NOT EXISTS" -o lab/results/planviz/case4-pg13-pg17.html
```

Đọc lần lượt từng plan:

1. **PG13 (4.0 s).** `MISEST` ở `CTE Scan current_brand_code` trong SubPlan: ↑80× sinh ra tại chỗ, planner đoán 4.7K dòng, thực tế 400K. Ước lượng nhỏ nên bảng băm ước tính chỉ khoảng 0.3 MB, vừa 4 MB, và SubPlan **được hash**: chạy 1 lần. PG13 nhanh là nhờ đoán sai.
2. **PG17 (20m 28s).** `SUBPLAN_RESCAN` mức nghiêm trọng: `SubPlan 2 chạy lại 5.0K lần`. Không còn `MISEST`, vì PG17 ước lượng 380K, gần đúng. Nhưng 380K × 56 B ≈ 20.3 MB lớn hơn 8 MB nên không hash; cần work_mem ≥ 10.1 MB. Ở tab So sánh, dòng đầu tiên là CTE Scan: `1.1 s → 17m 03s`, loops `1 → 5.0K`. Biểu đồ đoạn nối cho thấy chấm của PG13 nằm cao trên đường chéo (thiếu ×85), chấm của PG17 về sát đường chéo.
3. **PG17 + hint work_mem (4.2 s).** Hết `SUBPLAN_RESCAN`; SubPlan được hash trở lại.
4. **PG17 NOT EXISTS (4.2 s).** Tab So sánh liệt kê *khác biệt hình dạng*: SubPlan biến mất, xuất hiện `Hash Anti Join`. Phát hiện `HASH_SPILL` (8 batch) cho thấy bảng băm vẫn lớn hơn bộ nhớ, nhưng Hash Join tràn ra đĩa theo batch chứ không quét lại.

Bản tóm tắt console tương ứng: [`lab/results/planviz/summary.txt`](../lab/results/planviz/summary.txt).

---

## 6. Chế độ `explain` (chạy trực tiếp)

```bash
python $CLI explain --dsn "host=10.0.0.5 dbname=app user=app_batch" --query-file q.sql \
       [--analyze] [--timeout 30min] [--verbose] [--psql "docker exec -i pg17 psql -U postgres -d app"] \
       -o q.pg17.json --html q.html
```

Cách tool chạy câu lệnh:

- `q.sql` chứa **đúng một câu lệnh**, không có `EXPLAIN`. File có nhiều câu, hoặc có lệnh meta của psql (`\gset`…), sẽ bị từ chối. Dấu `;` nằm trong chuỗi, dollar-quote hay comment thì vẫn được chấp nhận.
- Script gửi qua psql: `BEGIN; SET LOCAL statement_timeout = '…'; EXPLAIN (…) <câu lệnh>; ROLLBACK;`.
- **Không có `--analyze`**: chỉ EXPLAIN, câu lệnh không được thực thi.
- **Có `--analyze`**: câu lệnh **chạy thật** rồi bị ROLLBACK. Dữ liệu không đổi, nhưng UPDATE/DELETE **giữ khoá dòng** cho tới khi ROLLBACK, và vẫn tốn CPU/IO như chạy thật. Trên production nên chạy ở instance restore hoặc vào giờ thấp điểm. Hết timeout thì psql báo lỗi và tool thoát với mã 2.
- Mật khẩu trong DSN được che (`***`) trong trang HTML.
- Câu dùng tham số `$1`, hoặc cần mô phỏng role/GUC của ứng dụng: dùng `pgdoctor capture` (có `--param`, `--generic-plan`, `--app-name`, `--client-options`), rồi đưa thư mục capture cho planviz.

---

## 7. Giới hạn

- Plan dạng text cần có cost (`COSTS ON`, mặc định). Plan `FORMAT XML/YAML` chưa hỗ trợ; dùng JSON.
- Thời gian riêng là suy luận từ số liệu trung bình mỗi loop. Với InitPlan, nếu nhiều node cùng dùng một kết quả, tool chọn node đầu tiên theo thứ tự plan. Với CTE đọc bởi nhiều CTE Scan, thời gian được gán theo thứ tự plan, có thể khác thứ tự chạy thật.
- Node song song: nếu `parallel_leader_participation = off` thì số tiến trình là số worker; tool lấy `min(loops, workers + 1)`, đủ đúng với plan thường gặp.
- Ghép node giữa hai plan chỉ dựa vào hình dạng. Hai node cùng loại, cùng đọc một tập bảng (hiếm) được ghép theo thứ tự.
- Ngưỡng hash dùng `work_mem` / `hash_mem_multiplier` trong `Settings` của plan. EXPLAIN chỉ in giá trị khác mặc định, và không thấy giá trị đặt bằng `ALTER ROLE`. Muốn biết giá trị hiệu lực thật thì dùng `pgdoctor run`.

---

## 8. Cấu trúc mã và test

```
.claude/skills/planviz/
  SKILL.md            hướng dẫn cho agent (tiếng Anh)
  planviz_cli.py      entry point
  planviz/
    loader.py         nhận dạng đầu vào, chuẩn hoá về cấu trúc FORMAT JSON
    textplan.py       parser EXPLAIN text (lấy từ pgdoctor, thêm khối Planning:, Output, Serialization)
    analyze.py        chỉ số từng node, phát hiện, ghép node giữa hai plan
    render.py         ghép trang HTML (shell + CSS + JS + dữ liệu JSON đã escape)
    explain.py        chế độ chạy qua psql
    cli.py            report / summary / app / explain
    assets/
      core.js         bản JavaScript của loader + textplan + analyze (cho plan dán vào trình duyệt)
      ui.js style.css shell.html   giao diện
      rules.json      nội dung các phát hiện (CLI và trang HTML dùng chung)
      demo/           4 plan ví dụ từ lab
tests/test_planviz.py, tests/planviz_node.js, tests/fake_psql_plan.py
```

`tests/test_planviz.py` có 34 test. Chúng kiểm tra cách đọc đầu vào, thời gian riêng (cộng lại đúng bằng thời gian của gốc; quy tắc CTE, InitPlan, song song), sai số tại chỗ, các phát hiện, ghép node, trang HTML (không có tài nguyên ngoài, không phá được thẻ `<script>`), CLI và chế độ explain (dùng psql giả). Ngoài ra có bộ test **so khớp Python ↔ JavaScript**: chạy `core.js` qua `node` trên mọi plan của lab và tests/samples, và yêu cầu từng con số, từng phát hiện, từng cặp node ghép phải giống nhau. Khi sửa `analyze.py` thì phải sửa `core.js` theo, và ngược lại. Nếu máy không có node, bộ test này được bỏ qua.

```bash
python -m unittest tests.test_planviz -v
```
