"""Remediation planner: turn findings + plans into ranked fix options, options that need NO application change first.

Every option states: does it need a code change / rebuild, how wide its effect is ("blast radius"), whether the
instance must restart, the exact SQL, how to verify it, how to roll it back, and what was measured in the lab
(lab/remedies, PG 13.23 vs 17.11 + pg_hint_plan 1.7.2).

Order of preference (narrowest effect first):
  1. pg_hint_plan hint table   - one statement only (key = query_id), optionally one application_name
  2. ALTER ROLE ... IN DATABASE ... SET   - every statement of one role in one database (new sessions)
  3. Instance flag            - whole instance
  4. Code change (rebuild)    - permanent fix, independent of memory thresholds
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from .findings import Finding, fmt_num
from .plan import Node, Plan
from .rules_plan import subplan_info


@dataclass
class RemedyContext:
    app_role: str = "<app_role>"       # role the application / batch job logs in with
    app_db: str = "<db>"
    app_name: str = ""                 # application_name of the batch job ('' = every application)
    target: str = "cloudsql"           # cloudsql | selfhosted
    instance: str = "<INSTANCE>"
    keyword: str = ""                  # text to find the statement in pg_stat_statements


@dataclass
class Remedy:
    key: str
    title: str
    rank: int                          # lower = try first
    code_change: bool
    blast: str                         # scope of the effect
    restart: bool
    why: str
    steps: List[str] = field(default_factory=list)
    verify: List[str] = field(default_factory=list)
    rollback: List[str] = field(default_factory=list)
    caveats: List[str] = field(default_factory=list)
    lab: str = ""                      # what was measured in lab/remedies
    subject: str = ""                  # query name
    target_desc: str = ""              # node / subplan this addresses
    effective: bool = True             # False = listed only to say it does NOT work

    def to_dict(self) -> Dict:
        return asdict(self)


# ------------------------------------------------------------------ helpers
def _nice_mb(kb: float) -> str:
    """Round a kB value up to a 'nice' work_mem literal (MB, power-of-two-ish steps)."""
    mb = kb / 1024
    for step in (1, 2, 4, 8, 16, 24, 32, 48, 64, 96, 128, 192, 256, 384, 512, 768, 1024, 1536, 2048, 3072, 4096):
        if mb <= step:
            return f"{step}MB"
    return f"{int(math.ceil(mb / 1024))}GB"


def _nice_mult(x: float) -> str:
    for step in (1.5, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 256, 512, 1000):
        if x <= step:
            return f"{step:g}"
    return f"{math.ceil(x):g}"


def _mb(b: float) -> str:
    return f"{b / 1024 ** 2:.1f} MB" if b >= 1024 ** 2 else f"{b / 1024:.0f} kB"


def _qid_lines(plan: Plan, ctx: RemedyContext) -> List[str]:
    kw = ctx.keyword or "<một đoạn text đặc trưng của câu lệnh>"
    lines = []
    if plan.query_id is not None:
        lines.append(f"-- Query Identifier trong plan: {plan.query_id}. CHỈ dùng nếu plan này lấy từ đúng câu ứng dụng gửi:")
        lines.append("--   câu dùng literal ('VN') và câu dùng bind parameter ($1, JDBC/prepared) có query_id KHÁC nhau (lab E3).")
    lines.append("-- (a) Câu ĐANG chạy (vd. đang treo 2 giờ): pg_stat_activity.query_id (PG14+, cần compute_query_id = auto/on)")
    lines.append("SELECT pid, query_id, now() - query_start AS running_for, application_name, usename, left(query, 100) AS query")
    lines.append(f"FROM pg_stat_activity WHERE query ILIKE '%{kw}%' AND pid <> pg_backend_pid();")
    lines.append("-- (b) Câu đã chạy XONG ít nhất một lần (câu bị huỷ/timeout không được pg_stat_statements ghi lại):")
    lines.append("SELECT queryid, calls, round(mean_exec_time) AS mean_ms, left(query, 120) AS query")
    lines.append("FROM pg_stat_statements")
    lines.append(f"WHERE query ILIKE '%{kw}%' ORDER BY total_exec_time DESC LIMIT 10;")
    return lines


def _hint_table_setup(ctx: RemedyContext) -> List[str]:
    out = []
    if ctx.target == "cloudsql":
        out += ["-- B1. Bật extension (Cloud SQL): flag cloudsql.enable_pg_hint_plan=on – CẦN RESTART instance.",
                "--     gcloud sql instances patch " + ctx.instance +
                " --database-flags=cloudsql.enable_pg_hint_plan=on,<TẤT CẢ FLAG ĐANG CÓ>",
                "--     (patch --database-flags GHI ĐÈ toàn bộ flag hiện có – phải liệt kê lại; hoặc sửa trên Console)"]
    else:
        out += ["-- B1. Nạp thư viện: shared_preload_libraries = '..., pg_hint_plan' (cần restart) hoặc session_preload_libraries."]
    out += [f"-- B2. Trong database {ctx.app_db} (user thuộc cloudsqlsuperuser / owner):",
            "SET pg_hint_plan.enable_hint_table = off;   -- BẮT BUỘC khi tạo extension, nếu đang on sẽ lỗi",
            "                                            -- 'column \"query_id\" does not exist' (đã gặp trong lab)",
            "CREATE EXTENSION IF NOT EXISTS pg_hint_plan;",
            "SELECT extversion FROM pg_extension WHERE extname = 'pg_hint_plan';  -- PG17: 1.7.x (hint table khoá theo query_id)"]
    return out


def _enable_hint_table(ctx: RemedyContext) -> List[str]:
    return [f"-- B5. Bật hint table cho role ứng dụng (áp dụng cho SESSION MỚI – cần recycle connection pool):",
            f"ALTER ROLE {ctx.app_role} IN DATABASE {ctx.app_db} SET pg_hint_plan.enable_hint_table = on;",
            "--     (hoặc flag instance pg_hint_plan.enable_hint_table=on – không cần restart, áp dụng mọi session)"]


def _verify_explain(ctx: RemedyContext, expect: str) -> List[str]:
    return [f"-- Đăng nhập BẰNG role {ctx.app_role}" + (f" với application_name={ctx.app_name}" if ctx.app_name else "") +
            " trong một session MỚI, rồi:",
            "BEGIN;",
            "EXPLAIN <nguyên văn câu lệnh>;     -- không cần ANALYZE: plan hiện ra ngay",
            f"--   kỳ vọng: {expect}",
            "ROLLBACK;"]


def _role_set(ctx: RemedyContext, guc: str, val: str) -> List[str]:
    return [f"ALTER ROLE {ctx.app_role} IN DATABASE {ctx.app_db} SET {guc} = {val};",
            "-- Áp dụng cho session MỚI: recycle connection pool (HikariCP maxLifetime / restart pod / pgbouncer RECONNECT).",
            "-- Kiểm tra trong session của ứng dụng: SELECT name, setting, source FROM pg_settings WHERE name = '" + guc + "';",
            "--   source phải là 'database user'. Nếu là 'client' => connection string đang gửi options -c ... ghi đè.",
            "-- Nếu báo 'permission denied': PG16+ yêu cầu ADMIN OPTION trên role đó; hoặc để chính role ứng dụng chạy",
            f"--   ALTER ROLE CURRENT_USER IN DATABASE {ctx.app_db} SET {guc} = {val};"]


# ------------------------------------------------------- non-hashed SubPlan
def _subplan_remedies(name: str, plan: Plan, sp: Node, ctx: RemedyContext) -> List[Remedy]:
    info = subplan_info(plan, sp)
    out: List[Remedy] = []
    tgt = f"{sp.subplan_name}: {info['expr'][:160]}"
    if info["correlated"]:
        out.append(Remedy(
            "subplan-correlated", "SubPlan tương quan (correlated) – không thể hash bằng cấu hình", 1, True,
            "1 câu lệnh", False,
            "Subquery tham chiếu cột của query ngoài nên PostgreSQL (13 → 18) không bao giờ hash nó; tăng work_mem vô ích. "
            "Mỗi dòng ngoài chạy lại subquery.",
            ["-- Viết lại NOT IN/ANY tương quan thành NOT EXISTS/EXISTS với điều kiện nối (anti/semi join),",
             "-- hoặc đảm bảo subquery dùng được index trên cột tương quan (CREATE INDEX ... ON <bảng trong subquery>(<cột>))."],
            subject=name, target_desc=tgt))
        return out
    need = info["hash_bytes"]
    hm, wm = info["hash_mem_multiplier"], info["work_mem_bytes"]
    min_kb, min_hm = info["min_work_mem_kb"], info["min_hash_mem_multiplier"]
    rec_wm = _nice_mb(2 * min_kb)                       # x2 margin: the estimate grows with the data
    rec_hm = _nice_mult(max(2 * min_hm, hm + 1))
    assumed = " (work_mem/hash_mem_multiplier không có trong EXPLAIN SETTINGS ⇒ giả định mặc định " \
              f"{_mb(wm)} × {hm:g})" if info["assumed_defaults"] else ""
    calc = (f"Planner chỉ hash khi rows × (MAXALIGN(width) + 24) ≤ work_mem × hash_mem_multiplier: "
            f"{fmt_num(info['rows'])} × ({info['entry_bytes'] - 24} + 24) B = {_mb(need)} > {_mb(wm * hm)}{assumed}. "
            f"Tối thiểu: work_mem ≥ {min_kb} kB (với hash_mem_multiplier {hm:g}) hoặc hash_mem_multiplier ≥ {min_hm:.2f} "
            f"(với work_mem {_mb(wm)}). Khuyến nghị gấp đôi để chịu được dữ liệu tăng: work_mem = {rec_wm} "
            f"hoặc hash_mem_multiplier = {rec_hm}.")
    act = sp.actual_rows if sp.analyzed and not sp.never_executed else None
    mem_note = (f"Bảng băm của hashed SubPlan KHÔNG bị giới hạn bởi work_mem khi chạy (không spill): bộ nhớ ≈ số dòng THỰC TẾ "
                f"của subquery × {info['entry_bytes']} B + overhead hash table (ước lượng hiện tại {_mb(need)}).")
    lab = ("CASE 4 (PG17.11, 2M dòng, 2 vCPU): không hash 1 228 s; hint/role setting → hashed SubPlan 4.1–5.6 s "
           "(PG13: 4.0 s). Ngưỡng đo được: 10381kB → SubPlan, 10382kB → hashed (khớp công thức).")
    hint = f'Set(hash_mem_multiplier {rec_hm})'
    out.append(Remedy(
        "hint-table-hash-mem", "pg_hint_plan hint table: Set(hash_mem_multiplier) CHỈ cho câu lệnh này", 1, False,
        "1 câu lệnh (khoá query_id)" + (f", chỉ application_name='{ctx.app_name}'" if ctx.app_name else ""),
        ctx.target == "cloudsql",
        calc + " Hint Set() chỉ đổi tham số trong lúc LẬP PLAN (khi thực thi work_mem vẫn như cũ), nên không ảnh hưởng "
               "câu nào khác và không làm sort/hash khác tốn thêm bộ nhớ.",
        _hint_table_setup(ctx) + ["-- B3. Lấy query_id"] + _qid_lines(plan, ctx) + [
            "-- B4. Gắn hint (application_name '' = mọi ứng dụng):",
            "INSERT INTO hint_plan.hints(query_id, application_name, hints)",
            f"VALUES ({plan.query_id if plan.query_id is not None else '<queryid>'}, '{ctx.app_name}', '{hint}');"]
        + _enable_hint_table(ctx),
        _verify_explain(ctx, "dòng Filter có chữ 'hashed SubPlan'"),
        [f"DELETE FROM hint_plan.hints WHERE query_id = {plan.query_id if plan.query_id is not None else '<queryid>'};"],
        ["query_id đổi nếu câu SQL đổi cấu trúc, hoặc bảng bị DROP/CREATE lại (query_id dựa trên OID) – kiểm tra lại sau mỗi lần deploy/migration.",
         "Cloud SQL: bật cloudsql.enable_pg_hint_plan cần restart → làm trong cửa sổ bảo trì.",
         mem_note],
        lab, name, tgt))
    out.append(Remedy(
        "role-hash-mem", "ALTER ROLE … IN DATABASE … SET hash_mem_multiplier (không cần extension)", 2, False,
        f"mọi câu của role {ctx.app_role} trong DB {ctx.app_db}", False,
        calc + " hash_mem_multiplier chỉ nới bộ nhớ cho node dạng hash (Hash Join, HashAgg, hashed SubPlan); "
               "sort vẫn dùng work_mem cũ – hẹp hơn tăng work_mem. Tốt nhất khi job batch có role riêng.",
        _role_set(ctx, "hash_mem_multiplier", rec_hm),
        _verify_explain(ctx, "dòng Filter có chữ 'hashed SubPlan'"),
        [f"ALTER ROLE {ctx.app_role} IN DATABASE {ctx.app_db} RESET hash_mem_multiplier;"],
        ["Mỗi node hash của MỌI câu thuộc role này được dùng tới work_mem × hash_mem_multiplier: tính lại tổng RAM "
         "khi nhiều connection chạy song song.", mem_note],
        "E2b: role-in-database hash_mem_multiplier = 8 (work_mem 4MB) → hashed SubPlan, 4.2 s.", name, tgt))
    out.append(Remedy(
        "role-work-mem", "ALTER ROLE … IN DATABASE … SET work_mem", 3, False,
        f"mọi câu của role {ctx.app_role} trong DB {ctx.app_db}", False,
        calc, _role_set(ctx, "work_mem", f"'{rec_wm}'"),
        _verify_explain(ctx, "dòng Filter có chữ 'hashed SubPlan'"),
        [f"ALTER ROLE {ctx.app_role} IN DATABASE {ctx.app_db} RESET work_mem;"],
        ["work_mem áp dụng cho MỌI sort/hash của role: rộng hơn hash_mem_multiplier.", mem_note],
        "E2a: ALTER ROLE app_batch SET work_mem = '16MB' → hashed SubPlan, 5.6 s.", name, tgt))
    flag_cmd = (f"gcloud sql instances patch {ctx.instance} --database-flags=hash_mem_multiplier={rec_hm},<TẤT CẢ FLAG ĐANG CÓ>"
                if ctx.target == "cloudsql" else f"ALTER SYSTEM SET hash_mem_multiplier = {rec_hm}; SELECT pg_reload_conf();")
    out.append(Remedy(
        "instance-flag", "Flag instance hash_mem_multiplier / work_mem", 4, False, "toàn instance", False,
        calc + " Cách nhanh nhất nhưng rộng nhất.",
        [flag_cmd, "-- Cloud SQL: hash_mem_multiplier (1..1000) và work_mem là flag không cần restart; áp dụng cho session mới."],
        _verify_explain(ctx, "dòng Filter có chữ 'hashed SubPlan'"),
        ["-- đặt lại giá trị cũ của flag (Cloud SQL mặc định: work_mem 4MB, hash_mem_multiplier 2)"],
        ["Ảnh hưởng mọi query của mọi ứng dụng; rủi ro OOM khi nhiều connection cùng dùng hash lớn."],
        "", name, tgt))
    out.append(Remedy(
        "rewrite-not-exists", "Sửa code: NOT IN → NOT EXISTS (anti-join) – sửa vĩnh viễn", 5, True, "1 câu lệnh", False,
        "NOT EXISTS được chuyển thành Hash/Merge Anti Join, không phụ thuộc ngưỡng bộ nhớ của hashed SubPlan. "
        "Chú ý ngữ nghĩa NULL: nếu cột hai bên có thể NULL, dùng dạng tương đương đầy đủ bên dưới.",
        ["-- Khi cả <x> và <s.col> đều NOT NULL:",
         "... AND NOT EXISTS (SELECT 1 FROM <cte> s WHERE <điều kiện cũ> AND s.col = t.x)",
         "-- Tương đương đầy đủ với x NOT IN (SELECT col FROM S) trong WHERE (kể cả khi có NULL):",
         "... AND NOT EXISTS (SELECT 1 FROM S WHERE S.col = t.x)",
         "    AND NOT EXISTS (SELECT 1 FROM S WHERE S.col IS NULL)",
         "    AND (t.x IS NOT NULL OR NOT EXISTS (SELECT 1 FROM S))"],
        ["-- So số dòng bị ảnh hưởng giữa câu cũ và câu mới trên bản clone (UPDATE ... RETURNING trong transaction rồi ROLLBACK)"],
        ["-- deploy lại bản cũ"],
        ["Cần build/deploy lại ứng dụng.",
         "PG19 (chưa phát hành tại thời điểm viết) mới tự chuyển NOT IN → anti-join, và chỉ khi chứng minh được hai vế không NULL."],
        "E7: NOT EXISTS → Hash Anti Join trên PG17 4.4 s, dạng null-safe 4.2 s; số dòng UPDATE giống hệt NOT IN (2500).",
        name, tgt))
    out.append(Remedy(
        "not-effective", "KHÔNG hiệu quả với pattern này (đã thử trong lab)", 9, False, "-", False,
        "Index: SubPlan duyệt kết quả CTE/Materialize (tuplestore, không có index) – thêm index trên products/brand/stores "
        "không đổi plan (lab E4). ANALYZE lại / tăng STATISTICS: ước lượng của PG17 vốn đã gần đúng, vấn đề là ngưỡng. "
        "ALTER COLUMN … SET STATISTICS 0: ANALYZE KHÔNG xoá stats cũ (lab E5). CREATE STATISTICS: không áp dụng cho cột CTE. "
        "Không có GUC nào tắt việc PG17 lấy stats qua CTE (f7816aec2). Revert code ứng dụng: câu SQL cũ vẫn gặp planner PG17.",
        effective=False, subject=name, target_desc=tgt))
    return out


# ------------------------------------------------- nested loop over a CTE
def _alias_list(n: Node) -> List[str]:
    out = []
    for x in n.walk():
        a = x.alias or x.relation
        if a and x.node_type in ("Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan", "CTE Scan",
                                 "Subquery Scan", "Function Scan", "Values Scan") and a not in out:
            out.append(a)
    return out


def _no_nestloop_hint(nl: Node) -> str:
    """NoNestLoop(...) for EVERY alias set that contains the re-scanned inner side.

    Lab E9 (CASE 3): hinting only the flagged join set, HashJoin(cu a b), is NOT enough – pg_hint_plan applies it,
    but the planner re-orders the joins and puts the Nested Loop on the pair {a, b} instead (still 10000 loops).
    Forbidding nested loop for every set that includes the inner alias works: 100 s -> 0.84 s."""
    from itertools import combinations
    aliases = _alias_list(nl)
    inner = nl.inner_child()
    inner_aliases = set(_alias_list(inner)) if inner is not None else set()
    if not inner_aliases or len(aliases) > 6:
        return "Set(enable_nestloop off)"
    sets = [c for k in range(2, len(aliases) + 1) for c in combinations(aliases, k) if inner_aliases & set(c)]
    return " ".join(f"NoNestLoop({' '.join(c)})" for c in sets)


def _nestloop_remedies(name: str, plan: Plan, nl: Node, ctx: RemedyContext) -> List[Remedy]:
    aliases = _alias_list(nl)
    tgt = f"{nl.describe()} {{{', '.join(aliases)}}}"
    join_hint = _no_nestloop_hint(nl)
    return [
        Remedy("hint-table-join", "pg_hint_plan hint table: ép Hash Join / tắt Nested Loop CHỈ cho câu này", 1, False,
               "1 câu lệnh (khoá query_id)", ctx.target == "cloudsql",
               "Nested Loop được chọn vì ước lượng outer ≈ vài dòng (PG17 lấy stats bảng gốc cho cột CTE). Hint table cấm "
               "Nested Loop cho mọi tập alias có chứa phía bị quét lại, mà không sửa SQL. Chỉ hint đúng một tập (vd. HashJoin(cu a b)) "
               "là KHÔNG đủ: planner đổi thứ tự join và đặt Nested Loop xuống cặp nhỏ hơn (lab E9). Alias phải đúng như trong plan.",
               _hint_table_setup(ctx) + ["-- B3. Lấy query_id"] + _qid_lines(plan, ctx) + [
                   "-- B4. Gắn hint (chọn 1):",
                   f"INSERT INTO hint_plan.hints(query_id, application_name, hints) VALUES "
                   f"({plan.query_id if plan.query_id is not None else '<queryid>'}, '{ctx.app_name}', '{join_hint}');",
                   "-- hoặc đơn giản hơn (vẫn chỉ trong phạm vi câu này): 'Set(enable_nestloop off)'"] + _enable_hint_table(ctx),
               _verify_explain(ctx, "không còn Nested Loop quét lại CTE Scan/Materialize"),
               [f"DELETE FROM hint_plan.hints WHERE query_id = {plan.query_id if plan.query_id is not None else '<queryid>'};"],
               ["Join hint áp dụng cho alias của CTE và alias bên trong subquery/thân CTE; dùng pg_hint_plan.debug_print = verbose "
                "để xem hint 'used' / 'not used'."],
               "E9 (CASE 3, PG17): không hint 100 s; HashJoin(cu a b) 95 s (không đủ); "
               "NoNestLoop(a b) NoNestLoop(b cu) NoNestLoop(a b cu) 0.84 s; Set(enable_nestloop off) 1.0 s.", name, tgt),
        Remedy("role-enable-nestloop", "ALTER ROLE … SET enable_nestloop = off (chỉ để cứu tạm)", 6, False,
               f"mọi câu của role {ctx.app_role} trong DB {ctx.app_db}", False,
               "Rộng và thô: câu OLTP cần Nested Loop + index sẽ chậm đi. Chỉ dùng cho role batch riêng và tạm thời.",
               _role_set(ctx, "enable_nestloop", "off"), _verify_explain(ctx, "Hash/Merge Join thay cho Nested Loop"),
               [f"ALTER ROLE {ctx.app_role} IN DATABASE {ctx.app_db} RESET enable_nestloop;"], [], "", name, tgt),
        Remedy("rewrite-cte", "Sửa code: CTE NOT MATERIALIZED hoặc bảng tạm + ANALYZE", 5, True, "1 câu lệnh", False,
               "Inline CTE để planner dùng index/stats trên bảng gốc kèm điều kiện, hoặc materialize ra bảng tạm có index và ANALYZE "
               "để có stats thật của tập con.",
               ["WITH x AS NOT MATERIALIZED (...)",
                "-- hoặc: CREATE TEMP TABLE x AS ...; CREATE INDEX ON x(<khoá join>); ANALYZE x;"],
               [], [], ["Cần build/deploy lại."],
               "CASE 3: NOT MATERIALIZED 16.7 s → 0.37 s.", name, tgt),
    ]


def _guc_off_remedy(name: str, plan: Plan, guc: str, why: str, ctx: RemedyContext, cloudsql_flag: bool) -> Remedy:
    return Remedy(f"off-{guc}", f"Tắt {guc} cho câu này (hint table) hoặc cho role", 1, False,
                  "1 câu lệnh (hint table) / 1 role (ALTER ROLE)", False, why,
                  [f"INSERT INTO hint_plan.hints(query_id, application_name, hints) VALUES "
                   f"({plan.query_id if plan.query_id is not None else '<queryid>'}, '{ctx.app_name}', 'Set({guc} off)');",
                   "-- hoặc:"] + _role_set(ctx, guc, "off")
                  + ([f"-- Cloud SQL: {guc} cũng là database flag (không cần restart)."] if cloudsql_flag else
                     [f"-- Cloud SQL: {guc} KHÔNG phải database flag; dùng hint table hoặc ALTER ROLE."]),
                  _verify_explain(ctx, f"plan không còn dùng tính năng của {guc}"),
                  [f"ALTER ROLE {ctx.app_role} IN DATABASE {ctx.app_db} RESET {guc};"], [], "", name, guc)


# --------------------------------------------------------------- entry point
def remedies_for_query(name: str, old: Optional[Plan], new: Optional[Plan], findings: List[Finding],
                       ctx: Optional[RemedyContext] = None) -> List[Remedy]:
    """Remedies for one query, given its findings (only findings on the NEW plan / comparison drive remedies)."""
    ctx = ctx or RemedyContext()
    plan = new or old
    if plan is None:
        return []
    rules = {f.rule for f in findings if f.side in ("new", "both", None) and f.severity in ("critical", "high", "medium")}
    out: List[Remedy] = []
    # 1) non-hashed ANY/NOT IN SubPlans of the new plan
    seen = set()
    if rules & {"CMP-SUBPLAN-HASH-LOST", "PLAN-SUBPLAN-NOHASH"}:
        for sp in plan.nodes:
            if sp.parent_rel != "SubPlan" or not sp.subplan_name or sp.subplan_name in seen:
                continue
            info = subplan_info(plan, sp)
            if info["hashed"] or not info["any_type"]:
                continue
            seen.add(sp.subplan_name)
            out += _subplan_remedies(name, plan, sp, ctx)
    # 2) nested loops re-scanning a CTE / non-index inner side
    if rules & {"PLAN-NL-RESCAN", "PLAN-NL-BET", "CMP-CTE-CORRELATION"} and not seen:
        nl_ids = [i for f in findings if f.rule in ("PLAN-NL-RESCAN", "PLAN-NL-BET") and f.side in ("new", None)
                  for i in f.node_ids[:1]]
        for nid in nl_ids[:1]:
            n = plan.node(nid)
            if n.node_type == "Nested Loop":
                out += _nestloop_remedies(name, plan, n, ctx)
    # 3) single switches
    if rules & {"PLAN-MEMOIZE"} or any(f.rule == "CMP-NEW-NODE" and "Memoize" in f.message for f in findings):
        out.append(_guc_off_remedy(name, plan, "enable_memoize",
                                   "Memoize (PG14+) chọn sai khi n_distinct của khoá cache bị ước lượng sai: miss/eviction nhiều.",
                                   ctx, True))
    if "CMP-GROUPKEY-ORDER" in rules:
        out.append(_guc_off_remedy(name, plan, "enable_group_by_reordering",
                                   "PG17 đổi thứ tự cột GROUP BY; tắt để quay về hành vi PG13.", ctx, False))
    if rules & {"PLAN-JIT", "CMP-JIT"}:
        if ctx.target == "cloudsql":
            out.append(Remedy("jit-cloudsql", "JIT: không áp dụng trên Cloud SQL", 9, False, "-", False,
                              "Cloud SQL không hỗ trợ LLVM JIT (trang 'Unsupported features'), nên thời gian JIT thấy trong lab/self-hosted "
                              "không xuất hiện trên Cloud SQL. Kiểm tra: SELECT pg_jit_available();",
                              effective=False, subject=name))
        else:
            out.append(_guc_off_remedy(name, plan, "jit", "Biên dịch JIT tốn thời gian khi cost ước lượng khổng lồ.", ctx, False))
    if rules & {"CMP-NOSTATS", "HEALTH-NEVER-ANALYZED"}:
        out.append(Remedy("analyze", "ANALYZE (thiếu thống kê sau pg_upgrade)", 0, False, "các bảng liên quan", False,
                          "pg_upgrade (Cloud SQL in-place upgrade) không chuyển optimizer statistics; Cloud SQL không tự ANALYZE.",
                          ["vacuumdb --all --analyze-in-stages --jobs=4 -h <host> -U <user>",
                           "-- hoặc trong từng DB: ANALYZE VERBOSE;  và đọc lỗi (PG17: safe search_path có thể làm ANALYZE lỗi)"],
                          ["SELECT relname, last_analyze, last_autoanalyze FROM pg_stat_user_tables ORDER BY 2 NULLS FIRST;"],
                          [], [], "CASE 2: > 900 s → 6.0 s sau ANALYZE.", name, ""))
    out.sort(key=lambda r: (not r.effective, r.rank))
    return out


# ----------------------------------------------------------------- rendering
def render_runbook(groups: List[Dict], ctx: RemedyContext) -> str:
    """SQL runbook (comments in Vietnamese) – one block per query, options in order of preference."""
    L = ["-- pgdoctor remedy runbook",
         f"-- target={ctx.target}  role={ctx.app_role}  db={ctx.app_db}  application_name={ctx.app_name or '(mọi)'}",
         "-- Thứ tự: phương án KHÔNG sửa code, phạm vi hẹp nhất trước. Chạy phần 'verify' sau mỗi bước.",
         "-- KHÔNG chạy cả file một lượt: mỗi phương án là một lựa chọn thay thế.", ""]
    for g in groups:
        if not g["remedies"]:
            continue
        L.append("-- " + "=" * 100)
        L.append(f"-- Query: {g['name']}")
        for i, r in enumerate(g["remedies"], 1):
            L.append("-- " + "-" * 100)
            flag = "KHÔNG HIỆU QUẢ" if not r.effective else ("CẦN SỬA CODE" if r.code_change else "KHÔNG sửa code")
            L.append(f"-- [{i}] {r.title}   ({flag}; phạm vi: {r.blast}{'; cần restart' if r.restart else ''})")
            if r.target_desc:
                L.append(f"--     áp dụng cho: {r.target_desc}")
            for line in _wrap(r.why):
                L.append(f"--     {line}")
            if r.lab:
                L.append(f"--     Lab: {r.lab}")
            for c in r.caveats:
                for j, line in enumerate(_wrap(c)):
                    L.append(f"--     {'! ' if j == 0 else '  '}{line}")
            if r.steps:
                L += r.steps
            if r.verify:
                L.append("-- verify:")
                L += r.verify
            if r.rollback:
                L.append("-- rollback:")
                L += r.rollback
            L.append("")
    return "\n".join(L) + "\n"


def _wrap(text: str, width: int = 110) -> List[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width and cur:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines
